#include "ConversationController.h"
#include "ClientUi.h"
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQuickWindow>
#include <QQuickItem>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QUuid>
#include <QtTest>
#include <functional>

namespace {
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

class DeletionBackendTest:public QObject {
    Q_OBJECT
private slots:
    void realDeletionUi() {
        configureClientUi(*qobject_cast<QGuiApplication*>(QCoreApplication::instance()));
        BackendConnection connection;
        TaskController tasks(connection); tasks.setBaseUrl(QUrl(qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL")));
        ConversationController c(connection,&tasks);
        QQmlApplicationEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError>&errors){ for(const auto &error:errors) warnings.append(error.toString()); });
        engine.setInitialProperties({{"backendConnection",QVariant::fromValue(&connection)},{"controller",QVariant::fromValue(&tasks)},{"conversations",QVariant::fromValue(&c)},{"developmentTest",true}});
        engine.load(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")));
        QVERIFY2(!engine.rootObjects().isEmpty(),qPrintable(warnings.join('\n')));
        auto *window=qobject_cast<QQuickWindow*>(engine.rootObjects()[0]); QVERIFY(window); QVERIFY(QTest::qWaitForWindowExposed(window));
        auto find=[&](const QString &name)->QObject* {
            if(auto *object=window->findChild<QObject*>(name)) return object;
            std::function<QObject*(QQuickItem*)> visit=[&](QQuickItem *item)->QObject* {
                if(item->objectName()==name) return item;
                for(auto *child:item->childItems()) if(auto *found=visit(child)) return found;
                return nullptr;
            }; return visit(window->contentItem());
        };
        auto ready=[&](const QString &name){
            auto *item=qobject_cast<QQuickItem*>(find(name));
            return item && item->isEnabled() && item->isVisible();
        };
        auto click=[&](const QString &name){
            auto *item=qobject_cast<QQuickItem*>(find(name));
            if(!item || !item->isEnabled() || !item->isVisible()) return false;
            QTest::qWait(60);
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,item->mapToScene(QPointF(item->width()/2,item->height()/2)).toPoint());
            return true;
        };
        const auto output=qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT");
        auto capture=[&](const QString &name){ QTest::qWait(100); return window->grabWindow().save(output+"/deletion-"+name+".png"); };
        auto *prompt=find("conversationPrompt"); QVERIFY(prompt);
        QTRY_VERIFY(!c.historyLoading());
        const auto restart=qEnvironmentVariable("FORGEPILOT_RESTART_DELETION");
        if(!restart.isEmpty()) {
            QTRY_COMPARE(c.conversationId(),restart); QTRY_VERIFY(!c.loading());
            QCOMPARE(c.history().size(),1); QCOMPARE(c.historyTotal(),1); QCOMPARE(c.messages()->count(),2);
            QCOMPARE(c.title(),QString("保留的对话")); QVERIFY(!c.active()); QVERIFY(tasks.taskId().isEmpty());
            tasks.refreshTasks(); QTRY_VERIFY(!tasks.historyLoading()); QCOMPARE(tasks.tasks().size(),0);
            tasks.reconnectEvents(); QTest::qWait(150); QVERIFY(tasks.taskId().isEmpty());
            QVERIFY(capture("restart"));
        } else {
            QVERIFY(c.history().isEmpty()); QVERIFY(c.conversationId().isEmpty());
            prompt->setProperty("text","其他对话的消息必须保留"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),2,10000); QTRY_VERIFY(!c.active());
            c.renameConversation("保留的对话"); QTRY_COMPARE(c.title(),QString("保留的对话")); const auto keep=c.conversationId();
            const auto keepReply=c.messages()->get(1).value("content");
            QVERIFY(click("newTaskButton")); QTRY_VERIFY(c.conversationId()!=keep); QTRY_VERIFY(!c.loading());
            const auto victim=c.conversationId();
            c.chooseRepository(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY")));
            QTRY_VERIFY(!c.repositoryPath().isEmpty()); QTRY_VERIFY(!c.busy());
            QVERIFY(click("taskModeButton")); prompt->setProperty("text","修复后删除记录，项目文件保留");
            QVERIFY(submitLegacyTask(tasks,c,prompt->property("text").toString()));
            QTRY_VERIFY_WITH_TIMEOUT(tasks.canApprove(),15000); QVERIFY(click("approveButton"));
            QTRY_COMPARE_WITH_TIMEOUT(tasks.state(),"completed",60000); QTRY_VERIFY(!c.active());
            QVERIFY(tasks.snapshot().value("build_result").toMap().value("tests").toMap().value("passed").toInt()==1);
            c.selectConversation(keep); QTRY_COMPARE(c.messages()->count(),2); QTRY_VERIFY(!c.historyLoading());
            QTRY_VERIFY(find("conversationMore_"+victim)); QVERIFY(click("conversationMore_"+victim));
            QTRY_VERIFY(ready("renameConversation_"+victim)); QVERIFY(click("renameConversation_"+victim)); QTRY_VERIFY(ready("renameConversationInput"));
            find("renameConversationInput")->setProperty("text","有执行记录的待删除对话"); QVERIFY(click("saveConversationNameButton"));
            QTRY_COMPARE(c.history().first().toMap().value("title").toString(),QString("有执行记录的待删除对话"));
            QTRY_VERIFY(!find("renameConversationDialog")->property("visible").toBool());
            QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.conversationId(),keep);
            QTRY_VERIFY(ready("conversationMore_"+victim)); QVERIFY(click("conversationMore_"+victim)); QVERIFY(capture("menu"));
            QTRY_VERIFY(ready("deleteConversation_"+victim)); QVERIFY(click("deleteConversation_"+victim)); QTRY_VERIFY(!c.deleteLoading());
            auto *dialog=find("deleteConversationDialog"); QVERIFY(dialog); QCOMPARE(dialog->property("title").toString(),QString("删除这段对话？"));
            QCOMPARE(find("confirmDeleteConversationButton")->property("text").toString(),QString("删除对话"));
            QVERIFY(capture("confirm")); QVERIFY(click("keepConversationButton")); QTRY_VERIFY(c.deleteTarget().isEmpty());
            QCOMPARE(c.historyTotal(),2); QCOMPARE(c.conversationId(),keep);
            QVERIFY(click("conversationMore_"+victim)); QTRY_VERIFY(ready("deleteConversation_"+victim)); QVERIFY(click("deleteConversation_"+victim)); QTRY_VERIFY(!c.deleteLoading());
            QVERIFY(click("confirmDeleteConversationButton")); QTRY_VERIFY(!c.deleting()); QTRY_COMPARE(c.historyTotal(),1);
            QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.conversationId(),keep); QCOMPARE(c.messages()->get(1).value("content"),keepReply);
            QVERIFY(tasks.taskId().isEmpty()); QVERIFY(capture("non-current-deleted"));
            QVERIFY(click("newTaskButton")); QTRY_VERIFY(c.conversationId()!=keep); QTRY_VERIFY(!c.loading()); const auto answering=c.conversationId();
            prompt->setProperty("text","慢回答 fixture"); QVERIFY(click("sendMessageButton")); QTRY_VERIFY(c.active()); QTRY_VERIFY(!c.historyLoading());
            QTRY_COMPARE(c.history().first().toMap().value("title").toString(),QString("慢回答 fixture"));
            QTRY_VERIFY(ready("conversationMore_"+answering)); QVERIFY(click("conversationMore_"+answering)); QTRY_VERIFY(ready("deleteConversation_"+answering)); QVERIFY(click("deleteConversation_"+answering)); QTRY_VERIFY(!c.deleteLoading());
            QCOMPARE(find("confirmDeleteConversationButton")->property("text").toString(),QString("停止任务并删除")); QVERIFY(capture("active-confirm"));
            QVERIFY(click("confirmDeleteConversationButton")); QVERIFY(c.deleting()); QVERIFY(!find("keepConversationButton")->property("enabled").toBool());
            QVERIFY(capture("cleanup")); QTRY_VERIFY_WITH_TIMEOUT(!c.deleting(),10000); QTRY_COMPARE(c.conversationId(),keep); QTRY_COMPARE(c.messages()->count(),2);
            QVERIFY(click("newTaskButton")); QTRY_VERIFY(c.conversationId()!=keep); QTRY_VERIFY(!c.loading()); const auto waiting=c.conversationId();
            c.chooseRepository(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY")));
            QTRY_VERIFY(!c.repositoryPath().isEmpty()); QTRY_VERIFY(!c.busy()); QVERIFY(click("taskModeButton"));
            prompt->setProperty("text","删除等待审批的任务");
            QVERIFY(submitLegacyTask(tasks,c,prompt->property("text").toString())); QTRY_VERIFY_WITH_TIMEOUT(tasks.canApprove(),15000);
            QTRY_COMPARE(c.history().first().toMap().value("title").toString(),QString("删除等待审批的任务"));
            QTRY_VERIFY(!c.historyLoading()); QTRY_VERIFY(ready("conversationMore_"+waiting)); QVERIFY(click("conversationMore_"+waiting)); QTRY_VERIFY(ready("deleteConversation_"+waiting)); QVERIFY(click("deleteConversation_"+waiting)); QTRY_VERIFY(!c.deleteLoading());
            QVERIFY(c.deleteActive()); QVERIFY(capture("approval-confirm")); QVERIFY(click("confirmDeleteConversationButton"));
            QTRY_VERIFY(!c.deleting()); QTRY_COMPARE(c.conversationId(),keep); QTRY_VERIFY(!c.historyLoading()); QTRY_COMPARE(c.messages()->count(),2);
            QCOMPARE(c.historyTotal(),1); QCOMPARE(c.messages()->get(1).value("content"),keepReply);
            QVERIFY(tasks.taskId().isEmpty()); QVERIFY(!tasks.canApprove()); QCOMPARE(tasks.traces()->count(),0); QCOMPARE(tasks.tasks().size(),0);
            tasks.reconnectEvents(); QTest::qWait(200); QCOMPARE(c.historyTotal(),1); QVERIFY(capture("current-deleted"));
            QFile marker(output+"/deletion-keep-id.txt"); QVERIFY(marker.open(QIODevice::WriteOnly)); marker.write(keep.toUtf8());
        }
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
    }
};
QTEST_MAIN(DeletionBackendTest)
#include "tst_deletion_backend.moc"
