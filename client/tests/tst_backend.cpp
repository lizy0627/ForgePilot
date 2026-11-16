#include "TaskController.h"
#include "ConversationController.h"
#include "RagController.h"
#include "ClientUi.h"
#include "HttpFixture.h"
#include <QGuiApplication>
#include <QDir>
#include <QJsonDocument>
#include <QQmlApplicationEngine>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QQuickItem>
#include <QtTest>
#include <algorithm>
#include <QMetaProperty>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QUuid>
#include <QTcpServer>
#include <functional>

namespace {
// Exercise persisted v1 conversations explicitly; desktop submissions use Missions.
bool submitLegacyTask(TaskController &tasks, ConversationController &conversations, const QString &prompt)
{
    QNetworkAccessManager network; network.setProxy(QNetworkProxy::NoProxy);
    auto url=tasks.baseUrl();
    url.setPath("/api/v1/conversations/"+conversations.conversationId()+"/requests");
    QNetworkRequest request(url); request.setHeader(QNetworkRequest::ContentTypeHeader,"application/json");
    QJsonObject body{{"request_id",QUuid::createUuid().toString(QUuid::WithoutBraces)},
        {"mode","task"},{"prompt",prompt},{"use_rag",conversations.useRag()}};
    auto *reply=network.post(request,QJsonDocument(body).toJson(QJsonDocument::Compact));
    QSignalSpy finished(reply,&QNetworkReply::finished);
    if(!reply->isFinished() && !finished.wait(15000)) return false;
    if(reply->error()!=QNetworkReply::NoError || reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt()!=202) {
        qWarning()<<"Legacy fixture admission failed"<<reply->readAll(); return false;
    }
    const auto admitted=QJsonDocument::fromJson(reply->readAll()).object();
    const auto task=admitted.value("task_id").toString();
    if(!task.isEmpty()) tasks.selectTask(task);
    conversations.refresh();
    conversations.refreshHistory();
    return true;
}
}

class BackendTest : public QObject {
    Q_OBJECT
    void jointLocalEdit() {
        const auto url=qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL");
        const auto repository=qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY");
        const auto output=qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT");
        TaskController controller; controller.setBaseUrl(QUrl(url));
        ConversationController conversations(&controller);
        QQmlApplicationEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError> &errors) { for(const auto &e:errors) warnings.append(e.toString()); });
        engine.setInitialProperties({{"controller",QVariant::fromValue(&controller)},{"conversations",QVariant::fromValue(&conversations)},{"developmentTest",true}});
        engine.load(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")));
        QVERIFY2(!engine.rootObjects().isEmpty(),qPrintable(warnings.join('\n')));
        auto *window=qobject_cast<QQuickWindow*>(engine.rootObjects()[0]); QVERIFY(window);
        QVERIFY(QTest::qWaitForWindowExposed(window));
        auto find=[&](const char *name) -> QQuickItem* {
            if(auto *item=window->findChild<QQuickItem*>(name)) return item;
            std::function<QQuickItem*(QQuickItem*)> visit=[&](QQuickItem *item)->QQuickItem* {
                if(item->objectName()==name) return item;
                for(auto *child:item->childItems()) if(auto *found=visit(child)) return found;
                return nullptr;
            };
            return visit(window->contentItem());
        };
        // A clean scene may skip buffer swapping after a screenshot. Wait for the
        // GUI frame phase instead, so a second click needs no artificial delay.
        auto frame=[&] { QSignalSpy painted(window,&QQuickWindow::afterAnimating); window->update(); return !painted.isEmpty() || painted.wait(1000); };
        auto click=[&](const char *name) {
            if(!frame()) return false;
            auto *item=find(name); if(!item || !item->isEnabled() || !item->isVisible()) { qInfo()<<"Joint button unavailable"<<name<<item; return false; }
            QSignalSpy clicked(item,SIGNAL(clicked()));
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,item->mapToScene(QPointF(item->width()/2,item->height()/2)).toPoint());
            if(clicked.isEmpty()) qInfo()<<"Joint click not delivered"<<name<<item->mapToScene(QPointF(0,0))<<item->size();
            return !clicked.isEmpty();
        };
        auto capture=[&](const char *name) { return frame() && window->grabWindow().save(output+'/'+name+".png"); };
        auto read=[&](const QString &path) { QFile file(path); return file.open(QIODevice::ReadOnly)?file.readAll():QByteArray{}; };
        auto details=[&](int tab) { window->setProperty("detailsExpanded",true); find("detailTabs")->setProperty("currentIndex",tab); };
        QTRY_COMPARE(conversations.serverStatus(),QString("connected")); QTRY_VERIFY(!conversations.historyLoading());
        const auto restored=qEnvironmentVariable("FORGEPILOT_RESTART_TASK_ID");
        if(!restored.isEmpty()) {
            conversations.selectConversation(qEnvironmentVariable("FORGEPILOT_RESTART_CONVERSATION_ID"));
            QTRY_VERIFY(!conversations.loading());
            controller.viewHistoryTask(restored); QTRY_COMPARE(controller.state(),QString("completed"));
            QTRY_COMPARE(controller.connectionState(),QString("ended")); details(0);
            QVERIFY(controller.historyReadOnly()); QVERIFY(!find("approveButton")->isEnabled()); QVERIFY(!find("planFeedbackButton")->isEnabled());
            QCOMPARE(controller.planVersion(),2); QCOMPARE(controller.diffFiles().size(),1);
            AgentApiClient api; QVERIFY(api.setBaseUrl(QUrl(url))); QSignalSpy errors(&api,&AgentApiClient::requestError);
            api.approveTask(restored,2); QTRY_COMPARE(errors.size(),1); QCOMPARE(errors[0][3].toInt(),409);
            api.feedbackTask(restored,2,"不能恢复历史任务"); QTRY_COMPARE(errors.size(),2); QCOMPARE(errors[1][3].toInt(),409);
        } else {
            QVERIFY(click("taskModeButton")); conversations.chooseRepository(QUrl::fromLocalFile(repository));
            QTRY_VERIFY(!conversations.busy() && !conversations.loading());
            QCOMPARE(controller.repositoryPath(),QUrl::fromLocalFile(repository).toLocalFile());
            const auto baseline=read(output+"/byte-baseline/main.cpp"); QVERIFY(baseline.size()>=30*1024 && baseline.size()<=60*1024);
            QCOMPARE(read(repository+"/main.cpp"),baseline);
            find("conversationPrompt")->setProperty("text",QString("修复 total_price 的金额计算缺陷；保持公开接口、中文注释和用户备注，保护现有测试。"));
            QSignalSpy builds(&controller,&TaskController::buildFinished), tests(&controller,&TaskController::testFinished);
            QVERIFY(submitLegacyTask(controller,conversations,find("conversationPrompt")->property("text").toString()));
            QTRY_VERIFY_WITH_TIMEOUT(controller.canApprove(),15000);
            const auto task=controller.taskId(); QCOMPARE(controller.planVersion(),1); details(0);
            QCOMPARE(read(repository+"/main.cpp"),baseline); QCOMPARE(builds.size(),0); QCOMPARE(tests.size(),0);
            QVERIFY(controller.diffFiles().isEmpty()); QVERIFY(!QFile::exists(repository+"/.forgepilot/build/CMakeCache.txt"));
            QVERIFY(capture("joint-v1"));
            QVERIFY(click("planFeedbackButton")); find("planFeedbackInput")->setProperty("text",QString("保留公开接口，只修改 total_price 函数实现；保留用户修改与全部非目标字节，使用 edit_file，不能修改测试。"));
            QVERIFY(click("submitPlanFeedbackButton")); QTRY_COMPARE(controller.state(),QString("planning")); QTRY_VERIFY(!controller.feedbackOpen());
            QCOMPARE(controller.taskId(),task); QCOMPARE(controller.planVersion(),1);
            QCOMPARE(read(repository+"/main.cpp"),baseline); QCOMPARE(builds.size(),0); QCOMPARE(tests.size(),0);
            QVERIFY(!find("approveButton")->isEnabled()); QVERIFY(find("stopTaskButton")->isEnabled()); QVERIFY(capture("joint-planning"));
            QNetworkAccessManager network;
            auto *release=network.post(QNetworkRequest(QUrl(url+"/_fixture/release-feedback")),QByteArray{});
            QSignalSpy released(release,&QNetworkReply::finished); if(!release->isFinished()) QVERIFY(released.wait(5000));
            QCOMPARE(release->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt(),200); release->deleteLater();
            QTRY_VERIFY_WITH_TIMEOUT(controller.canApprove() && controller.planVersion()==2,10000);
            QCOMPARE(controller.taskId(),task); QCOMPARE(read(repository+"/main.cpp"),baseline);
            QCOMPARE(builds.size(),0); QCOMPARE(tests.size(),0); QVERIFY(controller.diffFiles().isEmpty());
            QVERIFY(find("planView")->property("text").toString().contains("main.cpp"));
            QVERIFY(find("plan_implementation_steps")->property("text").toString().contains("edit_file"));
            AgentApiClient api; QVERIFY(api.setBaseUrl(QUrl(url))); QSignalSpy errors(&api,&AgentApiClient::requestError);
            api.approveTask(task,1); QTRY_COMPARE(errors.size(),1); QCOMPARE(errors[0][3].toInt(),409); QCOMPARE(errors[0][4].toString(),QString("stale_plan"));
            QCOMPARE(read(repository+"/main.cpp"),baseline); QVERIFY(capture("joint-v2-before-approval"));
            QVERIFY(click("approveButton"));
            QTRY_COMPARE_WITH_TIMEOUT(controller.state(),QString("completed"),60000);
            QTRY_COMPARE(controller.connectionState(),QString("ended"));
            QTRY_VERIFY(!conversations.active() && !conversations.busy() && !conversations.loading());
            QCOMPARE(controller.taskId(),task); QCOMPARE(controller.snapshot().value("approved_plan_version").toInt(),2);
            QCOMPARE(builds.size(),2); QCOMPARE(tests.size(),1); QVERIFY(tests[0][0].toBool());
            QTRY_COMPARE(controller.diffFiles().size(),1);
            QByteArray expected=baseline; expected.replace("    return quantity + unit_price;","    return quantity * unit_price;");
            QCOMPARE(read(repository+"/main.cpp"),expected);
            for(const auto *name:{"counter.h","CMakeLists.txt","tests/counter_test.cpp"})
                QCOMPARE(read(repository+'/'+name),read(output+"/byte-baseline/"+name));
            QFile conversation(output+"/backend-conversation-id.txt"); QVERIFY(conversation.open(QIODevice::WriteOnly));
            QCOMPARE(conversation.write(conversations.conversationId().toUtf8()),conversations.conversationId().toUtf8().size());
        }
        details(1); QVERIFY(click("refreshTraceButton")); QTRY_VERIFY(!controller.traceLoading());
        int feedbacks=0,plans=0;
        for(int i=0;i<controller.storedTrace()->count();++i) {
            const auto row=controller.storedTrace()->get(i); const auto kind=row.value("kind").toString();
            if(kind=="plan_created") ++plans;
            if(kind=="plan_feedback") { ++feedbacks; controller.loadTraceDetail(row.value("sequence").toLongLong()); }
        }
        QCOMPARE(feedbacks,1); QCOMPARE(plans,2); QTRY_VERIFY(controller.traceDetailText().contains("保留公开接口"));
        QVERIFY(capture(restored.isEmpty()?"joint-trace":"joint-restart-trace"));
        details(2); QVERIFY(capture(restored.isEmpty()?"joint-completed":"joint-restarted-history"));
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
        qInfo().noquote()<<"JOINT QML"<<(restored.isEmpty()?"initial":"restart")<<controller.taskId()<<"v"<<controller.planVersion()<<"diff files"<<controller.diffFiles().size();
    }
private slots:
    void initTestCase() { configureClientUi(*qobject_cast<QGuiApplication*>(QCoreApplication::instance())); }
    void codeIndexMinimalPanel() {
        TaskController tasks; RagController rag; rag.setUseForTasks(false);
        QQmlEngine engine;
        const auto clientRoot=QDir(QFileInfo(QString::fromUtf8(__FILE__)).absolutePath()).absoluteFilePath("..");
        QQmlComponent component(&engine,QUrl::fromLocalFile(clientRoot+"/panels/SettingsPanel.qml"));
        QScopedPointer<QObject> panel(component.createWithInitialProperties({
            {"controller",QVariant::fromValue(&tasks)},{"rag",QVariant::fromValue(&rag)}}));
        QVERIFY2(panel,qPrintable(component.errorString()));
        auto *indexPanel=panel->findChild<QObject*>("codeIndexPanel");
        auto *build=panel->findChild<QObject*>("buildCodeIndexButton");
        auto *remove=panel->findChild<QObject*>("deleteCodeIndexButton");
        auto *toggle=panel->findChild<QObject*>("ragUseSwitch");
        auto *status=panel->findChild<QObject*>("codeIndexStatus");
        QVERIFY(indexPanel); QVERIFY(build); QVERIFY(remove); QVERIFY(toggle); QVERIFY(status);
        QVERIFY(!build->property("enabled").toBool());
        QVERIFY(!remove->property("enabled").toBool());
        QVERIFY(!toggle->property("checked").toBool());
        QCOMPARE(status->property("text").toString(),QString("未启用"));
    }

    void ragQmlPresentationLogicIsExplicitAndSafe() {
        QQmlEngine engine;
        const auto clientRoot=QDir(QFileInfo(QString::fromUtf8(__FILE__)).absolutePath()).absoluteFilePath("..");
        QQmlComponent component(&engine);
        component.setData(R"(
            import QtQml
            import "../panels/RagUi.js" as RagUi
            QtObject {
                property string used: RagUi.taskUsage(true, true, "used")
                property string degraded: RagUi.taskUsage(true, false, "degraded")
                property string disabled: RagUi.taskUsage(false, false, "not_requested")
                property string unknownReason: RagUi.statusDetail("failed", "private_stack_value", true)
            }
        )",QUrl::fromLocalFile(clientRoot+"/tests/RagUiHarness.qml"));
        QScopedPointer<QObject> logic(component.create());
        QVERIFY2(logic,qPrintable(component.errorString()));
        QCOMPARE(logic->property("used").toString(),QString("本次已使用 RAG"));
        QCOMPARE(logic->property("degraded").toString(),QString("本次未使用 RAG（检索失败，已降级）"));
        QCOMPARE(logic->property("disabled").toString(),QString("本次未使用 RAG（发送时开关已关闭）"));
        QVERIFY(!logic->property("unknownReason").toString().contains("private_stack_value"));
    }

    void codeIndexProductionApiLifecycle() {
        if(!qEnvironmentVariable("FORGEPILOT_RESTART_TASK_ID").isEmpty()
                || !qEnvironmentVariable("FORGEPILOT_RESTART_DELETION").isEmpty())
            QSKIP("Restart phase intentionally opens history without the fake RAG runtime.");
        const auto url=qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL");
        const auto repository=qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY");
        if(url.isEmpty() || repository.isEmpty()) QSKIP("Production API fixture is not active.");
        RagController rag; rag.setBaseUrl(QUrl(url)); rag.setRepositoryPath(repository);
        QTRY_VERIFY_WITH_TIMEOUT(rag.reachable(),5000);
        QVERIFY(rag.enabled()); QVERIFY(rag.available());
        rag.buildIndex(); QTRY_VERIFY_WITH_TIMEOUT(!rag.busy() && !rag.running(),15000);
        QCOMPARE(rag.status(),QString("ready")); QVERIFY(rag.fileCount()>0); QVERIFY(rag.chunkCount()>0);
        rag.deleteIndex(); QTRY_VERIFY_WITH_TIMEOUT(!rag.busy(),10000);
        QCOMPARE(rag.status(),QString("empty")); QCOMPARE(rag.fileCount(),0); QCOMPARE(rag.chunkCount(),0);
        QVERIFY(QFileInfo::exists(repository+"/main.cpp"));
    }

    void feedbackQmlStateIsolation() {
        Source source; QJsonObject current=snapshot(); Request pending,stream;
        int posts=0,gets=0;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/events")) { stream=r; Source::stream(r,{},false); }
            else if(r.method=="GET") { ++gets; Source::json(r,current); }
            else { ++posts; pending=r; }
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_VERIFY(c.canFeedback());
        QQmlEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError>& errors) { for(const auto &e:errors) warnings.append(e.toString()); });
        const auto directory=QFileInfo(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")).absolutePath();
        QQmlComponent component(&engine,QUrl::fromLocalFile(directory+"/panels/PlanPanel.qml"));
        QQuickWindow window; window.resize(900,760);
        QScopedPointer<QObject> panel(component.createWithInitialProperties({{"controller",QVariant::fromValue(&c)}}));
        QVERIFY2(panel,qPrintable(component.errorString()));
        auto *root=qobject_cast<QQuickItem*>(panel.data()); QVERIFY(root);
        root->setParentItem(window.contentItem()); root->setSize(QSizeF(900,760)); window.show();
        QVERIFY(QTest::qWaitForWindowExposed(&window));
        auto find=[&](const char *name) { return panel->findChild<QQuickItem*>(name); };
        const auto output=qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT");
        auto capture=[&](const QString &name) {
            if(output.isEmpty()) return true;
            QSignalSpy frame(&window,&QQuickWindow::frameSwapped); window.requestUpdate();
            if(frame.isEmpty() && !frame.wait(1000)) return false;
            return window.grabWindow().save(output+'/'+name+".png");
        };
        auto click=[&](const char *name) {
            auto *item=find(name); if(!item || !item->isEnabled() || !item->isVisible()) return false;
            QSignalSpy frame(&window,&QQuickWindow::frameSwapped); window.requestUpdate();
            if(frame.isEmpty() && !frame.wait(1000)) return false;
            QSignalSpy clicked(item,SIGNAL(clicked()));
            QTest::mouseClick(&window,Qt::LeftButton,Qt::NoModifier,item->mapToScene(QPointF(item->width()/2,item->height()/2)).toPoint());
            return !clicked.isEmpty();
        };
        QTRY_VERIFY(find("planFeedbackButton")->width()>0); QVERIFY(click("planFeedbackButton"));
        auto *input=find("planFeedbackInput"); auto *submit=find("submitPlanFeedbackButton"); QVERIFY(input && submit);
        QTRY_VERIFY(input->isVisible()); QVERIFY(!submit->isEnabled());
        QCOMPARE(input->property("textFormat").toInt(),0); // TextEdit.PlainText
        const QString html="<b>保留公开接口</b> 😀";
        for(const auto &bad:QStringList{QString{},QString(" \n\t"),QString("a")+QChar(0),QString(4001,QChar(0x610f)),QString::fromUtf8("😀").repeated(4001)}) {
            input->setProperty("text",bad); QTRY_VERIFY(!submit->isEnabled()); QCOMPARE(posts,0);
        }
        const auto boundary=QString::fromUtf8("😀").repeated(3999)+QString(QChar(0x610f));
        input->setProperty("text",boundary); QTRY_VERIFY(submit->isEnabled());
        QCOMPARE(find("planFeedbackCount")->property("text").toString(),QString("4000 / 4000 字符"));
        input->setProperty("text",html); QTRY_COMPARE(c.feedbackDraft(),html);
        QVERIFY(capture("feedback-draft"));
        QTRY_VERIFY(submit->width()>0); QVERIFY(click("submitPlanFeedbackButton"));
        QVERIFY(!submit->isEnabled()); QVERIFY(!find("approveButton")->isEnabled()); QVERIFY(!find("rejectButton")->isEnabled());
        QVERIFY(input->property("readOnly").toBool()); QVERIFY(c.canCancel());
        QTRY_COMPARE(posts,1); QCOMPARE(pending.body.value("feedback").toString(),html); QCOMPARE(pending.body.value("plan_version").toInt(),1);
        // Lose the response after the server accepted. GET and then a different client's v2 cannot acknowledge this text.
        current=snapshot(A,"planning",4,1); pending.socket->disconnectFromHost();
        QTRY_COMPARE(gets,2); QTRY_COMPARE(c.state(),QString("planning")); QTRY_VERIFY(!c.busy());
        QVERIFY(input->isVisible()); QCOMPARE(input->property("text").toString(),html); QVERIFY(!submit->isEnabled());
        QVERIFY(find("planFeedbackNotice")->property("text").toString().contains("完整执行记录"));
        QVERIFY(find("feedbackPlanningNotice")->isVisible()); QVERIFY(find("previousPlanNotice")->isVisible());
        QVERIFY(capture("feedback-unconfirmed"));
        QCOMPARE(posts,1);
        current=snapshot(A,"planning",5,2); c.refreshTask(); QTRY_COMPARE(c.planVersion(),2);
        QVERIFY(find("feedbackPlanningNotice")->isVisible()); QVERIFY(!find("previousPlanNotice")->isVisible());
        QVERIFY(!find("approveButton")->isEnabled()); QVERIFY(!submit->isEnabled());
        current=snapshot(A,"waiting_approval",6,2); c.refreshTask(); QTRY_COMPARE(c.planVersion(),2);
        QTRY_COMPARE(c.state(),QString("waiting_approval"));
        QCOMPARE(input->property("text").toString(),html); QVERIFY(!submit->isEnabled());
        QVERIFY(find("planFeedbackNotice")->property("text").toString().contains("版本已变化"));
        QVERIFY(click("closePlanFeedbackButton")); QTRY_VERIFY(!input->isVisible()); QVERIFY(click("planFeedbackButton"));
        QTRY_VERIFY(input->isVisible()); QVERIFY(input->property("text").toString().isEmpty());
        input->setProperty("text",QString("新的意见")); QTRY_VERIFY(submit->isEnabled()); QVERIFY(click("submitPlanFeedbackButton")); QTRY_COMPARE(posts,2);
        QCOMPARE(pending.body.value("plan_version").toInt(),2);
        // A definite send failure keeps the same editor and text for an explicit retry.
        Source::json(pending,{{"error",QJsonObject{{"code","storage_unavailable"},{"message","fixture failure"}}}},503);
        QTRY_VERIFY(!c.busy()); QVERIFY(input->isVisible()); QCOMPARE(input->property("text").toString(),QString("新的意见"));
        QVERIFY(click("submitPlanFeedbackButton")); QTRY_COMPARE(posts,3);
        current=snapshot(A,"planning",7,2); Source::json(pending,current,202);
        QTRY_VERIFY(!input->isVisible()); QVERIFY(c.feedbackDraft().isEmpty());
        QVERIFY(find("feedbackPlanningNotice")->isVisible()); QVERIFY(!find("approveButton")->isEnabled()); QVERIFY(c.canCancel());
        current=snapshot(A,"waiting_approval",9,3); c.refreshTask(); QTRY_COMPARE(c.planVersion(),3);
        QVERIFY(click("planFeedbackButton")); input->setProperty("text",QString("版本三草稿")); QTRY_VERIFY(submit->isEnabled());
        // An unsolicited version update while editing must not rebind the draft.
        current=snapshot(A,"waiting_approval",11,4); c.refreshTask(); QTRY_COMPARE(c.planVersion(),4);
        QCOMPARE(c.feedbackSourceVersion(),3); QCOMPARE(input->property("text").toString(),QString("版本三草稿")); QVERIFY(!submit->isEnabled());
        QVERIFY(click("closePlanFeedbackButton")); QVERIFY(click("planFeedbackButton")); input->setProperty("text",QString("旧任务在途意见"));
        QTRY_VERIFY(submit->isEnabled()); QVERIFY(click("submitPlanFeedbackButton")); QTRY_COMPARE(posts,4);
        const auto late=pending;
        current=snapshot(B); c.selectTask(B); QTRY_VERIFY(c.canFeedback()); QTRY_VERIFY(find("planFeedbackButton")->isVisible());
        QVERIFY(click("planFeedbackButton")); input->setProperty("text",QString("新任务草稿"));
        Source::json(late,snapshot(A,"planning",12,4),202);
        const int priorGets=gets; c.refreshTask(); QTRY_COMPARE(gets,priorGets+1);
        QCOMPARE(input->property("text").toString(),QString("新任务草稿")); QCOMPARE(c.feedbackSourceVersion(),1); QCOMPARE(posts,4);
        // Old SSE history fills the trace without overwriting a newer snapshot/draft.
        QTRY_VERIFY(stream.socket); stream.socket->write(sseEvent(1,"task_state_changed",{{"from","analyzing"},{"state","planning"}},B));
        QTRY_COMPARE(c.eventSequence(),1); QCOMPARE(c.state(),QString("waiting_approval")); QCOMPARE(input->property("text").toString(),QString("新任务草稿"));
        c.viewHistoryTask(B); QTRY_COMPARE(c.state(),QString("waiting_approval"));
        QVERIFY(!find("planFeedbackButton")->isEnabled()); QVERIFY(!find("approveButton")->isEnabled()); QVERIFY(!input->isVisible());
        current=snapshot(A,"planning",3,1); c.selectTask(A); QTRY_COMPARE(c.state(),QString("planning"));
        QVERIFY(!find("feedbackPlanningNotice")->isVisible()); QVERIFY(!find("previousPlanNotice")->isVisible());
        QVERIFY(capture("feedback-initial-planning"));
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
        qInfo("QML feedback: Unicode limits, plain text, lost response, explicit retry, 202 clearing, external version, task isolation, replay and history passed");
    }
    void reflectingPresentation() {
        QQmlEngine engine;
        const auto path=QFileInfo(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")).absolutePath();
        QQmlComponent component(&engine,QUrl::fromLocalFile(path+"/panels/StatusBadge.qml"));
        QScopedPointer<QObject> badge(component.create());
        QVERIFY2(badge,qPrintable(component.errorString()));
        badge->setProperty("status","reflecting");
        QCOMPARE(badge->property("text").toString(),QString("失败原因分析中"));
        const auto reflectingColor=badge->property("color").value<QColor>();
        badge->setProperty("status","failed");
        QCOMPARE(badge->property("text").toString(),QString("失败"));
        QVERIFY(badge->property("color").value<QColor>()!=reflectingColor);
    }
    void qmlRealBackend() {
        if(qEnvironmentVariable("FORGEPILOT_INTEGRATION_SCENARIO")=="joint") { jointLocalEdit(); return; }
        const auto url = qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL");
        const auto repository = qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY");
        const auto qml = qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML");
        QVERIFY2(!url.isEmpty() && !repository.isEmpty() && !qml.isEmpty(), "Run via Test-ClientIntegration.cmd");
        TaskController controller;
        controller.setBaseUrl(QUrl(url));
        ConversationController conversations(&controller);
        QQmlApplicationEngine engine;
        QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError>&errors) {
            for(const auto &error:errors) warnings.append(error.toString());
        });
        engine.setInitialProperties({{"controller",QVariant::fromValue(&controller)},{"conversations",QVariant::fromValue(&conversations)},{"developmentTest",true}});
        engine.load(QUrl::fromLocalFile(qml));
        QVERIFY2(!engine.rootObjects().isEmpty(),qPrintable(warnings.join('\n')));
        auto *window=qobject_cast<QQuickWindow*>(engine.rootObjects()[0]); QVERIFY(window);
        QVERIFY(QTest::qWaitForWindowExposed(window));
        auto find=[&](const char *name) -> QObject* {
            if(auto *object=window->findChild<QObject*>(name)) return object;
            std::function<QObject*(QQuickItem*)> visit=[&](QQuickItem *item)->QObject* {
                if(item->objectName()==name) return item;
                for(auto *child:item->childItems()) if(auto *found=visit(child)) return found;
                return nullptr;
            };
            return visit(window->contentItem());
        };
        auto click=[&](const char *name) {
            if(QByteArray(name)=="refreshTraceButton") {
                window->setProperty("detailsExpanded",true);
                find("detailTabs")->setProperty("currentIndex",1);
            }
            if(QByteArray(name)=="approveButton" || QByteArray(name)=="rejectButton" || QByteArray(name).contains("Feedback")) {
                window->setProperty("detailsExpanded",true);
                find("detailTabs")->setProperty("currentIndex",0);
            }
            auto *item=qobject_cast<QQuickItem*>(find(name));
            if(!item || !item->isEnabled()) return false;
            // Wait for bindings/Repeater layouts before mapping a mouse position.
            QTest::qWait(150);
            if(!item->isVisible()) return false;
            const auto scroll=QByteArray(name)=="refreshTraceButton" ? "traceScroll" : "workspaceScroll";
            auto *flick=qobject_cast<QQuickItem*>(find(scroll)->property("contentItem").value<QObject*>());
            if(flick && flick->isAncestorOf(item)) {
                for(int pass=0;pass<2;++pass) {
                    const auto y=item->mapToItem(flick,QPointF(0,item->height()/2)).y();
                    const auto limit=std::max(0.,flick->property("contentHeight").toDouble()-flick->height());
                    flick->setProperty("contentY",std::clamp(flick->property("contentY").toDouble()+y-flick->height()/2,0.,limit));
                    QTest::qWait(100);
                }
            }
            QSignalSpy clicked(item,SIGNAL(clicked()));
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,item->mapToScene(QPointF(item->width()/2,item->height()/2)).toPoint(),50);
            const bool received=!clicked.isEmpty() || clicked.wait(1000);
            if(!received) qWarning()<<"Mouse click was not delivered:"<<name<<item->mapToScene(QPointF(0,0));
            return received;
        };
        const auto output=qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT");
        QTRY_COMPARE(conversations.serverStatus(),"connected");
        QTRY_VERIFY(!conversations.historyLoading());
        const auto restored=qEnvironmentVariable("FORGEPILOT_RESTART_TASK_ID");
        if(!restored.isEmpty()) {
            const auto restoredConversation=qEnvironmentVariable("FORGEPILOT_RESTART_CONVERSATION_ID");
            QVERIFY(!restoredConversation.isEmpty());
            conversations.selectConversation(restoredConversation);
            QTRY_VERIFY(!conversations.loading());
            QTRY_COMPARE(controller.tasks().size(),4); QCOMPARE(controller.historyTotal(),4);
            controller.viewHistoryTask(restored); QTRY_COMPARE(controller.state(),"completed");
            QTRY_COMPARE(controller.connectionState(),"ended");
            QVERIFY(controller.historyReadOnly()); QVERIFY(controller.snapshot().value("read_only").toBool());
            QVERIFY(!find("planFeedbackButton")->property("enabled").toBool());
            QVERIFY(!find("approveButton")->property("enabled").toBool()); QVERIFY(!find("sendMessageButton")->property("enabled").toBool());
            QVERIFY(!find("executionViewButton")->property("visible").toBool());
            QCOMPARE(controller.diffFiles().size(),2); QVERIFY(controller.buildReports().size()==1);
            QVERIFY(click("refreshTraceButton")); QTRY_VERIFY(controller.storedTrace()->count()>0);
            controller.loadTraceDetail(controller.storedTrace()->get(0).value("sequence").toLongLong());
            QTRY_VERIFY(!controller.traceDetailText().isEmpty());
            QVERIFY(controller.traceDetailText().contains("task_created"));
            if(!output.isEmpty()) { QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/restarted-history.png")); }
            QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
            qInfo().noquote()<<"QML persisted history after backend restart:"<<restored<<"trace rows="<<controller.storedTrace()->count();
            return;
        }
        QTest::qWait(150);
        QVERIFY(!window->property("detailsExpanded").toBool());
        QVERIFY(find("conversationView")->property("height").toDouble()>350);
        for(const auto *name : {"conversationPrompt"}) {
            auto *field=find(name); QVERIFY(field);
            const auto foreground=field->property("color").value<QColor>();
            const auto background=field->property("background").value<QObject*>()->property("color").value<QColor>();
            QVERIFY(foreground.lightnessF()<0.3); QVERIFY(background.lightnessF()>0.8);
        }
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/empty.png"));
        auto *settingsButton=qobject_cast<QQuickItem*>(find("settingsButton"));
        const auto buttonBackground=settingsButton->property("background").value<QObject*>();
        const auto idleColor=buttonBackground->property("color").value<QColor>();
        QTest::mouseMove(window,settingsButton->mapToScene(QPointF(10,10)).toPoint());
        QTRY_VERIFY(settingsButton->property("hovered").toBool());
        QVERIFY(buttonBackground->property("color").value<QColor>()!=idleColor);
        QVERIFY(click("settingsButton")); QTest::qWait(100);
        QVERIFY(find("codeIndexPanel"));
        QVERIFY(find("buildCodeIndexButton"));
        QVERIFY(!find("buildCodeIndexButton")->property("enabled").toBool());
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/settings.png"));
        QTest::keyClick(window,Qt::Key_Escape);
        // Exercise Qt's folder dialog lifecycle and the product's accepted binding with the test fallback dialog.
        QVERIFY(click("taskModeButton"));
        auto *dialog=find("conversationFolderDialog"); QVERIFY(dialog);
        const auto option=dialog->metaObject()->property(dialog->metaObject()->indexOfProperty("options")).enumerator();
        const int nonNative=option.keyToValue("DontUseNativeDialog"); QVERIFY(nonNative>=0);
        QVERIFY(dialog->setProperty("options",nonNative));
        QVERIFY(QMetaObject::invokeMethod(dialog,"open")); QTest::qWait(100);
        bool folderCaptured=false;
        for(auto *candidate:QGuiApplication::topLevelWindows()) {
            auto *popup=qobject_cast<QQuickWindow*>(candidate);
            if(popup && popup!=window && popup->isVisible() && popup->transientParent()==window) {
                QVERIFY(QTest::qWaitForWindowExposed(popup)); QTest::qWait(150);
                if(!output.isEmpty()) QVERIFY(popup->grabWindow().save(output+"/folder-dialog.png"));
                folderCaptured=true;
            }
        }
        // Qt may render the fallback dialog as an item instead of a separate window.
        if(!folderCaptured && !output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/folder-dialog.png"));
        QVERIFY(dialog->setProperty("selectedFolder",QUrl::fromLocalFile(repository)));
        QCOMPARE(dialog->property("selectedFolder").toUrl(),QUrl::fromLocalFile(repository));
        QVERIFY(QMetaObject::invokeMethod(dialog,"accept"));
        QTRY_COMPARE(controller.repositoryPath(),QUrl::fromLocalFile(repository).toLocalFile());
        QTRY_VERIFY(!conversations.busy() && !conversations.loading());
        QCOMPARE(conversations.mode(),QString("task"));
        const auto firstConversation=conversations.conversationId();
        QVERIFY(!firstConversation.isEmpty());
        if(!output.isEmpty()) {
            QFile savedConversation(output+"/backend-conversation-id.txt");
            QVERIFY(savedConversation.open(QIODevice::WriteOnly));
            QCOMPARE(savedConversation.write(firstConversation.toUtf8()),firstConversation.toUtf8().size());
        }
        QTRY_VERIFY(!controller.repository()->loading()); QVERIFY(controller.repository()->error().isEmpty());
        QCOMPARE(controller.repository()->count(),3); // Only source, CMakeLists and user's preexisting file.
        QVERIFY(find("conversationPrompt")); find("conversationPrompt")->setProperty("text",QString("修复 main.cpp 并验证"));
        QVERIFY(QMetaObject::invokeMethod(find("conversationPromptMenu"),"open")); QTest::qWait(100);
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/input-menu.png"));
        QVERIFY(QMetaObject::invokeMethod(find("conversationPromptMenu"),"close"));
        QSignalSpy builds(&controller,&TaskController::buildFinished), tests(&controller,&TaskController::testFinished);
        QVERIFY(submitLegacyTask(controller,conversations,find("conversationPrompt")->property("text").toString()));
        QTRY_VERIFY_WITH_TIMEOUT(controller.canApprove(),15000);
        QCOMPARE(controller.planVersion(),1);
        QTRY_VERIFY(find("planView"));
        QVERIFY(find("planView")->property("text").toString().contains("main.cpp"));
        QVERIFY(find("planView")->property("text").toString().contains("result.h"));
        QVERIFY(window->property("detailsExpanded").toBool());
        QCOMPARE(find("approveButton")->property("text").toString(),QString("批准并执行"));
        QCOMPARE(find("rejectButton")->property("text").toString(),QString("拒绝方案"));
        auto *approveItem=qobject_cast<QQuickItem*>(find("approveButton"));
        QVERIFY(approveItem->mapToScene(QPointF(0,approveItem->height())).y()<window->height());
        QTest::qWait(100);
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/waiting-approval.png"));
        auto *planScroll=qobject_cast<QQuickItem*>(find("workspaceScroll"));
        auto *planBar=planScroll->findChild<QQuickItem*>("verticalScrollBar"); QVERIFY(planBar);
        QVERIFY(qAbs(planBar->x()+planBar->width()-planScroll->width())<1);
        QVERIFY(planBar->height()>100);
        auto *planFlick=planScroll->property("contentItem").value<QObject*>();
        // The full-height work panel can show a short plan without scrolling.
        if(planFlick->property("contentHeight").toDouble()>planFlick->property("height").toDouble()+1) {
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,
                planBar->mapToScene(QPointF(planBar->width()/2,planBar->height()-10)).toPoint());
            QTRY_VERIFY(planFlick->property("contentY").toDouble()>0);
        } else {
            QVERIFY(qAbs(planFlick->property("contentY").toDouble())<1);
        }
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/waiting-approval-details.png"));
        if(qEnvironmentVariable("FORGEPILOT_INTEGRATION_SCENARIO")=="feedback") {
            const auto task=controller.taskId();
            QFile source(repository+"/main.cpp"); QVERIFY(source.open(QIODevice::ReadOnly)); const auto original=source.readAll(); source.close();
            QVERIFY(click("planFeedbackButton"));
            auto *input=find("planFeedbackInput"); QVERIFY(input);
            input->setProperty("text",QString("保留公开接口，只调整实现；补充边界验证 😀"));
            QVERIFY(click("submitPlanFeedbackButton"));
            QTRY_COMPARE(controller.state(),QString("planning")); QTRY_VERIFY(!controller.feedbackOpen());
            QCOMPARE(controller.taskId(),task); QCOMPARE(controller.planVersion(),1);
            QVERIFY(find("feedbackPlanningNotice")->property("visible").toBool());
            QVERIFY(find("previousPlanNotice")->property("visible").toBool());
            QVERIFY(!find("approveButton")->property("enabled").toBool()); QVERIFY(!find("rejectButton")->property("enabled").toBool());
            QVERIFY(!find("planFeedbackButton")->property("enabled").toBool()); QVERIFY(find("stopTaskButton")->property("enabled").toBool());
            QVERIFY(input->property("text").toString().isEmpty());
            if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/feedback-planning.png"));
            // Release the explicitly selected offline scenario only after observing the real UI.
            QNetworkAccessManager network;
            auto *release=network.post(QNetworkRequest(QUrl(url+"/_fixture/release-feedback")),QByteArray{});
            QSignalSpy released(release,&QNetworkReply::finished); if(!release->isFinished()) QVERIFY(released.wait(5000));
            QCOMPARE(release->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt(),200); release->deleteLater();
            QTRY_VERIFY_WITH_TIMEOUT(controller.canApprove() && controller.planVersion()==2,10000);
            QCOMPARE(controller.taskId(),task); QVERIFY(source.open(QIODevice::ReadOnly)); QCOMPARE(source.readAll(),original); source.close();
            QVERIFY(find("approveButton")->property("enabled").toBool()); QVERIFY(!find("feedbackPlanningNotice")->property("visible").toBool());
            QVERIFY(click("refreshTraceButton")); QTRY_VERIFY(!controller.traceLoading());
            bool feedbackFound=false;
            for(int i=0;i<controller.storedTrace()->count();++i) {
                const auto row=controller.storedTrace()->get(i);
                if(row.value("kind").toString()=="plan_feedback") {
                    feedbackFound=true; controller.loadTraceDetail(row.value("sequence").toLongLong()); break;
                }
            }
            QVERIFY(feedbackFound); QTRY_VERIFY(controller.traceDetailText().contains("保留公开接口"));
            qInfo("REAL QML feedback: v1 -> 202 planning -> v2, old source bytes unchanged, stop enabled, feedback in stored trace");
        }
        QVERIFY(click("approveButton"));
        QTRY_VERIFY(!find("approveButton")->property("enabled").toBool());
        QTRY_VERIFY(controller.state()!="waiting_approval");
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/running.png"));
        QTRY_COMPARE_WITH_TIMEOUT(controller.state(),"completed",60000);
        QTRY_VERIFY_WITH_TIMEOUT(!controller.snapshot().value("finished_at").toString().isEmpty(),10000);
        QTRY_COMPARE(controller.connectionState(),"ended");
        QCOMPARE(builds.size(),2); QCOMPARE(tests.size(),1); QVERIFY(tests[0][0].toBool());
        QTRY_VERIFY(!conversations.active() && !conversations.busy() && !conversations.loading());
        QTRY_COMPARE(conversations.messages()->count(),2);
        QTRY_COMPARE(find("conversationMessages")->property("count").toInt(),2);
        QVERIFY(conversations.messages()->get(1).value("content").toString().contains("任务已完成"));
        QCOMPARE(controller.snapshot().value("outcome").toString(),QString("modification_validated"));
        QTRY_COMPARE(controller.diffFiles().size(),2); QCOMPARE(controller.buildReports().size(),1);
        const auto stages=controller.buildReports()[0].toMap().value("stages").toList();
        for(const auto &stage:stages) { QCOMPARE(stage.toMap().value("status").toString(),"succeeded"); QCOMPARE(stage.toMap().value("exitCode").toInt(),0); }
        QVERIFY(controller.traces()->count()>=5);
        QVERIFY(find("diff_main.cpp")); QVERIFY(!find("diff_main.cppLoader")->property("active").toBool());
        find("diff_main.cpp")->setProperty("expanded",true);
        QTRY_VERIFY(find("diff_main.cppLoader")->property("active").toBool());
        QVERIFY(find("diff_main.cpp")->property("text").toString().contains(" // user note"));
        QVERIFY(!find("diff_main.cpp")->property("text").toString().contains("+// user note"));
        const auto completed=controller.taskId();
        const int count=controller.eventSequence();
        controller.reconnectEvents(); QTRY_COMPARE(controller.connectionState(),"ended");
        QCOMPARE(controller.eventSequence(),count);
        controller.refreshTasks(); QTRY_COMPARE(controller.tasks().size(),1);
        QTest::qWait(100);
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/completed.png"));
        find("detailTabs")->setProperty("currentIndex",2);
        find("resultTabs")->setProperty("currentIndex",1);
        QVERIFY(find("log_cmake_build")); QVERIFY(!find("log_cmake_buildLoader")->property("active").toBool());
        find("log_cmake_build")->setProperty("expanded",true);
        QTRY_VERIFY(find("log_cmake_buildLoader")->property("active").toBool());
        QVERIFY(find("log_cmake_build")->property("text").toString().contains("MSBuild"));
        window->resize(1000,720);
        QTRY_COMPARE(window->width(),1000); QTRY_COMPARE(window->height(),720);
        // The native backing surface may resize after the logical QWindow geometry.
        QTRY_COMPARE(window->grabWindow().width(),qRound(1000*window->devicePixelRatio()));
        auto *chatPanel=qobject_cast<QQuickItem*>(find("conversationView"));
        auto *submitButton=qobject_cast<QQuickItem*>(find("sendMessageButton"));
        QVERIFY(submitButton->mapToItem(chatPanel,QPointF(0,submitButton->height())).y()<=chatPanel->height()-8);
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/small-build.png"));
        find("resultTabs")->setProperty("currentIndex",2); QTest::qWait(80);
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/small-test.png"));
        window->resize(1440,960);
        QTRY_COMPARE(window->grabWindow().width(),qRound(1440*window->devicePixelRatio()));
        find("resultTabs")->setProperty("currentIndex",0);
        qInfo().noquote()<<"QML completed:"<<completed<<"events="<<count<<"builds="<<builds.size()<<"tests="<<tests.size();
        find("conversationPrompt")->setProperty("text","等待审批后取消");
        QVERIFY(submitLegacyTask(controller,conversations,find("conversationPrompt")->property("text").toString()));
        QTRY_VERIFY_WITH_TIMEOUT(controller.canApprove(),15000);
        const auto waiting=controller.taskId();
        controller.viewHistoryTask(waiting); QTRY_COMPARE(controller.state(),"waiting_approval");
        QVERIFY(controller.historyReadOnly()); QVERIFY(!find("approveButton")->property("enabled").toBool());
        QVERIFY(!find("stopTaskButton")->property("enabled").toBool()); QVERIFY(!find("sendMessageButton")->property("enabled").toBool());
        QVERIFY(click("executionViewButton")); QTRY_VERIFY(controller.canCancel());
        QVERIFY(click("stopTaskButton")); QTRY_COMPARE_WITH_TIMEOUT(controller.state(),"cancelled",10000);
        QTRY_VERIFY(!conversations.active() && !conversations.busy() && !conversations.loading());
        QTRY_COMPARE(conversations.messages()->count(),4);
        QVERIFY(conversations.messages()->get(3).value("content").toString().contains("任务已停止"));
        qInfo().noquote()<<"QML cancelled:"<<controller.taskId()<<controller.snapshot();
        if(!output.isEmpty()) { QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/cancelled.png")); }
        find("conversationPrompt")->setProperty("text","拒绝本轮方案");
        QVERIFY(submitLegacyTask(controller,conversations,find("conversationPrompt")->property("text").toString()));
        QTRY_VERIFY_WITH_TIMEOUT(controller.canApprove(),15000);
        QVERIFY(click("rejectButton")); QTRY_COMPARE_WITH_TIMEOUT(controller.state(),"cancelled",10000);
        QTRY_COMPARE(controller.snapshot().value("outcome").toString(),"rejected");
        QTRY_VERIFY(!conversations.active() && !conversations.busy() && !conversations.loading());
        find("conversationPrompt")->setProperty("text","fixture failure");
        QVERIFY(submitLegacyTask(controller,conversations,find("conversationPrompt")->property("text").toString()));
        QTRY_COMPARE_WITH_TIMEOUT(controller.state(),"failed",15000);
        QTRY_VERIFY(!conversations.active() && !conversations.busy() && !conversations.loading());
        QTRY_COMPARE(conversations.messages()->count(),8);
        QVERIFY(conversations.messages()->get(7).value("content").toString().contains("任务失败"));
        QTRY_COMPARE(controller.connectionState(),"ended");
        if(!output.isEmpty()) { QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/failed.png")); }
        qInfo().noquote()<<"QML failed:"<<controller.taskId()<<controller.snapshot();
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
        // Loading another completed task reconstructs its traces without stale failed callbacks.
        controller.viewHistoryTask(completed); QTRY_COMPARE(controller.state(),"completed");
        QTRY_COMPARE(controller.eventSequence(),count);
        QVERIFY(controller.historyReadOnly()); QCOMPARE(controller.diffFiles().size(),2);
        if(!output.isEmpty()) { QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/history.png")); }
        QVERIFY(click("newTaskButton"));
        QTRY_VERIFY(conversations.conversationId()!=firstConversation && !conversations.busy() && !conversations.loading());
        QVERIFY(controller.taskId().isEmpty());
        QVERIFY(!controller.configureBackend("https://remote.example"));
        QVERIFY(find("errorMessage")->property("text").toString().contains("HTTP"));
        controller.clearError();
        // Legacy conversation fixtures still report repository validation failures.
        conversations.chooseRepository(QUrl::fromLocalFile(repository+"/missing"));
        QTRY_VERIFY(!conversations.busy() && !conversations.loading());
        QVERIFY(click("taskModeButton"));
        find("conversationPrompt")->setProperty("text","开发测试：不存在的仓库");
        QVERIFY(submitLegacyTask(controller,conversations,find("conversationPrompt")->property("text").toString()));
        QTRY_COMPARE_WITH_TIMEOUT(controller.state(),"failed",10000);
        QVERIFY(controller.snapshot().value("error").toString().contains("repository"));
        QTRY_VERIFY(!conversations.active() && !conversations.busy() && !conversations.loading());
        QTRY_COMPARE(conversations.messages()->count(),2);
        QVERIFY(conversations.messages()->get(1).value("content").toString().contains("任务失败"));
        // A missing historical task exercises the current UI's HTTP error detail dialog.
        controller.selectTask("00000000-0000-4000-8000-000000000987");
        QTRY_VERIFY_WITH_TIMEOUT(controller.error().contains("unknown_task"),10000);
        QVERIFY(find("errorMessage")->property("visible").toBool()); QVERIFY(!controller.busy());
        if(!output.isEmpty()) { QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/error.png")); }
        QVERIFY(click("errorDetailsButton")); QTest::qWait(100);
        if(!output.isEmpty()) QVERIFY(window->grabWindow().save(output+"/error-dialog.png"));
        QVERIFY(QMetaObject::invokeMethod(find("errorDialog"),"close"));
        QTcpServer unusedPort; QVERIFY(unusedPort.listen(QHostAddress::LocalHost));
        const auto disconnectedUrl=QString("http://127.0.0.1:%1").arg(unusedPort.serverPort()); unusedPort.close();
        QVERIFY(controller.configureBackend(disconnectedUrl));
        QTRY_COMPARE(conversations.serverStatus(),"error");
        QTRY_VERIFY(conversations.error().contains("后端"));
        QVERIFY(!conversations.retryPending());
        if(!output.isEmpty()) { QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/disconnected.png")); }
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
    }
};
QTEST_MAIN(BackendTest)
#include "tst_backend.moc"
