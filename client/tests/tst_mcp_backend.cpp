#include "McpController.h"
#include "ConversationController.h"
#include "ClientUi.h"
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQuickWindow>
#include <QQuickItem>
#include <QJsonDocument>
#include <QSettings>
#include <QDirIterator>
#include <QTemporaryDir>
#include <QFile>
#include <QDesktopServices>
#include <QtTest>

class SourceObserver : public QObject {
    Q_OBJECT
public:
    QUrl opened;
public slots:
    void visit(const QUrl &url) { opened=url; }
};

class McpBackendTest : public QObject {
    Q_OBJECT
private slots:
    void realSettings() {
        configureClientUi(*qobject_cast<QGuiApplication*>(QCoreApplication::instance()));
        QTemporaryDir settingsDirectory; QVERIFY(settingsDirectory.isValid());
        QSettings::setDefaultFormat(QSettings::IniFormat);
        QSettings::setPath(QSettings::IniFormat,QSettings::UserScope,settingsDirectory.path());
        TaskController tasks; const auto base=qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL"); tasks.setBaseUrl(QUrl(base));
        ConversationController conversations(&tasks); McpController mcp(&tasks);
        QQmlApplicationEngine engine; QStringList warnings;
        connect(&engine,&QQmlEngine::warnings,this,[&](const QList<QQmlError> &errors){for(const auto &error:errors) warnings.append(error.toString());});
        engine.setInitialProperties({{"controller",QVariant::fromValue(&tasks)}, {"conversations",QVariant::fromValue(&conversations)}, {"mcp",QVariant::fromValue(&mcp)}});
        engine.load(QUrl::fromLocalFile(qEnvironmentVariable("FORGEPILOT_INTEGRATION_QML")));
        QVERIFY2(!engine.rootObjects().isEmpty(),qPrintable(warnings.join('\n')));
        auto *window=qobject_cast<QQuickWindow*>(engine.rootObjects().first()); QVERIFY(window); QVERIFY(QTest::qWaitForWindowExposed(window));
        auto find=[&](const QString &name)->QObject* {
            if(auto *object=window->findChild<QObject*>(name)) return object;
            std::function<QObject*(QQuickItem*)> visit=[&](QQuickItem *item)->QObject* {
                if(item->objectName()==name) return item;
                for(auto *child:item->childItems()) if(auto *found=visit(child)) return found;
                return nullptr;
            }; return visit(window->contentItem());
        };
        auto ready=[&](const QString &name) { auto *item=qobject_cast<QQuickItem*>(find(name)); return item && item->isEnabled() && item->isVisible(); };
        auto reveal=[&](QQuickItem *item) {
            QTest::qWait(70); // Let newly discovered delegates finish their layout before scrolling.
            for(auto *parent=item->parentItem();parent;parent=parent->parentItem()) {
                if(parent->metaObject()->indexOfProperty("contentY")>=0 && parent->metaObject()->indexOfProperty("contentHeight")>=0) {
                    const auto y=item->mapToItem(parent,QPointF(0,0)).y()+parent->property("contentY").toReal();
                    const auto maximum=qMax(0.0,parent->property("contentHeight").toReal()-parent->height());
                    parent->setProperty("contentY",qBound(0.0,y-parent->height()/2,maximum));
                }
            }
            QTest::qWait(40);
        };
        auto click=[&](const QString &name) {
            if(!ready(name)) return false;
            auto *item=qobject_cast<QQuickItem*>(find(name)); reveal(item);
            const auto point=item->mapToScene(QPointF(item->width()/2,item->height()/2));
            if(point.y()>=window->height() || point.y()<0) { captureFailure(window); return false; }
            QTest::mouseClick(window,Qt::LeftButton,Qt::NoModifier,point.toPoint()); QTest::qWait(30); return true;
        };
        // Simulate a text edit/paste through the real QML TextField signal.
        auto input=[&](const QString &name,const QString &value) {
            auto *item=qobject_cast<QQuickItem*>(find(name)); if(!item || !item->isEnabled()) return false;
            reveal(item); item->forceActiveFocus(); item->setProperty("text",value);
            if(name=="conversationPrompt") return true; // TextArea uses textChanged, not TextField.textEdited.
            return QMetaObject::invokeMethod(item,"textEdited");
        };
        auto choose=[&](const QString &name,int index) {
            auto *item=find(name); if(!item || !item->property("enabled").toBool()) return false;
            item->setProperty("currentIndex",index); return QMetaObject::invokeMethod(item,"activated",Q_ARG(int,index));
        };
        auto capture=[&](const QString &name) { QTest::qWait(100); return window->grabWindow().save(qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT")+"/"+name+".png"); };
        auto http=[&](const QByteArray &method,const QString &path,const QJsonObject &body=QJsonObject{}) {
            QNetworkAccessManager network; QEventLoop wait; QJsonObject result;
            JsonHttp::send(network,&wait,method,QUrl(base+path),body,512*1024,[&](QNetworkReply*,const JsonHttp::Response &r){result=r.body; result["_status"]=r.status; wait.quit();});
            QTimer::singleShot(5000,&wait,&QEventLoop::quit); wait.exec(); return result;
        };
        auto control=[&](const QJsonObject &body) { return http("POST","/fixture/control",body).value("ok").toBool(); };
        auto revision=[&](const QString &id) { for(const auto &entry:mcp.items()) if(entry.toMap().value("server_id")==id) return entry.toMap().value("revision").toLongLong(); return qint64(0); };
        auto select=[&](const QString &name) { return click("mcpTool_"+name); };
        auto edit=[&](const QString &id) { return click("editMcp_"+id); };
        auto remove=[&](const QString &id) { return click("deleteMcp_"+id) && click("confirmMcpRemovalButton"); };
        QTRY_VERIFY(!conversations.historyLoading());
        const auto phase=qEnvironmentVariable("FORGEPILOT_MCP_PHASE");
        const bool liveModel=phase=="runtime-model" || phase=="runtime-model-restart";
        if(phase=="runtime" || phase=="runtime-restart" || liveModel) {
            auto closeTools=[&] { return QMetaObject::invokeMethod(find("mcpToolsDialog"),"close"); };
            if(phase=="runtime-restart" || phase=="runtime-model-restart") {
                QTRY_VERIFY(!conversations.conversationId().isEmpty() && !conversations.loading());
                QCOMPARE(conversations.messages()->count(),liveModel ? 2 : 6);
                conversations.showMcpTools(conversations.messages()->get(1).value("request_id").toString());
                QVERIFY(QMetaObject::invokeMethod(find("mcpToolsDialog"),"open"));
                QTRY_VERIFY(!conversations.mcpToolsLoading()); QCOMPARE(conversations.mcpTools().size(),2);
                QCOMPARE(conversations.mcpTools().last().toMap().value("tool_name").toString(),QString("tavily-extract"));
                QCOMPARE(conversations.mcpTools().first().toMap().value("sources").toList().size(),1);
                QVERIFY(capture("runtime-restart-history")); QVERIFY(closeTools());
                conversations.prepareDelete(conversations.conversationId(),conversations.title());
                QTRY_VERIFY(!conversations.deleteLoading()); conversations.confirmDelete();
                QTRY_VERIFY(!conversations.deleting()); QVERIFY(conversations.mcpTools().isEmpty());
                QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n'))); return;
            }
            QVERIFY(click("settingsButton")); QTRY_VERIFY(!mcp.loading()); QVERIFY(click("mcpSettingsTab"));
            const auto secret=qEnvironmentVariable("FORGEPILOT_MCP_SECRET");
            QVERIFY(click("addMcpButton")); QVERIFY(input("mcpKey",secret)); QVERIFY(click("probeMcpButton"));
            QTRY_COMPARE_WITH_TIMEOUT(mcp.state(),QString("discovered"),10000);
            QSignalSpy saved(&mcp,&McpController::saved); QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),1);
            QVERIFY(click("closeSettingsButton"));
            QVERIFY(conversations.repositoryPath().isEmpty());
            QVERIFY(input("conversationPrompt",liveModel
                ? "这是隔离 MCP 验证。请先调用 Search public fixture 搜索 Qt documentation，再调用 Extract public fixture 提取 https://doc.qt.io/qt-6/ 。两种工具各调用一次，失败则如实说明；成功后只依据实际结果给出两句中文概括和来源。无需项目。"
                : "查询 Qt 文档并提取正文 fixture")); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(conversations.messages()->count(),2,liveModel ? 240000 : 12000); QTRY_VERIFY(!conversations.active());
            QCOMPARE(conversations.toolActivity().value("status").toString(),QString("succeeded"));
            if(liveModel) QVERIFY(!conversations.messages()->get(1).value("content").toString().isEmpty());
            else QVERIFY(conversations.messages()->get(1).value("content").toString().contains("实际查询"));
            QVERIFY(click("activeMcpToolsButton")); QTRY_VERIFY(!conversations.mcpToolsLoading());
            QCOMPARE(conversations.mcpTools().size(),2);
            const auto extract=conversations.mcpTools().last().toMap();
            QCOMPARE(extract.value("tool_name").toString(),QString("tavily-extract"));
            QCOMPARE(extract.value("status").toString(),QString("succeeded"));
            QVERIFY(extract.value("result_excerpt").toString().contains("正文提取 fixture"));
            QCOMPARE(extract.value("sources").toList().size(),1);
            const auto sources=conversations.mcpTools().first().toMap().value("sources").toList(); QCOMPARE(sources.size(),1);
            QCOMPARE(sources.first().toMap().value("url").toString(),QString("https://doc.qt.io/qt-6/"));
            SourceObserver observer; QDesktopServices::setUrlHandler("https",&observer,"visit");
            QVERIFY(click("mcpSourceLink")); QCOMPARE(observer.opened,QUrl("https://doc.qt.io/qt-6/")); QDesktopServices::unsetUrlHandler("https");
            QVERIFY(capture("runtime-sources")); QVERIFY(closeTools());
            if(liveModel) { QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n'))); return; }
            // A delayed record response must not reopen a closed dialog.
            QVERIFY(control({{"delay_records",true}})); conversations.showMcpTools(conversations.messages()->get(1).value("request_id").toString());
            QTest::qWait(150); conversations.closeMcpTools(); QTest::qWait(900); QVERIFY(conversations.mcpTools().isEmpty());
            QVERIFY(control({{"mode","tool_error"}}));
            QVERIFY(input("conversationPrompt","失败查询 fixture")); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(conversations.messages()->count(),4,12000); QTRY_VERIFY(!conversations.active());
            QCOMPARE(conversations.toolActivity().value("status").toString(),QString("failed"));
            QVERIFY(click("activeMcpToolsButton")); QTRY_VERIFY(!conversations.mcpToolsLoading());
            QCOMPARE(conversations.mcpTools().first().toMap().value("error_code").toString(),QString("mcp_tool_error"));
            QVERIFY(capture("runtime-failure")); QVERIFY(closeTools());
            QVERIFY(control({{"mode","hang"},{"fail_on","tools/call"}}));
            QVERIFY(input("conversationPrompt","取消查询 fixture")); QVERIFY(click("sendMessageButton"));
            QTRY_COMPARE_WITH_TIMEOUT(conversations.toolActivity().value("status").toString(),QString("running"),12000);
            // Running includes lazy initialization. Stop only after the peer has
            // received the hanging business call, so this tests in-flight cleanup.
            QTRY_COMPARE(http("POST","/fixture/control").value("business_call_count").toInt(),4);
            QVERIFY(click("stopAnswerButton")); QTRY_COMPARE_WITH_TIMEOUT(conversations.messages()->count(),6,12000);
            QTRY_VERIFY(!conversations.active()); QCOMPARE(conversations.toolActivity().value("status").toString(),QString("cancelled"));
            window->resize(860,640); QVERIFY(click("activeMcpToolsButton")); QTRY_VERIFY(!conversations.mcpToolsLoading());
            QVERIFY(capture("runtime-cancel-small")); QVERIFY(closeTools());
            QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n'))); return;
        }
        QVERIFY(click("settingsButton")); QTRY_VERIFY(!mcp.loading()); QVERIFY(click("mcpSettingsTab"));
        if(phase=="restart-disabled") {
            QCOMPARE(mcp.items().size(),1); QVERIFY(mcp.disabled());
            const auto id=mcp.items().first().toMap().value("server_id").toString();
            QVERIFY(!ready("addMcpButton")); QVERIFY(!ready("testMcp_"+id));
            QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy()); QCOMPARE(mcp.kind(),0); QVERIFY(mcp.hasSecret("api_key"));
            QCOMPARE(mcp.draft().value("selected_tools").toList().size(),2);
            QVERIFY(find("mcpKey")->property("text").toString().isEmpty()); QVERIFY(!ready("probeMcpButton")); QVERIFY(!ready("saveMcpButton"));
            QVERIFY(click("closeMcpEditor")); QVERIFY(click("mcpEnabled_"+id)); QTRY_VERIFY(!mcp.loading() && !mcp.busy());
            QVERIFY(!mcp.items().first().toMap().value("enabled").toBool()); QVERIFY(!ready("mcpEnabled_"+id));
            QVERIFY(capture("restart-disabled")); QVERIFY(remove(id)); QTRY_VERIFY(mcp.items().isEmpty());
            QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n'))); return;
        }
        QVERIFY(!mcp.disabled()); QVERIFY(mcp.items().isEmpty()); QVERIFY(capture("empty"));
        const auto endpoint=qEnvironmentVariable("FORGEPILOT_MCP_ENDPOINT");
        const auto secret=qEnvironmentVariable("FORGEPILOT_MCP_SECRET"), replacement=qEnvironmentVariable("FORGEPILOT_MCP_REPLACEMENT");
        QSignalSpy saved(&mcp,&McpController::saved);
        if(phase=="diagnostics") {
            QVERIFY(click("addMcpButton")); QVERIFY(choose("mcpKind",1)); QVERIFY(input("mcpEndpoint",endpoint));
            QVERIFY(choose("mcpAuth",1)); QVERIFY(input("mcpKey",secret));
            for(const auto &entry:QList<QPair<QString,QString>>{
                {"mcp_redirect","最终 MCP 地址"},{"mcp_disconnected","检查服务是否运行"},
                {"mcp_unavailable","检查远程服务或本地程序"},{"mcp_capability","工具能力"},
                {"mcp_storage","原配置已保留"},{"mcp_catalog","工具列表无效"},
                {"mcp_request","服务拒绝"},{"server_stopping","启动后端后重试"},
                {"mcp_transient","稍后重试"},{"mcp_session_expired","重新连接"}}) {
                QVERIFY(control({{"probe_error",entry.first}})); QVERIFY(click("probeMcpButton"));
                QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(mcp.editorError().contains(entry.first));
                QVERIFY(mcp.editorError().contains(entry.second));
                QVERIFY(!mcp.editorError().contains(secret)); QVERIFY(!mcp.editorError().contains("private fixture"));
                QCOMPARE(find("mcpKey")->property("text").toString(),secret);
                QVERIFY(!mcp.uncertain()); QVERIFY(!mcp.pendingSave());
            }
            QVERIFY(capture("fixed-errors"));
            QVERIFY(control({{"probe_error","untrusted-"+secret}})); QVERIFY(click("probeMcpButton"));
            QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(!mcp.editorError().contains(secret));
            QVERIFY(!mcp.editorError().contains("untrusted-"));
            QVERIFY(control({{"probe_error",QJsonValue::Null}})); QVERIFY(click("probeMcpButton"));
            QTRY_COMPARE(mcp.state(),QString("discovered")); QVERIFY(!ready("mcpTool_bad_schema"));
            QVERIFY(mcp.toolReason("mcp_schema_unsupported: "+secret).contains("mcp_schema_unsupported"));
            QVERIFY(!mcp.toolReason("unknown: "+secret).contains(secret));
            QVERIFY(capture("incompatible-tools")); QVERIFY(click("closeMcpEditor")); QVERIFY(click("closeSettingsButton"));
            QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n'))); return;
        }
        if(phase=="recovery") {
            // New UUID: a committed PUT loses its response, then verification is delayed.
            QVERIFY(click("addMcpButton")); QVERIFY(choose("mcpKind",1)); QVERIFY(input("mcpEndpoint",endpoint));
            QVERIFY(choose("mcpAuth",1)); QVERIFY(input("mcpKey",secret));
            QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered")); QVERIFY(select("search"));
            const auto pendingId=mcp.serverId();
            QVERIFY(control({{"drop_save",true},{"delay_detail",true}})); QVERIFY(click("saveMcpButton"));
            QTRY_VERIFY(mcp.state()!="saving" && !mcp.canCloseEditor());
            QVERIFY(mcp.editorBusy()); QVERIFY(!ready("resolveMcpSave"));
            QVERIFY(!ready("closeMcpEditor")); QVERIFY(!ready("closeSettingsButton"));
            mcp.closeEditor(); mcp.close(); QVERIFY(mcp.editorOpen());
            QTest::keyClick(window,Qt::Key_Escape); QVERIFY(mcp.editorOpen());
            QVERIFY(!window->close()); QVERIFY(window->isVisible());
            QVERIFY(capture("verification-busy"));
            QTRY_VERIFY(!mcp.pendingSave()); QTRY_VERIFY(!mcp.loading());
            QCOMPARE(mcp.items().size(),1); QCOMPARE(mcp.serverId(),pendingId); QCOMPARE(saved.count(),0);
            QVERIFY(mcp.editorNotice().contains("当前保存版本")); QVERIFY(!mcp.editorNotice().contains("成功"));
            auto requests=http("POST","/fixture/control");
            QCOMPARE(requests.value("put_ids").toArray(),QJsonArray{pendingId});
            QCOMPARE(requests.value("detail_ids").toArray(),QJsonArray{pendingId});

            // An unknown replacement that never committed can only read the old version.
            QVERIFY(input("mcpKey",replacement)); QVERIFY(control({{"expected_secret",replacement}}));
            QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
            const auto before=revision(pendingId);
            QVERIFY(control({{"uncommitted_save",true}})); QVERIFY(click("saveMcpButton"));
            QTRY_VERIFY(!mcp.pendingSave()); QTRY_VERIFY(!mcp.loading());
            QCOMPARE(revision(pendingId),before); QCOMPARE(saved.count(),0);
            QVERIFY(mcp.editorNotice().contains("当前保存版本")); QVERIFY(!mcp.editorNotice().contains("成功"));
            QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
            QVERIFY(click("closeMcpEditor")); QVERIFY(control({{"expected_secret",secret}}));
            QVERIFY(click("testMcp_"+pendingId)); QTRY_VERIFY(!mcp.loading() && !mcp.busy());
            QVERIFY(mcp.notice().contains("检查成功"));

            // Backend changes forcibly discard pending state and fence a late detail reply.
            QVERIFY(edit(pendingId)); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(input("mcpName","旧后端保存"));
            QVERIFY(control({{"drop_save",true},{"delay_detail",true}})); QVERIFY(click("saveMcpButton"));
            QTRY_VERIFY(mcp.state()!="saving" && !mcp.canCloseEditor());
            const auto putCount=http("POST","/fixture/control").value("put_ids").toArray().size();
            tasks.setBaseUrl(QUrl(qEnvironmentVariable("FORGEPILOT_MCP_ALTERNATE")));
            QVERIFY(!ready("addMcpButton")); QTRY_VERIFY(!mcp.loading()); QTest::qWait(1800);
            QVERIFY(!mcp.pendingSave()); QVERIFY(mcp.canCloseEditor()); QVERIFY(!mcp.editorOpen()); QVERIFY(mcp.items().isEmpty());
            QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
            QCOMPARE(http("POST","/fixture/control").value("put_ids").toArray().size(),putCount);
            tasks.setBaseUrl(QUrl(base)); QTRY_VERIFY(!mcp.loading()); QCOMPARE(mcp.items().size(),1);
            QVERIFY(remove(pendingId)); QTRY_VERIFY(mcp.items().isEmpty()); QTRY_VERIFY(!mcp.loading());

            // A new committed UUID survives failed verification and closing/reopening settings.
            QVERIFY(click("addMcpButton")); QVERIFY(input("mcpKey",secret));
            QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
            const auto newId=mcp.serverId();
            QVERIFY(control({{"drop_save",true},{"fail_detail",true}})); QVERIFY(click("saveMcpButton"));
            QTRY_VERIFY(mcp.editorError().contains("无法确认保存状态")); QVERIFY(mcp.canCloseEditor());
            const auto newPutCount=http("POST","/fixture/control").value("put_ids").toArray().size();
            QVERIFY(click("closeMcpEditor")); QVERIFY(click("closeSettingsButton"));
            QVERIFY(mcp.pendingSave()); QVERIFY(mcp.draft().isEmpty());
            QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
            QVERIFY(click("settingsButton")); QTRY_VERIFY(!mcp.loading()); QVERIFY(click("mcpSettingsTab"));
            QVERIFY(!ready("addMcpButton")); mcp.edit(); QVERIFY(!mcp.editorOpen());
            QVERIFY(capture("pending-new-save"));
            QVERIFY(control({{"fail_detail",false}})); QVERIFY(click("resolveMcpPending"));
            QTRY_VERIFY(!mcp.pendingSave()); QTRY_VERIFY(!mcp.loading()); QCOMPARE(mcp.items().size(),1);
            QCOMPARE(mcp.items().first().toMap().value("server_id").toString(),newId);
            requests=http("POST","/fixture/control");
            QCOMPARE(requests.value("put_ids").toArray().size(),newPutCount);
            QCOMPARE(requests.value("detail_ids").toArray().last().toString(),newId);
            QVERIFY(remove(newId)); QTRY_VERIFY(mcp.items().isEmpty()); QTRY_VERIFY(!mcp.loading());

            // A verified 404 keeps an open new draft and its original UUID for explicit retry.
            QVERIFY(click("addMcpButton")); QVERIFY(input("mcpKey",secret));
            QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
            const auto absentId=mcp.serverId();
            QVERIFY(control({{"uncommitted_save",true}})); QVERIFY(click("saveMcpButton"));
            QTRY_VERIFY(!mcp.pendingSave()); QTRY_VERIFY(!mcp.loading()); QVERIFY(mcp.items().isEmpty());
            QCOMPARE(mcp.serverId(),absentId); QVERIFY(mcp.editorNotice().contains("此 ID 尚无保存记录"));
            QCOMPARE(find("mcpKey")->property("text").toString(),secret); QCOMPARE(saved.count(),0);
            QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
            QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),1); QTRY_VERIFY(!mcp.loading());
            QCOMPARE(mcp.items().first().toMap().value("server_id").toString(),absentId);
            requests=http("POST","/fixture/control"); const auto puts=requests.value("put_ids").toArray();
            QCOMPARE(puts[puts.size()-2].toString(),absentId); QCOMPARE(puts.last().toString(),absentId);
            QVERIFY(remove(absentId)); QTRY_VERIFY(mcp.items().isEmpty()); QTRY_VERIFY(!mcp.loading());
            QVERIFY(click("closeSettingsButton"));
            QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n'))); return;
        }
        QVERIFY(click("addMcpButton")); QTRY_VERIFY(mcp.editorOpen());
        QCOMPARE(find("saveMcpButton")->property("text").toString(),QString("保存并启用"));
        QVERIFY(choose("mcpKind",1)); QVERIFY(input("mcpName","自定义远程查询")); QVERIFY(input("mcpEndpoint",endpoint));
        QVERIFY(choose("mcpAuth",1)); QVERIFY(input("mcpKey",secret));
        const auto id=mcp.serverId(); QVERIFY(!id.isEmpty());
        QVERIFY(click("probeMcpButton")); mcp.probe(); // Guard repeat submission while the real request is pending.
        QTRY_COMPARE_WITH_TIMEOUT(mcp.state(),QString("discovered"),10000); QCOMPARE(mcp.tools().size(),4);
        QVERIFY(!mcp.canSave()); QVERIFY(!ready("mcpTool_write_item")); QVERIFY(!ready("mcpTool_bad_schema"));
        QVERIFY(mcp.toolReason("mcp_schema_unsupported: private").contains("参数结构暂不支持"));
        QVERIFY(mcp.toolReason("mcp_read_only: private").contains("修改外部数据"));
        QVERIFY(select("search")); QVERIFY(mcp.canSave());
        QVERIFY(input("mcpName","远程查询已编辑")); QCOMPARE(mcp.state(),QString("discovered"));
        QVERIFY(capture("remote-discovered")); QVERIFY(control({{"delay_save",true}})); QVERIFY(click("saveMcpButton")); mcp.save();
        QVERIFY(!mcp.canCloseEditor()); QVERIFY(!ready("closeMcpEditor")); QVERIFY(!ready("closeSettingsButton"));
        mcp.closeEditor(); mcp.close(); QVERIFY(mcp.editorOpen());
        QTest::keyClick(window,Qt::Key_Escape); QVERIFY(mcp.editorOpen());
        QVERIFY(!window->close()); QVERIFY(window->isVisible());
        QTRY_COMPARE_WITH_TIMEOUT(saved.count(),1,10000); QTRY_VERIFY(!mcp.loading()); QCOMPARE(mcp.items().size(),1); QCOMPARE(revision(id),1);
        QVERIFY(find("mcpKey")->property("text").toString().isEmpty()); QVERIFY(!mcp.editorOpen());
        QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(mcp.hasSecret("api_key"));
        QTRY_VERIFY(find("mcpKeyStatus")->property("text").toString().contains("已保存，留空保留"));
        QVERIFY(find("mcpKey")->property("text").toString().isEmpty()); QCOMPARE(mcp.tools().size(),4);
        QCOMPARE(mcp.state(),QString("discovered")); QVERIFY(mcp.editorNotice().contains("保存时会重新验证"));
        // Every credential scope change clears a typed replacement and forbids retained credentials.
        for(const auto &changed:QStringList{endpoint+"/",endpoint+"/other",QString(endpoint).replace("127.0.0.1","localhost"),QString(endpoint).replace("http:","https:"),"http://127.0.0.1:9/mcp"}) {
            QVERIFY(input("mcpKey",replacement)); QVERIFY(input("mcpEndpoint",changed));
            QVERIFY(!mcp.hasSecret("api_key")); QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
            QVERIFY(!find("mcpKeyStatus")->property("text").toString().contains("留空保留"));
            const auto requests=http("POST","/fixture/control").value("peer_requests").toInt();
            QVERIFY(click("probeMcpButton")); QVERIFY(mcp.editorError().contains("重新填写密钥"));
            QCOMPARE(http("POST","/fixture/control").value("peer_requests").toInt(),requests);
            QVERIFY(input("mcpEndpoint",endpoint)); QVERIFY(mcp.hasSecret("api_key"));
        }
        QVERIFY(click("closeMcpEditor")); QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy());
        QCOMPARE(find("saveMcpButton")->property("text").toString(),QString("保存"));
        const auto beforeRename=http("POST","/fixture/control").value("peer_requests").toInt();
        QVERIFY(input("mcpName","留空保留密钥")); QCOMPARE(mcp.state(),QString("discovered"));
        QVERIFY(click("mcpTaskScope")); QCOMPARE(mcp.state(),QString("discovered")); QVERIFY(mcp.canSave());
        QVERIFY(click("mcpTaskScope"));
        QCOMPARE(http("POST","/fixture/control").value("peer_requests").toInt(),beforeRename);
        QVERIFY(mcp.canSave()); QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),2); QTRY_VERIFY(!mcp.loading()); QCOMPARE(revision(id),2);
        QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(click("replaceMcpKey")); QVERIFY(input("mcpKey",replacement));
        QVERIFY(click("probeMcpButton")); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(mcp.editorError().contains("鉴权"));
        QCOMPARE(revision(id),2); QCOMPARE(find("mcpKey")->property("text").toString(),replacement);
        QVERIFY(capture("auth-failure"));
        QVERIFY(control({{"expected_secret",replacement}})); QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        // Fail the mandatory save-time re-probe; original database remains unchanged.
        QVERIFY(control({{"mode","429"}})); QVERIFY(click("saveMcpButton")); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(mcp.editorError().contains("繁忙"));
        QCOMPARE(http("GET","/api/v1/mcp/servers/"+id).value("revision").toInteger(),2);
        QCOMPARE(find("mcpKey")->property("text").toString(),replacement); QVERIFY(mcp.canSave());
        QVERIFY(control({{"mode","normal"},{"save_error","mcp_storage"}}));
        QVERIFY(click("saveMcpButton")); QTRY_VERIFY(!mcp.editorBusy());
        QVERIFY(mcp.editorError().contains("原配置已保留")); QVERIFY(mcp.canSave()); QVERIFY(!mcp.pendingSave());
        QCOMPARE(find("mcpKey")->property("text").toString(),replacement);
        QCOMPARE(http("GET","/api/v1/mcp/servers/"+id).value("revision").toInteger(),2);
        QVERIFY(control({{"save_error",QJsonValue::Null},{"change_catalog",true}}));
        QVERIFY(click("saveMcpButton")); QTRY_VERIFY(!mcp.editorBusy());
        QVERIFY(mcp.editorError().contains("工具定义已变化")); QVERIFY(!mcp.canSave()); QVERIFY(!mcp.uncertain());
        QCOMPARE(find("mcpKey")->property("text").toString(),replacement);
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),3); QTRY_VERIFY(!mcp.loading()); QCOMPARE(revision(id),3);
        QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(click("clearMcpKey")); QCOMPARE(mcp.draft().value("connection").toMap().value("auth_type").toString(),QString("none"));
        QVERIFY(control({{"auth","none"}})); QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),4); QTRY_VERIFY(!mcp.loading());
        QVERIFY(!http("GET","/api/v1/mcp/servers/"+id).value("has_secret").toBool());
        QVERIFY(click("testMcp_"+id)); QTRY_VERIFY(!mcp.loading() && !mcp.busy()); QVERIFY(mcp.notice().contains("检查成功"));
        QVERIFY(click("mcpEnabled_"+id)); QTRY_VERIFY(!mcp.loading() && !mcp.busy()); QVERIFY(!mcp.items().first().toMap().value("enabled").toBool());
        QVERIFY(click("mcpEnabled_"+id)); QTRY_VERIFY(!mcp.loading() && !mcp.busy()); QVERIFY(mcp.items().first().toMap().value("enabled").toBool());
        // Preserve UUID and query actual persisted state after a lost response.
        QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(input("mcpName","响应丢失但已保存"));
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        const auto beforeLost=revision(id); QVERIFY(control({{"drop_save",true}})); QVERIFY(click("saveMcpButton"));
        QTRY_VERIFY(!mcp.editorBusy()); QTRY_VERIFY(mcp.editorNotice().contains("当前保存版本")); QVERIFY(!mcp.uncertain()); QCOMPARE(mcp.serverId(),id);
        QTRY_VERIFY(!mcp.loading()); QCOMPARE(revision(id),beforeLost+1); QCOMPARE(mcp.items().size(),1); QCOMPARE(saved.count(),4);
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QCOMPARE(http("PATCH","/api/v1/mcp/servers/"+id,{{"revision",revision(id)},{"enabled",false}}).value("_status").toInt(),200);
        QVERIFY(click("saveMcpButton")); QTRY_COMPARE(mcp.state(),QString("conflict")); QVERIFY(!mcp.uncertain()); QVERIFY(!mcp.canSave());
        QVERIFY(click("reloadMcpEditor")); QTRY_VERIFY(!mcp.editorBusy());
        QCOMPARE(mcp.serverId(),id); QVERIFY(!mcp.draft().value("enabled").toBool());
        // Editing a disabled row keeps it disabled, including an empty selection.
        QVERIFY(select("search")); QVERIFY(mcp.canSave()); QVERIFY(input("mcpName","保持停用"));
        QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),5); QTRY_VERIFY(!mcp.loading());
        QVERIFY(!http("GET","/api/v1/mcp/servers/"+id).value("enabled").toBool());
        QVERIFY(edit(id)); QTRY_VERIFY(!mcp.editorBusy());
        QVERIFY(input("mcpName","关闭后继续核实")); QVERIFY(control({{"drop_save",true},{"fail_detail",true}}));
        QVERIFY(click("saveMcpButton")); QTRY_VERIFY(mcp.editorError().contains("无法确认保存状态"));
        QVERIFY(mcp.pendingSave()); QVERIFY(mcp.canCloseEditor());
        const auto putCount=http("POST","/fixture/control").value("put_ids").toArray().size();
        QVERIFY(click("closeMcpEditor")); QVERIFY(click("closeSettingsButton"));
        QVERIFY(mcp.pendingSave()); QVERIFY(mcp.draft().isEmpty());
        QVERIFY(click("settingsButton")); QTRY_VERIFY(!mcp.loading()); QVERIFY(click("mcpSettingsTab"));
        QVERIFY(!ready("addMcpButton")); QVERIFY(click("resolveMcpPending"));
        QTRY_VERIFY(mcp.canCloseEditor()); QVERIFY(mcp.pendingSave());
        QVERIFY(control({{"fail_detail",false}})); QVERIFY(click("resolveMcpPending"));
        QTRY_VERIFY(!mcp.pendingSave()); QTRY_VERIFY(!mcp.loading()); QCOMPARE(mcp.items().size(),1);
        QCOMPARE(http("POST","/fixture/control").value("put_ids").toArray().size(),putCount);
        QCOMPARE(mcp.items().first().toMap().value("server_id").toString(),id);
        QVERIFY(remove(id)); QTRY_VERIFY(mcp.items().isEmpty()); QTRY_VERIFY(!mcp.loading());
        // Non-MCP failure, connection edits invalidate discovery, no candidate row.
        QVERIFY(click("addMcpButton")); QVERIFY(choose("mcpKind",1)); QVERIFY(input("mcpEndpoint",endpoint)); QVERIFY(control({{"mode","html"}}));
        for(const auto &entry:QList<QPair<QString,QString>>{{"mcp_redirect","最终 MCP 地址"},{"mcp_disconnected","检查服务是否运行"},{"mcp_storage","原配置已保留"}}) {
            QVERIFY(control({{"probe_error",entry.first}})); QVERIFY(click("probeMcpButton")); QTRY_VERIFY(!mcp.editorBusy());
            QVERIFY(mcp.editorError().contains(entry.second)); QVERIFY(!mcp.editorError().contains(secret));
            QVERIFY(mcp.editorError().contains(entry.first));
            QVERIFY(!ready("reloadMcpEditor")); QVERIFY(!mcp.uncertain());
        }
        QVERIFY(control({{"probe_error",QJsonValue::Null}}));
        QVERIFY(click("probeMcpButton")); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(mcp.editorError().contains("MCP 端点")); QVERIFY(mcp.items().isEmpty());
        QVERIFY(control({{"mode","normal"}})); QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QVERIFY(input("mcpEndpoint",endpoint+"/changed")); QCOMPARE(mcp.state(),QString("editing")); QVERIFY(mcp.tools().isEmpty()); QVERIFY(!mcp.canSave());
        // Close before a successfully discovered response is delivered, then reopen.
        QVERIFY(input("mcpEndpoint",endpoint)); QVERIFY(control({{"delay_probe",true}})); QVERIFY(click("probeMcpButton"));
        QTest::qWait(300); QVERIFY(click("closeMcpEditor")); QTRY_VERIFY(!mcp.editorOpen()); QVERIFY(click("addMcpButton"));
        const auto fresh=mcp.serverId(); QTest::qWait(900); QCOMPARE(mcp.serverId(),fresh); QCOMPARE(mcp.state(),QString("editing")); QVERIFY(mcp.tools().isEmpty());
        QVERIFY(click("closeMcpEditor"));
        // Cancel a hanging real HTTP reader on editor close.
        QVERIFY(click("addMcpButton")); QVERIFY(choose("mcpKind",1)); QVERIFY(input("mcpEndpoint",endpoint));
        QVERIFY(control({{"mode","hang"},{"fail_on","tools/list"}})); QVERIFY(click("probeMcpButton")); QTest::qWait(200);
        QVERIFY(mcp.editorBusy()); QVERIFY(click("closeMcpEditor")); QTRY_VERIFY(!mcp.editorOpen()); QVERIFY(control({{"mode","normal"},{"fail_on","initialize"}}));
        // Backend change closes the active editor and clears its sensitive inputs.
        QVERIFY(click("addMcpButton")); QVERIFY(input("mcpKey",secret));
        tasks.setBaseUrl(QUrl(qEnvironmentVariable("FORGEPILOT_MCP_ALTERNATE"))); QTRY_VERIFY(!mcp.loading()); QTest::qWait(900);
        QVERIFY(mcp.items().isEmpty()); QVERIFY(!mcp.editorOpen()); QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
        tasks.setBaseUrl(QUrl(base)); QTRY_VERIFY(!mcp.loading());
        QVERIFY(click("addMcpButton")); QVERIFY(choose("mcpKind",1)); QVERIFY(input("mcpEndpoint",endpoint));
        QVERIFY(choose("mcpAuth",2)); QVERIFY(input("mcpHeader","X-API-Key")); QVERIFY(input("mcpKey",secret));
        QVERIFY(control({{"auth","api_key"},{"expected_secret",secret}})); QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QVERIFY(select("fetch")); QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),6); QTRY_VERIFY(!mcp.loading());
        const auto apiId=mcp.items().first().toMap().value("server_id").toString();
        QVERIFY(edit(apiId)); QTRY_VERIFY(!mcp.editorBusy());
        QVERIFY(input("mcpHeader","x-api-key")); QVERIFY(mcp.hasSecret("api_key"));
        QVERIFY(input("mcpKey",replacement)); QVERIFY(input("mcpHeader","X-New-Key")); QVERIFY(!mcp.hasSecret("api_key"));
        QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
        QVERIFY(click("probeMcpButton")); QVERIFY(mcp.editorError().contains("重新填写密钥"));
        QVERIFY(input("mcpHeader","X-API-Key")); QVERIFY(mcp.hasSecret("api_key"));
        QVERIFY(choose("mcpAuth",1)); QVERIFY(!mcp.hasSecret("api_key"));
        QVERIFY(click("closeMcpEditor")); QVERIFY(remove(apiId)); QTRY_VERIFY(mcp.items().isEmpty()); QTRY_VERIFY(!mcp.loading());
        // Local executable, Unicode/space argv, password environment and visible default cwd.
        QVERIFY(click("addMcpButton")); QVERIFY(choose("mcpKind",2)); QVERIFY(input("mcpName","本地项目说明"));
        QVERIFY(input("mcpCommand",qEnvironmentVariable("FORGEPILOT_MCP_PYTHON")));
        for(const auto &arg:QStringList{"-I","-u",qEnvironmentVariable("FORGEPILOT_MCP_SCRIPT"),"参数 含空格","独立参数"}) {
            const auto count=mcp.draft().value("connection").toMap().value("args").toList().size();
            QVERIFY(click("addMcpArg"));
            if(mcp.draft().value("connection").toMap().value("args").toList().size()!=count+1) capture("local-arg-failure");
            QTRY_COMPARE(mcp.draft().value("connection").toMap().value("args").toList().size(),count+1);
            QTRY_VERIFY(ready("mcpArg_"+QString::number(count))); QVERIFY(input("mcpArg_"+QString::number(count),arg));
        }
        QVERIFY(click("addMcpEnv")); QVERIFY(input("mcpEnvName_0","MCP_UNUSED")); QVERIFY(input("mcpEnvValue_0","discarded-value"));
        QVERIFY(click("addMcpEnv")); QVERIFY(input("mcpEnvName_1","MCP_FIXTURE")); QVERIFY(input("mcpEnvValue_1",secret));
        QVERIFY(click("removeMcpEnv_0"));
        QCOMPARE(mcp.draft().value("connection").toMap().value("env_names").toList(),QVariantList{"MCP_FIXTURE"});
        QTRY_VERIFY(find("mcpEnvValue_0")->property("text").toString().isEmpty());
        QTRY_VERIFY(find("mcpEnvValue_0")->property("placeholderText").toString().contains("本次已填写"));
        QVERIFY(find("mcpEffectiveCwd")->property("text").toString().contains("agent.venv"));
        QVERIFY(find("mcpEnvSensitive_0")->property("checked").toBool());
        for(const auto &entry:QList<QPair<QString,QString>>{{"DEBUG","true"},{"PYTHONUNBUFFERED","1"}}) {
            const auto index=mcp.draft().value("connection").toMap().value("env_names").toList().size();
            const auto suffix=QString::number(index);
            QVERIFY(click("addMcpEnv")); QVERIFY(input("mcpEnvName_"+suffix,entry.first));
            QVERIFY(find("mcpEnvSensitive_"+suffix)->property("checked").toBool());
            QVERIFY(input("mcpEnvValue_"+suffix,entry.second)); QVERIFY(click("mcpEnvSensitive_"+suffix));
        }
        QVERIFY(input("mcpEnvName_1","DEBUG_RENAMED")); QVERIFY(find("mcpEnvSensitive_1")->property("checked").toBool());
        QVERIFY(input("mcpEnvName_1","DEBUG")); QVERIFY(input("mcpEnvValue_1","true")); QVERIFY(click("mcpEnvSensitive_1"));
        window->resize(860,640); QTRY_COMPARE(window->width(),860); QVERIFY(capture("local-small"));
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE_WITH_TIMEOUT(mcp.state(),QString("discovered"),10000); QVERIFY(select("project_info"));
        QVERIFY(click("mcpEnvSensitive_1")); QCOMPARE(mcp.state(),QString("editing")); QVERIFY(!mcp.canSave());
        QVERIFY(click("mcpEnvSensitive_1")); mcp.updateList("public_env_names",QVariantList{"debug","PYTHONUNBUFFERED"});
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),7); QTRY_VERIFY(!mcp.loading());
        const auto local=mcp.items().first().toMap().value("server_id").toString(); QVERIFY(edit(local)); QTRY_VERIFY(!mcp.editorBusy());
        QCOMPARE(mcp.kind(),2); QVERIFY(mcp.hasSecret("env:MCP_FIXTURE")); QVERIFY(find("mcpEnvValue_0")->property("text").toString().isEmpty());
        QVERIFY(find("mcpEnvSensitive_0")->property("checked").toBool()); QVERIFY(!find("mcpEnvSensitive_1")->property("checked").toBool());
        QVERIFY(!find("mcpEnvSensitive_2")->property("checked").toBool());
        QVERIFY(find("mcpEnvValue_1")->property("text").toString().isEmpty()); QVERIFY(find("mcpEnvValue_2")->property("text").toString().isEmpty());
        QVERIFY(click("removeMcpEnv_0"));
        QCOMPARE(mcp.draft().value("connection").toMap().value("public_env_names").toList(),(QVariantList{"DEBUG","PYTHONUNBUFFERED"}));
        for(int i=0;i<2;++i) QVERIFY(click("removeMcpEnv_0"));
        QVERIFY(mcp.draft().value("connection").toMap().value("env_names").toList().isEmpty());
        QVERIFY(mcp.draft().value("connection").toMap().value("public_env_names").toList().isEmpty());
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered")); QVERIFY(click("saveMcpButton"));
        QTRY_COMPARE(saved.count(),8); QTRY_VERIFY(!mcp.loading());
        QVERIFY(!http("GET","/api/v1/mcp/servers/"+local).value("has_secret").toBool());
        QVERIFY(remove(local)); QTRY_VERIFY(mcp.items().isEmpty()); QTRY_VERIFY(!mcp.loading());
        // Only the exact preset names are default-selected; user types just a Key.
        QVERIFY(click("addMcpButton")); QCOMPARE(mcp.kind(),0); QVERIFY(input("mcpKey",secret));
        QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered")); QVERIFY(mcp.canSave());
        QCOMPARE(mcp.draft().value("selected_tools").toList().size(),2); QVERIFY(capture("tavily-small"));
        QVERIFY(select("tavily-extract")); QVERIFY(control({{"probe_error","mcp_timeout"}}));
        QVERIFY(click("probeMcpButton")); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(mcp.editorError().contains("超时"));
        QCOMPARE(find("mcpKey")->property("text").toString(),secret);
        QVERIFY(control({{"probe_error",QJsonValue::Null}})); QVERIFY(click("probeMcpButton")); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QCOMPARE(mcp.draft().value("selected_tools").toList(),QVariantList{"tavily-search"});
        QVERIFY(select("tavily-extract"));
        QVERIFY(click("saveMcpButton")); QTRY_COMPARE(saved.count(),9); QTRY_VERIFY(!mcp.loading()); QCOMPARE(mcp.items().size(),1);
        QVERIFY(capture("saved-small"));
        // A late nonempty list from the old backend cannot populate the new empty backend.
        QVERIFY(control({{"delay_list",true}})); mcp.refresh(); QTest::qWait(150);
        tasks.setBaseUrl(QUrl(qEnvironmentVariable("FORGEPILOT_MCP_ALTERNATE"))); QTRY_VERIFY(!mcp.loading()); QTest::qWait(900);
        QVERIFY(mcp.items().isEmpty()); tasks.setBaseUrl(QUrl(base)); QTRY_VERIFY(!mcp.loading()); QCOMPARE(mcp.items().size(),1);
        QVERIFY(click("closeSettingsButton")); QVERIFY(!mcp.editorOpen());
        QVERIFY(mcp.draft().isEmpty()); QVERIFY(mcp.items().isEmpty()); QVERIFY(find("mcpKey")->property("text").toString().isEmpty());
        QDirIterator settingsFiles(settingsDirectory.path(),QDir::Files,QDirIterator::Subdirectories);
        while(settingsFiles.hasNext()) { QFile input(settingsFiles.next()); QVERIFY(input.open(QIODevice::ReadOnly)); const auto bytes=input.readAll(); QVERIFY(!bytes.contains(secret.toUtf8())); QVERIFY(!bytes.contains(replacement.toUtf8())); }
        QVERIFY2(warnings.isEmpty(),qPrintable(warnings.join('\n')));
    }
private:
    static void captureFailure(QQuickWindow *window) { window->grabWindow().save(qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT")+"/scroll-failure.png"); }
};
QTEST_MAIN(McpBackendTest)
#include "tst_mcp_backend.moc"
