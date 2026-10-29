#include "ConversationController.h"
#include "ClientUi.h"
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQuickWindow>
#include <QQuickItem>
#include <QtTest>
#include <functional>

class MemoryBackendTest:public QObject {
    Q_OBJECT
private slots:
    void realMemoryUi() {
        configureClientUi(*qobject_cast<QGuiApplication*>(QCoreApplication::instance()));
        TaskController tasks; tasks.setBaseUrl(QUrl(qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL")));
        ConversationController c(&tasks); auto *memory=c.memory();
        QQmlApplicationEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError>&errors){ for(const auto &error:errors) warnings.append(error.toString()); });
        engine.setInitialProperties({{"controller",QVariant::fromValue(&tasks)},{"conversations",QVariant::fromValue(&c)},{"developmentTest",true}});
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
        auto ready=[&](const QString &name){ auto *item=qobject_cast<QQuickItem*>(find(name)); return item && item->isEnabled() && item->isVisible(); };
        auto click=[&](const QString &name){
            if(!ready(name)) return false;
            auto *item=qobject_cast<QQuickItem*>(find(name)); QTest::qWait(60);
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,item->mapToScene(QPointF(item->width()/2,item->height()/2)).toPoint()); return true;
        };
        auto capture=[&](const QString &name){QTest::qWait(100); return window->grabWindow().save(qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT")+"/memory-"+name+".png");};
        auto *prompt=find("conversationPrompt"); QVERIFY(prompt);
        QTRY_VERIFY(!c.historyLoading()); QTRY_VERIFY(!c.loading());
        const auto source=qEnvironmentVariable("FORGEPILOT_MEMORY_SOURCE");
        if(qEnvironmentVariableIsSet("FORGEPILOT_MEMORY_RESTART")) {
            QCOMPARE(c.historyTotal(),1); QCOMPARE(c.messages()->count(),2); QVERIFY(c.repositoryPath().isEmpty());
            QVERIFY(click("memoryManagerButton")); QTRY_VERIFY(!memory->loading()); QCOMPARE(memory->items().size(),1);
            QCOMPARE(memory->items().first().toMap().value("content").toString(),QString("保留的全局偏好：先给结论"));
            QVERIFY(memory->summary().value("summary").toMap().isEmpty()); QVERIFY(capture("restart"));
            QVERIFY(click("closeMemoryButton")); prompt->setProperty("text","重启后的偏好确认"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),4,10000); QTRY_VERIFY(!c.active());
        } else {
            QCOMPARE(c.conversationId(),source); QCOMPARE(c.messages()->count(),32);
            c.chooseRepository(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY")));
            QTRY_VERIFY(!c.repositoryPath().isEmpty()); QTRY_VERIFY(!c.busy());
            QVERIFY(click("memoryManagerButton")); QTRY_VERIFY(!memory->loading()); QVERIFY(memory->items().isEmpty()); QVERIFY(capture("empty"));
            QVERIFY(click("addMemoryButton")); QTRY_VERIFY(ready("memoryContentInput"));
            find("memoryContentInput")->setProperty("text","可编辑的全局偏好：默认中文回答"); QVERIFY(click("saveMemoryButton"));
            QTRY_COMPARE(memory->items().size(),1); QTRY_VERIFY(!memory->loading()); const auto global=memory->items().first().toMap().value("memory_id").toString();
            QVERIFY(click("editMemory_"+global)); QTRY_VERIFY(ready("memoryContentInput"));
            find("memoryContentInput")->setProperty("text","编辑后的全局偏好：先给结论"); QVERIFY(click("saveMemoryButton"));
            QTRY_COMPARE(memory->items().first().toMap().value("content").toString(),QString("编辑后的全局偏好：先给结论")); QTRY_VERIFY(!memory->loading());
            QVERIFY(click("deleteMemory_"+global)); QTRY_VERIFY(ready("confirmMemoryRemovalButton")); QVERIFY(click("confirmMemoryRemovalButton"));
            QTRY_VERIFY(memory->items().isEmpty()); QTRY_VERIFY(!memory->loading());
            QVERIFY(click("addMemoryButton")); QTRY_VERIFY(ready("projectMemoryScope")); QVERIFY(click("projectMemoryScope"));
            find("memoryContentInput")->setProperty("text","项目来源偏好：保留现有测试"); QVERIFY(capture("project-editor")); QVERIFY(click("saveMemoryButton"));
            QTRY_COMPARE(memory->items().size(),1); QTRY_VERIFY(!memory->loading());
            QCOMPARE(memory->items().first().toMap().value("source_conversation_id").toString(),source);
            QVERIFY(click("addMemoryButton")); QTRY_VERIFY(ready("memorySourceCheck")); QVERIFY(click("memorySourceCheck"));
            QVERIFY(!find("memorySourceCheck")->property("checked").toBool());
            find("memoryContentInput")->setProperty("text","保留的全局偏好：先给结论"); QVERIFY(click("saveMemoryButton"));
            QTRY_COMPARE(memory->items().size(),2); QTRY_VERIFY(!memory->loading()); QVERIFY(capture("scopes"));
            QVERIFY(click("closeMemoryButton")); prompt->setProperty("text","请检查当前文件 fixture"); QVERIFY(click("sendMessageButton"));
            // The first summary is generated after this reply, so its context notice
            // describes the pre-summary request. Verify the committed summary below.
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),34,10000); QTRY_VERIFY(!c.active());
            QVERIFY(click("memoryManagerButton")); QTRY_VERIFY(!memory->loading()); QVERIFY(click("summaryMemoryTab"));
            QTRY_VERIFY(!memory->summary().value("summary").toMap().isEmpty());
            QVERIFY(memory->summary().value("summary").toMap().value("through_message_id").toInt()>0); QVERIFY(capture("summary"));
            find("summaryMemoryContent")->setProperty("text","手工纠正的摘要：旧代码结论须重新读取确认"); QVERIFY(click("saveSummaryButton"));
            QTRY_VERIFY(!memory->busy()); QTRY_VERIFY(!memory->loading());
            QTRY_COMPARE(memory->summary().value("summary").toMap().value("content").toString(),QString("手工纠正的摘要：旧代码结论须重新读取确认"));
            QVERIFY(click("closeMemoryButton")); prompt->setProperty("text","使用编辑后的摘要 fixture"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),36,10000); QTRY_VERIFY(!c.active());
            QTRY_VERIFY(c.contextNotice().contains("摘要"));
            QVERIFY(click("memoryManagerButton")); QTRY_VERIFY(!memory->loading()); QVERIFY(click("deleteSummaryButton"));
            QTRY_VERIFY(ready("confirmMemoryRemovalButton")); QVERIFY(click("confirmMemoryRemovalButton"));
            QTRY_VERIFY(!memory->loading()); QTRY_VERIFY(!memory->busy()); QTRY_VERIFY(!memory->summary().value("enabled").toBool());
            QVERIFY(memory->summary().value("summary").toMap().isEmpty()); QVERIFY(capture("summary-disabled"));
            QVERIFY(click("closeMemoryButton")); prompt->setProperty("text","删除摘要后继续 fixture"); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),38,10000); QTRY_VERIFY(!c.active());
            QVERIFY(click("memoryManagerButton")); QTRY_VERIFY(!memory->loading()); QVERIFY(click("enableSummaryButton"));
            QTRY_VERIFY(memory->summary().value("enabled").toBool()); QTRY_VERIFY(!memory->loading()); QVERIFY(click("closeMemoryButton"));
            prompt->setProperty("text","重新生成摘要 fixture"); QVERIFY(click("sendMessageButton")); QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),40,10000); QTRY_VERIFY(!c.active());
            QVERIFY(click("newTaskButton")); QTRY_VERIFY(c.conversationId()!=source); QTRY_VERIFY(!c.loading());
            prompt->setProperty("text","新对话仅有全局记忆 fixture"); QVERIFY(click("sendMessageButton")); QTRY_COMPARE_WITH_TIMEOUT(c.messages()->count(),2,10000); QTRY_VERIFY(!c.active()); QTRY_VERIFY(!c.historyLoading());
            QTRY_VERIFY(ready("conversationMore_"+source)); QVERIFY(click("conversationMore_"+source));
            QTRY_VERIFY(ready("deleteConversation_"+source)); QVERIFY(click("deleteConversation_"+source)); QTRY_VERIFY(!c.deleteLoading());
            QVERIFY(capture("source-confirm")); QVERIFY(click("confirmDeleteConversationButton")); QTRY_VERIFY(!c.deleting()); QTRY_COMPARE(c.historyTotal(),1);
            QVERIFY(click("memoryManagerButton")); QTRY_VERIFY(!memory->loading()); QVERIFY(click("longTermMemoryTab"));
            QCOMPARE(memory->items().size(),1); QCOMPARE(memory->items().first().toMap().value("content").toString(),QString("保留的全局偏好：先给结论")); QVERIFY(capture("source-deleted"));
            window->resize(1000,720); QTRY_COMPARE(window->width(),1000); QVERIFY(capture("small"));
        }
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
    }
};
QTEST_MAIN(MemoryBackendTest)
#include "tst_memory_backend.moc"
