#include "ConversationController.h"
#include "ClientUi.h"
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQuickWindow>
#include <QQuickItem>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <QTcpServer>
#include <QTcpSocket>
#include <QtTest>
#include <functional>

class ConversationBackendTest:public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { configureClientUi(*qobject_cast<QGuiApplication*>(QCoreApplication::instance())); }
    void longErrorsKeepConversationControlsReachable() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost));
        const QString detail=QString("连接诊断 <原文>：请检查后端状态。\n").repeated(80);
        connect(&server,&QTcpServer::newConnection,this,[&] {
            while(server.hasPendingConnections()) {
                auto *socket=server.nextPendingConnection();
                connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
                connect(socket,&QTcpSocket::readyRead,socket,[socket,detail] {
                    socket->readAll();
                    if(socket->property("answered").toBool()) return;
                    socket->setProperty("answered",true);
                    const auto data=QJsonDocument(QJsonObject{{"error",QJsonObject{{"code","server_stopping"},{"message",detail}}}}).toJson(QJsonDocument::Compact);
                    socket->write("HTTP/1.1 503 Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(data.size())+"\r\n\r\n"+data);
                    socket->disconnectFromHost();
                });
            }
        });
        TaskController tasks; tasks.setBaseUrl(QUrl(QString("http://127.0.0.1:%1").arg(server.serverPort())));
        ConversationController c(&tasks); QQmlApplicationEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError> &errors){for(const auto &e:errors) warnings.append(e.toString());});
        engine.setInitialProperties({{"controller",QVariant::fromValue(&tasks)},{"conversations",QVariant::fromValue(&c)},{"width",1000},{"height",720}});
        engine.load(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")));
        QVERIFY(!engine.rootObjects().isEmpty());
        auto *window=qobject_cast<QQuickWindow*>(engine.rootObjects()[0]); QVERIFY(window); QVERIFY(QTest::qWaitForWindowExposed(window));
        QTRY_VERIFY(!c.error().isEmpty()); QTest::qWait(100);
        auto *send=window->findChild<QQuickItem*>("sendMessageButton"); QVERIFY(send);
        const auto output=qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT");
        QVERIFY(window->grabWindow().save(output+"/conversation-long-error.png"));
        const auto rect=send->mapRectToScene(QRectF(0,0,send->width(),send->height()));
        QVERIFY2(rect.top()>=0 && rect.bottom()<window->height() && rect.right()<=window->width(),"Long diagnostics must not push the send button outside the window");
        auto *details=window->findChild<QQuickItem*>("conversationErrorDetailsButton"); QVERIFY(details); QVERIFY(details->isVisible());
        QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,details->mapToScene(QPointF(details->width()/2,details->height()/2)).toPoint());
        auto *dialog=window->findChild<QObject*>("conversationErrorDialog"); QVERIFY(dialog); QTRY_VERIFY(dialog->property("visible").toBool());
        auto *content=window->findChild<QObject*>("conversationErrorContent"); QVERIFY(content); QCOMPARE(content->property("text").toString(),c.error());
        QVERIFY(content->property("text").toString().contains(detail));
        QTest::qWait(100); QVERIFY(window->grabWindow().save(output+"/conversation-long-error-details.png"));
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
    }
    void realConversationUi() {
        TaskController tasks; tasks.setBaseUrl(QUrl(qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL")));
        // Legacy tasks are historical fixtures; desktop submissions now create Missions.
        AgentApiClient legacy; QVERIFY(legacy.setBaseUrl(tasks.baseUrl()));
        auto createLegacyTask=[&](const QString &text) {
            QSignalSpy replies(&legacy,&AgentApiClient::responseReceived);
            legacy.createTask(qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY"),text);
            if(replies.isEmpty() && !replies.wait(15000)) return QString{};
            return replies.last()[3].toJsonObject().value("task_id").toString();
        };
        ConversationController c(&tasks);
        QQmlApplicationEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError>&errors){ for(const auto &error:errors) warnings.append(error.toString()); });
        engine.setInitialProperties({{"controller",QVariant::fromValue(&tasks)},{"conversations",QVariant::fromValue(&c)},{"developmentTest",true}});
        engine.load(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")));
        QVERIFY2(!engine.rootObjects().isEmpty(),qPrintable(warnings.join('\n')));
        auto *window=qobject_cast<QQuickWindow*>(engine.rootObjects()[0]); QVERIFY(window); QVERIFY(QTest::qWaitForWindowExposed(window));
        auto find=[&](const char *name)->QObject* {
            if(auto *object=window->findChild<QObject*>(name)) return object;
            std::function<QObject*(QQuickItem*)> visit=[&](QQuickItem *item)->QObject* {
                if(item->objectName()==name) return item;
                for(auto *child:item->childItems()) if(auto *found=visit(child)) return found;
                return nullptr;
            }; return visit(window->contentItem());
        };
        auto click=[&](const char *name){
            auto *item=qobject_cast<QQuickItem*>(find(name));
            if(!item || !item->isEnabled() || !item->isVisible()) return false;
            QTest::qWait(60);
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,item->mapToScene(QPointF(item->width()/2,item->height()/2)).toPoint());
            return true;
        };
        const auto output=qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT");
        auto capture=[&](const QString &name){ QTest::qWait(100); return window->grabWindow().save(output+"/conversation-"+name+".png"); };
        auto *prompt=find("conversationPrompt"); QVERIFY(prompt);
        QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.mode(),"chat");
        auto *toolMode=find("chatToolMode"); QVERIFY(toolMode); QCOMPARE(c.toolMode(),QString("auto"));
        toolMode->setProperty("currentIndex",1);
        QVERIFY(QMetaObject::invokeMethod(toolMode,"activated",Q_ARG(int,1))); QCOMPARE(c.toolMode(),QString("fast"));
        toolMode->setProperty("currentIndex",0);
        QVERIFY(QMetaObject::invokeMethod(toolMode,"activated",Q_ARG(int,0))); QCOMPARE(c.toolMode(),QString("auto"));
        const auto restart=qEnvironmentVariable("FORGEPILOT_RESTART_CONVERSATION_ID");
        if(!restart.isEmpty()) {
            c.selectConversation(restart); QTRY_VERIFY(!c.loading());
            QVERIFY(c.messages()->count()>=4); QVERIFY(!c.active()); QVERIFY(tasks.taskId().isEmpty());
            prompt->setProperty("text","重启后还记得吗？"); QVERIFY(click("sendMessageButton"));
            QTRY_VERIFY(c.active()); QTRY_VERIFY_WITH_TIMEOUT(!c.active(),10000);
            QTRY_VERIFY(!c.loading());
            QVERIFY(c.messages()->get(c.messages()->count()-1).value("content").toString().contains("青松"));
            tasks.refreshTasks(); QTRY_VERIFY(!tasks.historyLoading()); QCOMPARE(tasks.tasks().size(),0);
            QSignalSpy legacyList(&legacy,&AgentApiClient::responseReceived); legacy.listTasks();
            QTRY_COMPARE(legacyList.count(),1); QCOMPARE(legacyList.last()[3].toJsonObject().value("total").toInt(),3);
            tasks.viewHistoryTask(qEnvironmentVariable("FORGEPILOT_RESTART_TASK_ID"));
            QTRY_COMPARE(tasks.state(),"completed"); QVERIFY(tasks.historyReadOnly()); QVERIFY(!tasks.canApprove()); QVERIFY(!tasks.canCancel());
            QVERIFY(capture("restart"));
        } else {
            QVERIFY(c.conversationId().isEmpty()); QVERIFY(c.repositoryPath().isEmpty()); QVERIFY(capture("empty"));
            const auto background=prompt->property("background").value<QObject*>(); QVERIFY(background);
            QVERIFY(background->property("color").value<QColor>().lightnessF()>0.8);
            QVERIFY(prompt->property("color").value<QColor>().lightnessF()<0.3);
            QVERIFY(!window->property("detailsExpanded").toBool());
            prompt->setProperty("text","切换模式时保留草稿");
            QVERIFY(click("taskModeButton")); QCOMPARE(c.mode(),"task");
            QVERIFY(!find("sendMessageButton")->property("enabled").toBool());
            QCOMPARE(prompt->property("text").toString(),QString("切换模式时保留草稿"));
            QVERIFY(capture("work-empty"));
            QVERIFY(click("chatModeButton")); QCOMPARE(c.mode(),"chat");
            QVERIFY(click("sidebarToggle")); QVERIFY(!window->property("sidebarShown").toBool());
            QVERIFY(click("sidebarToggle")); QVERIFY(window->property("sidebarShown").toBool());
            auto *promptItem=qobject_cast<QQuickItem*>(prompt); QVERIFY(promptItem); promptItem->forceActiveFocus();
            QTest::keyClick(window,Qt::Key_Return,Qt::ShiftModifier);
            QVERIFY(prompt->property("text").toString().contains('\n')); QVERIFY(c.conversationId().isEmpty());
            prompt->setProperty("text","请记住本轮称呼是青松"); QTest::qWait(60);
            QTest::keyClick(window,Qt::Key_Return);
            QTRY_VERIFY(c.active()); QVERIFY(capture("answering"));
            QTRY_VERIFY_WITH_TIMEOUT(!c.active(),10000); QTRY_COMPARE(c.messages()->count(),2);
            const auto first=c.conversationId(); QVERIFY(!first.isEmpty());
            prompt->setProperty("text","刚才的称呼是什么？"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),4,10000);
            QTRY_VERIFY_WITH_TIMEOUT(!c.active(),10000);
            QVERIFY(c.messages()->get(3).value("content").toString().contains("青松")); QVERIFY(capture("followup"));
            c.renameConversation("青松的连续对话"); QTRY_COMPARE(c.title(),QString("青松的连续对话"));
            QVERIFY(click("newTaskButton")); QTRY_VERIFY(c.conversationId()!=first); QTRY_VERIFY(!c.loading());
            const auto second=c.conversationId(); QCOMPARE(c.messages()->count(),0);
            prompt->setProperty("text","慢回答 fixture"); QVERIFY(click("sendMessageButton")); QTRY_VERIFY(c.active());
            c.selectConversation(first); QTRY_COMPARE(c.messages()->count(),4);
            QTest::qWait(1800); QCOMPARE(c.messages()->count(),4);
            c.selectConversation(second); QTRY_VERIFY(c.active());
            QTRY_VERIFY(!c.loading());
            QSignalSpy resets(c.messages(),&QAbstractItemModel::modelReset);
            QSignalSpy updates(c.messages(),&QAbstractItemModel::dataChanged);
            for(int i=0;i<3;++i) {
                QTest::qWait(1100);
                QVERIFY(c.active()); QVERIFY(!c.loading()); QVERIFY(!c.busy());
                QVERIFY(find("stopAnswerButton")->property("enabled").toBool());
            }
            QCOMPARE(resets.count(),0); QCOMPARE(updates.count(),0); QCOMPARE(c.messages()->count(),2);
            QVERIFY(click("stopAnswerButton")); QTRY_VERIFY(!c.active()); QTRY_COMPARE(c.messages()->count(),2);
            QVERIFY(c.messages()->get(1).value("content").toString().contains("停止")); QVERIFY(capture("stopped"));
            prompt->setProperty("text","停止回答后继续提问"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),4,10000); QTRY_VERIFY(!c.active());
            QVERIFY(c.messages()->get(3).value("content").toString().contains("离线问答样例")); QVERIFY(tasks.taskId().isEmpty());
            c.selectConversation(first); QTRY_VERIFY(!c.loading());
            c.chooseRepository(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY")));
            QTRY_VERIFY(!c.repositoryPath().isEmpty()); QTRY_VERIFY(!c.busy());
            prompt->setProperty("text","只读检查 main.cpp fixture"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),6,10000); QTRY_VERIFY(!c.active());
            QVERIFY(c.messages()->get(5).value("content").toString().contains("只读文件已检查"));
            QVERIFY(tasks.taskId().isEmpty()); QCOMPARE(tasks.tasks().size(),0); QVERIFY(capture("readonly"));
            const auto task1=createLegacyTask("修复 main.cpp 并验证"); QVERIFY(!task1.isEmpty());
            tasks.selectTask(task1); window->setProperty("detailsExpanded",true);
            QTRY_VERIFY_WITH_TIMEOUT(tasks.canApprove(),15000);
            window->resize(1000,720); prompt->setProperty("text",QString("长草稿 <原文>\n").repeated(1000));
            QTest::qWait(150); auto *approve=qobject_cast<QQuickItem*>(find("approveButton")); QVERIFY(approve);
            QVERIFY(approve->mapToScene(QPointF(approve->width(),approve->height())).y()<window->height());
            QVERIFY(capture("approval-long-draft")); window->resize(1440,960);
            QVERIFY(capture("approval")); QVERIFY(click("approveButton"));
            QTRY_COMPARE_WITH_TIMEOUT(tasks.state(),"completed",60000);
            QVERIFY(!c.active()); QCOMPARE(c.messages()->count(),6);
            QVERIFY(capture("completed"));
            const auto task2=createLegacyTask("再次独立执行"); QVERIFY(!task2.isEmpty()); tasks.selectTask(task2);
            QTRY_VERIFY_WITH_TIMEOUT(tasks.canApprove(),15000); QVERIFY(task1!=tasks.taskId()); QCOMPARE(tasks.planVersion(),1);
            QVERIFY(click("rejectButton")); QTRY_COMPARE(tasks.state(),"cancelled"); QTRY_VERIFY(!c.active());
            QCOMPARE(c.messages()->count(),6); QTRY_COMPARE(tasks.tasks().size(),0);
            window->setProperty("detailsExpanded",false);
            prompt->setProperty("text","拒绝方案后继续提问"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),8,10000); QTRY_VERIFY(!c.active());
            QVERIFY(c.messages()->get(7).value("content").toString().contains("青松")); QCOMPARE(tasks.tasks().size(),0);
            const auto task3=createLegacyTask("启动后停止本轮任务"); QVERIFY(!task3.isEmpty()); tasks.selectTask(task3);
            QTRY_VERIFY_WITH_TIMEOUT(tasks.canApprove(),15000); tasks.cancelTask();
            QTRY_COMPARE(tasks.state(),"cancelled"); QVERIFY(!c.active()); QCOMPARE(c.messages()->count(),8);
            window->setProperty("detailsExpanded",false);
            prompt->setProperty("text","停止任务后继续提问"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),10,10000); QTRY_VERIFY(!c.active());
            QVERIFY(c.messages()->get(9).value("content").toString().contains("青松")); QTRY_COMPARE(tasks.tasks().size(),0);
            QSignalSpy legacyList(&legacy,&AgentApiClient::responseReceived); legacy.listTasks();
            QTRY_COMPARE(legacyList.count(),1); QCOMPARE(legacyList.last()[3].toJsonObject().value("total").toInt(),3);
            prompt->setProperty("text","fixture chat failure"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),12,10000);
            QTRY_VERIFY_WITH_TIMEOUT(!c.active(),10000);
            QVERIFY(c.messages()->get(11).value("content").toString().contains("回答未完成")); QVERIFY(capture("failed"));
            window->resize(1000,720); QTRY_COMPARE(window->width(),1000); QTest::qWait(100);
            auto *send=qobject_cast<QQuickItem*>(find("sendMessageButton")); QVERIFY(send);
            QVERIFY(send->mapToScene(QPointF(0,send->height())).y()<window->height());
            QVERIFY(capture("small"));
            QFile marker(output+"/conversation-id.txt"); QVERIFY(marker.open(QIODevice::WriteOnly)); marker.write(first.toUtf8());
            // Starting work from a blank view can attach a project in one step.
            c.selectConversation({}); QVERIFY(click("taskModeButton"));
            prompt->setProperty("text","选择项目后应保留的工作目标");
            QVERIFY(QMetaObject::invokeMethod(find("conversationView"),"chooseProject",
                Q_ARG(QVariant,QVariant::fromValue(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY"))))));
            QTRY_VERIFY(!c.conversationId().isEmpty()); QTRY_VERIFY(!c.busy()); QTRY_VERIFY(!c.loading());
            QVERIFY(!c.repositoryPath().isEmpty()); QCOMPARE(c.mode(),"task");
            QCOMPARE(prompt->property("text").toString(),QString("选择项目后应保留的工作目标"));
            QVERIFY(capture("work-ready"));
            window->resize(860,640); QTest::qWait(100);
            prompt->setProperty("text",QString("很长的草稿\n").repeated(500)); QTest::qWait(100);
            auto *composer=qobject_cast<QQuickItem*>(find("messageComposer")); QVERIFY(composer);
            const auto composerRect=composer->mapRectToScene(QRectF(0,0,composer->width(),composer->height()));
            QVERIFY(composerRect.top()>=0 && composerRect.bottom()<window->height());
            QVERIFY(capture("compact-work"));
        }
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
    }
};
QTEST_MAIN(ConversationBackendTest)
#include "tst_conversations_backend.moc"
