#include "AgentApiClient.h"
#include "TaskController.h"
#include "ConversationController.h"
#include "McpController.h"
#include "JsonHttp.h"
#include "Protocol.h"
#include "SseClient.h"
#include "ConversationStreamClient.h"
#include "SseParser.h"
#include <QJsonArray>
#include <QJsonDocument>
#include <QPointer>
#include <QTemporaryDir>
#include <QFile>
#include <QDir>
#include <QTcpServer>
#include <QTcpSocket>
#include <QtTest>
#include <functional>
#include <memory>
using namespace Qt::Literals::StringLiterals;

#include "HttpFixture.h"

QByteArray conversationSse(int revision,const QString &type,QJsonObject values={},
                           const QString &conversation=A,const QString &request=B)
{
    values["type"]=type; values["conversation_id"]=conversation;
    values["request_id"]=request; values["revision"]=revision;
    return "id: "+request.toUtf8()+':'+QByteArray::number(revision)+"\r\nevent: "+type.toUtf8()
        +"\r\ndata: "+QJsonDocument(values).toJson(QJsonDocument::Compact)+"\r\n\r\n";
}

// Local HTTP fixture: count actual requests and hold individual responses to order races.
class PollSource : public Source {
public:
    QString cid=A,rid=B;
    QJsonObject state{{"conversation_id",A},{"request_id",B},{"mode","task"},{"status","running"},{"task_id",B}};
    QList<Request> polls,details;
    int histories=0,records=0,stops=0;
    bool holdPoll=false,holdDetail=false,holdStop=false,removed=false;
    Request stop;
    PollSource() {
        handle=[this](const Request &r) {
            if(r.path.endsWith("/stop")) {
                ++stops; stop=r; state["status"]="cancelled";
                if(!holdStop) json(r,state);
            } else if(r.method=="DELETE") { removed=true; json(r,{{"conversation_id",cid},{"deleted",true}}); }
            else if(r.path.endsWith("/mcp-tools")) {
                ++records; json(r,{{"conversation_id",cid},{"request_id",rid},
                    {"items",QJsonArray{QJsonObject{{"sequence",records},{"status",state.value("status")}}}}});
            } else if(r.path.contains("/conversations/") && r.path.contains("/requests/")) {
                polls.append(r);
                state["context_notice"]=QString("notice-%1").arg(polls.size());
                state["tool_activity"]=QJsonObject{{"sequence",polls.size()},{"status","running"}};
                if(!holdPoll) json(r,state);
            } else if(r.path.startsWith("/api/v1/conversations?")) { ++histories; json(r,{{"items",QJsonArray{}},{"total",0}}); }
            else if(r.path.startsWith("/api/v1/conversations/")) {
                details.append(r); if(!holdDetail) json(r,detail(r.path.contains("before=")));
            } else if(r.path.startsWith("/api/v1/tasks?")) json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.endsWith("/events")) stream(r,{},false);
            else json(r,snapshot(B));
        };
    }
    QJsonObject detail(bool older=false) const {
        const bool running=state.value("status")=="running";
        QJsonArray messages{QJsonObject{{"message_id",older?1:2},{"conversation_id",cid},{"request_id",rid},
            {"role","user"},{"content",older?"older":"question"}}};
        if(!running && !older) messages.append(QJsonObject{{"message_id",3},{"conversation_id",cid},{"request_id",rid},
            {"role","assistant"},{"content","final"}});
        return {{"conversation_id",cid},{"title",cid},{"messages",messages},{"next_before",older?0:2},
            {"context_notice",state.value("context_notice")},{"last_request",state},
            {"active_request",running?QJsonValue(state):QJsonValue::Null}};
    }
};

class CommunicationTest : public QObject {
    Q_OBJECT
private slots:
    void feedbackWireRequires202() {
        Source source;
        source.handle=[](const Request &r) { Source::json(r,snapshot(A,"planning"),r.body.value("plan_version").toInt()==7?202:200); };
        AgentApiClient api; QVERIFY(api.setBaseUrl(source.url()));
        QSignalSpy ok(&api,&AgentApiClient::responseReceived), errors(&api,&AgentApiClient::requestError);
        const QString text=QString::fromUtf8("  保留公开接口\n\n😀  ");
        api.feedbackTask(A,7,text); QTRY_COMPARE(ok.size(),1);
        QCOMPARE(ok[0][1].toString(),QString("feedback"));
        QCOMPARE(source.requests.first().method,QByteArray("POST"));
        QCOMPARE(source.requests.first().path,("/api/v1/tasks/"+A+"/feedback").toUtf8());
        QCOMPARE(source.requests.first().body,QJsonObject({{"plan_version",7},{"feedback",text}}));
        api.feedbackTask(A,8,text); QTRY_COMPARE(errors.size(),1); QCOMPARE(ok.size(),1);
        QCOMPARE(errors[0][4].toString(),QString("invalid_response"));
    }
    void feedbackValidation_data() {
        QTest::addColumn<QString>("text"); QTest::addColumn<int>("count"); QTest::addColumn<bool>("valid");
        QTest::newRow("empty")<<QString{}<<0<<false;
        QTest::newRow("spaces")<<QString(" \t\n\r")<<4<<false;
        QTest::newRow("python-space")<<QString(QChar(0x1c))<<1<<false;
        QTest::newRow("nul")<<(QString("a")+QChar(0)+"b")<<3<<false;
        QTest::newRow("chinese-limit")<<QString(4000,QChar(0x610f))<<4000<<true;
        QTest::newRow("chinese-over")<<QString(4001,QChar(0x610f))<<4001<<false;
        QTest::newRow("non-bmp-limit")<<QString::fromUtf8("😀").repeated(4000)<<4000<<true;
        QTest::newRow("non-bmp-over")<<QString::fromUtf8("😀").repeated(4001)<<4001<<false;
        QTest::newRow("raw-length")<<(QString(4000,' ')+"x")<<4001<<false;
    }
    void feedbackMalformed202KeepsDraft_data() {
        QTest::addColumn<QString>("invalidField");
        QTest::newRow("cursor")<<QString("last_event_id");
        QTest::newRow("task")<<QString("task_id");
        QTest::newRow("version")<<QString("plan_version");
    }
    void feedbackMalformed202KeepsDraft() {
        QFETCH(QString,invalidField);
        Source source; int posts=0,gets=0;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.method=="GET") { ++gets; Source::json(r,snapshot()); }
            else {
                ++posts; auto body=snapshot(A,"planning",4);
                body[invalidField]=invalidField=="plan_version"?QJsonValue(2):QJsonValue("invalid");
                Source::json(r,body,202);
            }
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_VERIFY(c.canFeedback());
        c.openPlanFeedback(); c.setFeedbackDraft("Keep this unconfirmed draft"); c.submitPlanFeedback(c.feedbackDraft());
        QTRY_COMPARE(gets,2); QTRY_VERIFY(c.canFeedback()); QCOMPARE(posts,1);
        QVERIFY(c.feedbackOpen()); QCOMPARE(c.feedbackDraft(),QString("Keep this unconfirmed draft"));
        QVERIFY(c.feedbackNotice().contains("完整执行记录")); QCOMPARE(c.planVersion(),1);
    }
    void feedbackValidation() {
        QFETCH(QString,text); QFETCH(int,count); QFETCH(bool,valid);
        Source source; source.handle=[](const Request &r) {
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.method=="GET") Source::json(r,snapshot());
            else Source::json(r,snapshot(A,"planning",4),202);
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_VERIFY(c.canFeedback());
        QCOMPARE(c.feedbackCharacterCount(text),count); QCOMPARE(c.feedbackValidationError(text).isEmpty(),valid);
        c.openPlanFeedback(); c.setFeedbackDraft(text); c.submitPlanFeedback(text);
        if(valid) { QTRY_VERIFY(!c.busy()); QCOMPARE(source.requests.size(),3); QCOMPARE(source.requests.last().body.value("feedback").toString(),text); QVERIFY(!c.feedbackOpen()); }
        else { QCOMPARE(source.requests.size(),2); QVERIFY(c.feedbackOpen()); QCOMPARE(c.feedbackDraft(),text); }
    }
    void feedbackLostResponseNeverAcknowledgesDraft_data() {
        QTest::addColumn<QString>("state"); QTest::addColumn<int>("version"); QTest::addColumn<bool>("conflict");
        QTest::newRow("accepted-planning")<<QString("planning")<<1<<false;
        QTest::newRow("other-client-v2")<<QString("waiting_approval")<<2<<false;
        QTest::newRow("explicit-retry")<<QString("waiting_approval")<<1<<false;
        QTest::newRow("stale-409")<<QString("waiting_approval")<<2<<true;
    }
    void feedbackLostResponseNeverAcknowledgesDraft() {
        QFETCH(QString,state); QFETCH(int,version); QFETCH(bool,conflict);
        Source source; int gets=0,posts=0; Request feedback;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.method=="GET") { ++gets; Source::json(r,gets==1?snapshot():snapshot(A,state,4,version)); }
            else { ++posts; feedback=r; }
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_VERIFY(c.canFeedback());
        c.openPlanFeedback(); c.setFeedbackDraft("Keep API"); c.submitPlanFeedback(c.feedbackDraft());
        QVERIFY(!c.canApprove()); QVERIFY(!c.canFeedback()); QVERIFY(c.canCancel());
        c.submitPlanFeedback("repeat"); QTRY_COMPARE(posts,1);
        if(conflict) Source::json(feedback,{{"error",QJsonObject{{"code","stale_plan"},{"message","Review current plan"}}}},409);
        else feedback.socket->disconnectFromHost();
        QTRY_COMPARE(gets,2); QTRY_COMPARE(c.state(),state); QTRY_VERIFY(!c.busy());
        QTRY_COMPARE(c.planVersion(),version); QTRY_VERIFY(c.canFeedback() || state=="planning");
        QCOMPARE(posts,1); QCOMPARE(c.feedbackSourceVersion(),1);
        QCOMPARE(c.feedbackDraft(),QString("Keep API")); QVERIFY(c.feedbackOpen());
        if(!conflict) QVERIFY(c.feedbackNotice().contains("完整执行记录"));
        if(version==2) {
            QVERIFY(c.feedbackNotice().contains("版本已变化")); c.submitPlanFeedback(c.feedbackDraft()); QCOMPARE(posts,1);
            c.closePlanFeedback(); c.openPlanFeedback(); QCOMPARE(c.feedbackSourceVersion(),2); QVERIFY(c.feedbackDraft().isEmpty());
        } else if(state=="waiting_approval") {
            QTRY_VERIFY(c.canFeedback()); c.submitPlanFeedback(c.feedbackDraft()); QTRY_COMPARE(posts,2);
            Source::json(feedback,{{"error",QJsonObject{{"code","service_unavailable"},{"message","Try later"}}}},503);
            QTRY_VERIFY(c.canFeedback()); QVERIFY(c.feedbackNotice().contains("完整执行记录"));
            QCOMPARE(c.feedbackDraft(),QString("Keep API"));
            c.submitPlanFeedback(c.feedbackDraft()); QTRY_COMPARE(posts,3);
            QCOMPARE(feedback.body.value("plan_version").toInt(),1);
            Source::json(feedback,snapshot(A,"planning",5),202); QTRY_VERIFY(!c.feedbackOpen());
        } else { QVERIFY(c.feedbackReplanning()); QVERIFY(!c.canFeedback()); QVERIFY(c.canCancel()); }
    }
    void feedbackLate202AndReplayDoNotRegressVersion() {
        Source source; Request feedback,stream,refresh;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/events")) { stream=r; Source::stream(r,{},false); }
            else if(r.method=="POST") feedback=r;
            else if(refresh.socket) Source::json(r,snapshot(A,"waiting_approval",6,2));
            else { refresh=r; Source::json(r,snapshot()); }
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_VERIFY(c.canFeedback()); QTRY_VERIFY(stream.socket);
        c.openPlanFeedback(); c.setFeedbackDraft("first"); c.submitPlanFeedback(c.feedbackDraft()); QTRY_VERIFY(feedback.socket);
        stream.socket->write(sseEvent(1,"plan_created",{{"plan_version",1},{"plan",QJsonObject{{"summary","old"}}}})
            +sseEvent(2,"task_state_changed",{{"state","waiting_approval"}})
            +sseEvent(3,"approval_required",{{"plan_version",1}})
            +sseEvent(4,"task_state_changed",{{"from","waiting_approval"},{"state","planning"}})
            +sseEvent(5,"plan_created",{{"plan_version",2},{"plan",QJsonObject{{"summary","new"}}}}));
        QTRY_COMPARE(c.planVersion(),2); QCOMPARE(c.state(),QString("planning"));
        QVERIFY(c.feedbackReplanning()); QVERIFY(!c.feedbackShowsPreviousPlan()); QVERIFY(!c.canApprove());
        stream.socket->write(sseEvent(6,"task_state_changed",{{"state","waiting_approval"}}));
        QTRY_COMPARE(c.planVersion(),2); QTRY_COMPARE(c.state(),QString("waiting_approval"));
        QCOMPARE(c.feedbackDraft(),QString("first")); QVERIFY(c.feedbackSending());
        Source::json(feedback,snapshot(A,"planning",4,1),202);
        QTRY_VERIFY(!c.busy()); QVERIFY(!c.feedbackOpen()); QCOMPARE(c.planVersion(),2); QCOMPARE(c.state(),QString("waiting_approval"));
        c.openPlanFeedback(); c.setFeedbackDraft("second");
        stream.socket->write(sseEvent(4,"task_state_changed",{{"from","waiting_approval"},{"state","planning"}}));
        c.refreshTask(); QTRY_COMPARE(source.requests.size(),4);
        QCOMPARE(c.planVersion(),2); QCOMPARE(c.feedbackDraft(),QString("second")); QCOMPARE(c.feedbackSourceVersion(),2);
    }
    void feedbackTaskSwitchBudgetHistoryAndStop() {
        Source source; Request late; int feedbacks=0,cancels=0;
        source.handle=[&](const Request &r) {
            const QString id=r.path.contains(B.toUtf8())?B:A;
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.path.endsWith("/feedback")) { ++feedbacks; late=r; }
            else if(r.path.endsWith("/cancel")) { ++cancels; Source::json(r,snapshot(id,"cancelled",6)); }
            else Source::json(r,snapshot(id));
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_VERIFY(c.canFeedback());
        c.openPlanFeedback(); c.setFeedbackDraft("A draft"); c.submitPlanFeedback(c.feedbackDraft()); QTRY_COMPARE(feedbacks,1);
        c.selectTask(B); QTRY_VERIFY(c.canFeedback()); QVERIFY(!c.feedbackOpen()); c.openPlanFeedback(); c.setFeedbackDraft("B draft");
        Source::json(late,snapshot(A,"planning",4),202);
        c.refreshTask(); QTRY_COMPARE(source.requests.size(),6); QCOMPARE(c.taskId(),B); QCOMPARE(c.feedbackDraft(),QString("B draft"));
        c.submitPlanFeedback(c.feedbackDraft()); QTRY_COMPARE(feedbacks,2); QVERIFY(c.canCancel());
        c.cancelTask(); QTRY_COMPARE(cancels,1); QTRY_COMPARE(c.state(),QString("cancelled"));
        Source::json(late,snapshot(B,"planning",4),202); QTRY_VERIFY(!c.busy()); QCOMPARE(c.state(),QString("cancelled"));
        source.handle=[](const Request &r) {
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else { auto body=snapshot(); body.insert("steps",30); Source::json(r,body); }
        };
        c.selectTask(A); QTRY_VERIFY(c.canApprove()); QVERIFY(!c.canFeedback()); c.openPlanFeedback(); QVERIFY(!c.feedbackOpen());
        c.viewHistoryTask(A); QTRY_COMPARE(c.state(),QString("waiting_approval")); QVERIFY(!c.canFeedback());
    }
    void initialPlanningIsNotFeedback() {
        Source source; source.handle=[](const Request &r) {
            if(r.path.endsWith("/events")) Source::stream(r,sseEvent(1,"task_state_changed",{{"from","analyzing"},{"state","planning"}}),false);
            else Source::json(r,snapshot(A,"planning",2,1)); // Initial plan arrived before MCP cleanup.
        };
        TaskController c; c.setBaseUrl(source.url()); c.selectTask(A); QTRY_COMPARE(c.state(),QString("planning"));
        QVERIFY(!c.feedbackReplanning()); QVERIFY(c.canCancel()); QVERIFY(!c.canFeedback());
    }
    void requestPollingTenTicksLeavesMessagesAlone() {
        PollSource source; source.state.remove("task_id"); source.state["mode"]="chat";
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QCOMPARE(c.messages()->count(),1);
        QSignalSpy resets(c.messages(),&QAbstractItemModel::modelReset);
        QSignalSpy updates(c.messages(),&QAbstractItemModel::dataChanged);
        QSignalSpy inserts(c.messages(),&QAbstractItemModel::rowsInserted);
        c.showMcpTools(B); QTRY_COMPARE(source.records,1);
        // This fixture returns a non-SSE 200 response for /events.  R7 treats
        // that as a protocol error, performs one authoritative detail refresh,
        // then falls back to the existing status poller.
        QTRY_COMPARE(source.details.size(),2);
        QTRY_COMPARE_WITH_TIMEOUT(c.toolActivity().value("sequence").toInt(),10,12000);
        QCOMPARE(source.polls.size(),10); QCOMPARE(source.details.size(),2); QCOMPARE(source.histories,0);
        QCOMPARE(c.contextNotice(),QString("notice-10")); QCOMPARE(c.messages()->count(),1);
        QCOMPARE(resets.count(),0); QCOMPARE(updates.count(),0); QCOMPARE(inserts.count(),0);
        QVERIFY(!c.loading()); QVERIFY(!c.busy()); QCOMPARE(c.activeMode(),QString("chat"));
        QTRY_COMPARE(source.records,11); QTRY_VERIFY(!c.mcpToolsLoading());
        QCOMPARE(c.mcpTools().first().toMap().value("sequence").toInt(),11);
        qInfo("R7 running: 10 status GETs, initial plus protocol-recovery detail GETs, 0 history GETs, 0 message mutations, 11 MCP detail GETs");
        c.refresh(); QTRY_COMPARE(source.details.size(),3); QTRY_VERIFY(!c.loading());
        c.refresh(true); QTRY_COMPARE(source.details.size(),4); QTRY_VERIFY(!c.loading());
        QCOMPARE(c.messages()->count(),2); QVERIFY(!c.hasMoreMessages());
        source.state["status"]="completed";
        QTRY_VERIFY(!c.active()); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(c.messages()->count(),3);
        QCOMPARE(source.details.size(),5); QTRY_COMPARE(source.histories,1);
        QTRY_VERIFY(!c.mcpToolsLoading());
        QCOMPARE(c.mcpTools().first().toMap().value("status").toString(),QString("completed"));
        qInfo("R7 final: 1 additional detail GET and 1 history GET; manual refresh and older page each add 1 detail GET");
    }
    void terminalPollingAndTaskSignalDeduplicate_data() {
        QTest::addColumn<QString>("state"); QTest::addColumn<bool>("signalFirst");
        for(const auto &state:{"completed","failed","cancelled","interrupted"})
            for(bool signalFirst:{false,true}) QTest::newRow(qPrintable(QString("%1-%2").arg(state).arg(signalFirst)))<<QString(state)<<signalFirst;
    }
    void terminalPollingAndTaskSignalDeduplicate() {
        QFETCH(QString,state); QFETCH(bool,signalFirst);
        PollSource source; source.holdPoll=true;
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),1);
        if(signalFirst) tasks.taskFinished(state);
        source.state["status"]=state; source.holdDetail=true;
        Source::json(source.polls.last(),source.state);
        QTRY_COMPARE(source.details.size(),2); QVERIFY(!c.active());
        tasks.taskFinished(state); tasks.taskFinished(state);
        Source::json(source.details.last(),source.detail()); QTRY_VERIFY(!c.loading());
        QTRY_COMPARE(source.histories,1); QCOMPARE(source.polls.size(),1); QCOMPARE(source.details.size(),2);
        QCOMPARE(c.messages()->get(1).value("content").toString(),QString("final"));
    }
    void taskSignalBeforePublicationAndLateTaskBinding() {
        PollSource source; source.holdPoll=true; source.state.remove("task_id");
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QVERIFY(tasks.taskId().isEmpty());
        QTRY_COMPARE(source.polls.size(),1);
        source.state["task_id"]=B; Source::json(source.polls.last(),source.state);
        QTRY_COMPARE(tasks.taskId(),B); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        QCOMPARE(source.details.size(),1);
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),2);
        Source::json(source.polls.last(),source.state); QTest::qWait(50); QVERIFY(c.active());
        QCOMPARE(source.details.size(),1); QCOMPARE(source.histories,0);
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),3);
        source.state["status"]="completed"; Source::json(source.polls.last(),source.state);
        QTRY_VERIFY(!c.active()); QTRY_VERIFY(!c.loading()); QCOMPARE(source.details.size(),2);
        QTRY_COMPARE(source.histories,1);
    }
    void terminalResponseCanFirstBindTask() {
        PollSource source; source.holdPoll=true; source.state.remove("task_id");
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(source.polls.size(),1);
        source.state["task_id"]=B; source.state["status"]="completed";
        Source::json(source.polls.last(),source.state);
        QTRY_COMPARE(tasks.taskId(),B); QTRY_VERIFY(!c.loading()); QVERIFY(!c.active());
        QCOMPARE(source.details.size(),2); QTRY_COMPARE(source.histories,1);
    }
    void stopWhileStatusIsInFlight() {
        PollSource source; source.holdPoll=true; source.holdStop=true; source.state["mode"]="chat"; source.state.remove("task_id");
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(source.polls.size(),1);
        QVERIFY(!c.busy()); QVERIFY(!c.loading()); c.stopAnswer(); QTRY_COMPARE(source.stops,1); QVERIFY(c.busy());
        auto stale=source.state; stale["status"]="completed"; stale["context_notice"]="obsolete";
        Source::json(source.polls.first(),stale); QTest::qWait(50); QVERIFY(c.busy()); QVERIFY(c.active());
        QVERIFY(c.contextNotice()!=QString("obsolete"));
        Source::json(source.stop,source.state); QTRY_VERIFY(!c.busy()); QTRY_VERIFY(!c.active()); QTRY_VERIFY(!c.loading());
        QCOMPARE(source.details.size(),2); QTRY_COMPARE(source.histories,1);
    }
    void stalePollCannotReleaseNewPoll_data() {
        QTest::addColumn<QString>("change");
        for(const auto &change:{"conversation","reselect","server","request"}) QTest::newRow(change)<<QString(change);
    }
    void stalePollCannotReleaseNewPoll() {
        QFETCH(QString,change);
        PollSource source,other; source.holdPoll=true; other.holdPoll=true;
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),1);
        const auto old=source.polls.first(); auto stale=source.state; stale["status"]="completed"; stale["context_notice"]="obsolete";
        PollSource *current=&source;
        if(change=="server") { tasks.setBaseUrl(other.url()); current=&other; }
        else if(change=="conversation") { source.cid=B; source.state["conversation_id"]=B; }
        else if(change=="request") { source.rid=A; source.state["request_id"]=A; }
        if(change=="request") c.refresh(); else c.selectConversation(current->cid);
        QTRY_VERIFY(!c.loading()); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        tasks.taskFinished("completed"); const int expected=current==&source?2:1;
        QTRY_COMPARE(current->polls.size(),expected);
        Source::json(old,stale); QTest::qWait(60); tasks.taskFinished("completed"); QTest::qWait(60);
        QCOMPARE(current->polls.size(),expected); QVERIFY(c.active()); QVERIFY(c.contextNotice()!=QString("obsolete"));
        auto fresh=current->state; fresh["context_notice"]="fresh"; Source::json(current->polls.last(),fresh);
        QTRY_COMPARE(c.contextNotice(),QString("fresh")); tasks.taskFinished("completed");
        QTRY_COMPARE(current->polls.size(),expected+1);
    }
    void pollingErrorsAndDeletion() {
        PollSource source; source.holdPoll=true;
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),1); Source::json(source.polls.last(),{},503);
        QTRY_VERIFY(!c.error().isEmpty()); QVERIFY(c.active()); QVERIFY(!c.busy()); QCOMPARE(source.details.size(),1);
        c.clearError(); tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),2);
        auto invalid=source.state; invalid["request_id"]=A; Source::json(source.polls.last(),invalid);
        QTRY_VERIFY(c.error().contains("无效")); QVERIFY(c.active());
        c.clearError(); tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),3);
        Source::json(source.polls.last(),{},404); QTRY_VERIFY(c.conversationId().isEmpty());
        QCOMPARE(c.messages()->count(),0); QVERIFY(tasks.taskId().isEmpty()); QTRY_VERIFY(!c.historyLoading());
        c.selectConversation(A); QVERIFY(c.conversationId().isEmpty());
    }
    void deletingConversationDiscardsHeldPoll() {
        PollSource source; source.holdPoll=true;
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),1);
        c.showMcpTools(B); QTRY_VERIFY(!c.mcpToolsLoading()); c.prepareDelete(A,"delete"); QTRY_VERIFY(!c.deleteLoading());
        c.confirmDelete(); QTRY_VERIFY(source.removed); QTRY_VERIFY(!c.deleting()); QVERIFY(c.conversationId().isEmpty());
        Source::json(source.polls.last(),source.state); QTest::qWait(60);
        QVERIFY(c.conversationId().isEmpty()); QVERIFY(c.mcpToolsRequest().isEmpty()); QVERIFY(c.mcpTools().isEmpty());
        QVERIFY(tasks.taskId().isEmpty()); QCOMPARE(c.messages()->count(),0);
    }
    void terminalRefreshSupersedesHeldMcpRecords() {
        PollSource source; source.holdPoll=true;
        QList<Request> records; const auto normal=source.handle;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/mcp-tools")) records.append(r); else normal(r);
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QTRY_COMPARE(tasks.state(),QString("waiting_approval"));
        c.showMcpTools(B); QTRY_COMPARE(records.size(),1);
        tasks.taskFinished("completed"); QTRY_COMPARE(source.polls.size(),1);
        source.state["status"]="completed"; Source::json(source.polls.last(),source.state);
        QTRY_COMPARE(records.size(),2); QTRY_VERIFY(!c.loading()); QVERIFY(c.mcpToolsLoading());
        Source::json(records.first(),{{"conversation_id",A},{"request_id",B},{"items",QJsonArray{QJsonObject{{"status","running"}}}}});
        QTest::qWait(60); QVERIFY(c.mcpToolsLoading()); QVERIFY(c.mcpTools().isEmpty());
        Source::json(records.last(),{{"conversation_id",A},{"request_id",B},{"items",QJsonArray{QJsonObject{{"status","succeeded"}}}}});
        QTRY_VERIFY(!c.mcpToolsLoading()); QCOMPARE(c.mcpTools().first().toMap().value("status").toString(),QString("succeeded"));
    }
    void uncertainAdmissionUsesDetailRecoveryWithoutReposting() {
        PollSource source; bool accepted=false; int posts=0; const auto normal=source.handle;
        source.handle=[&](const Request &r) {
            if(r.method=="POST") {
                ++posts; source.rid=r.body.value("request_id").toString(); source.state["request_id"]=source.rid;
                Source::json(r,{},503);
            } else if(r.path=="/api/v1/conversations/"+A.toUtf8() && !accepted) {
                source.details.append(r); Source::json(r,{{"conversation_id",A},{"messages",QJsonArray{}}});
            } else normal(r);
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); c.sendMessage("uncertain admission");
        QTRY_COMPARE(posts,1); QTRY_VERIFY(!c.busy()); QTRY_COMPARE(source.details.size(),2); QTRY_VERIFY(!c.loading());
        QVERIFY(c.retryPending()); QVERIFY(!c.active()); accepted=true;
        QTRY_VERIFY(c.active()); QVERIFY(!c.retryPending()); QCOMPARE(source.details.size(),3);
        QTRY_VERIFY(!source.polls.isEmpty()); QCOMPARE(posts,1); QCOMPARE(source.details.size(),3);
    }
    void terminalOnOlderPageStillFetchesLatestMessages() {
        PollSource source;
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QVERIFY(c.hasMoreMessages());
        source.state["status"]="completed"; c.refresh(true);
        QTRY_COMPARE(source.details.size(),3); QTRY_VERIFY(!c.loading()); QVERIFY(!c.active());
        QCOMPARE(c.messages()->count(),3); QCOMPARE(c.messages()->get(2).value("content").toString(),QString("final"));
        QVERIFY(!c.hasMoreMessages()); QTRY_COMPARE(source.histories,1);
        QVERIFY(source.details[1].path.contains("before=")); QVERIFY(!source.details[2].path.contains("before="));
    }
    void mcpCallRecordsLateResponsesAndSafeLinks() {
        Source source; QList<Request> delayed;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/mcp-tools")) { delayed.append(r); return; }
            if(r.path.startsWith("/api/v1/conversations/")) {
                Source::json(r,{{"conversation_id",r.path.endsWith(A.toUtf8())?A:B},{"title","tools"},{"messages",QJsonArray{}},
                    {"last_request",QJsonObject{{"request_id",A},{"tool_activity",QJsonObject{{"status","running"},{"server_name","查询"},{"tool_name","search"}}}}}});
            } else Source::json(r,{{"items",QJsonArray{}},{"total",0}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading());
        QCOMPARE(c.toolActivity().value("status").toString(),QString("running"));
        c.showMcpTools(A); QTRY_COMPARE(delayed.size(),1);
        c.showMcpTools(B); QTRY_COMPARE(delayed.size(),2);
        Source::json(delayed[1],{{"conversation_id",A},{"request_id",B},{"items",QJsonArray{QJsonObject{
            {"sequence",1},{"status","succeeded"},{"sources",QJsonArray{
                QJsonObject{{"title","safe"},{"url","https://doc.qt.io/qt-6/"}},
                QJsonObject{{"title","bad"},{"url","javascript:alert(1)"}}}}}}}});
        QTRY_VERIFY(!c.mcpToolsLoading()); QCOMPARE(c.mcpTools().size(),1);
        QCOMPARE(c.mcpTools().first().toMap().value("sources").toList().size(),1);
        Source::json(delayed[0],{{"conversation_id",A},{"request_id",A},{"items",QJsonArray{}}});
        QTest::qWait(80); QCOMPARE(c.mcpToolsRequest(),B); QCOMPARE(c.mcpTools().size(),1);
        c.showMcpTools(A); QTRY_COMPARE(delayed.size(),3); c.selectConversation(B);
        Source::json(delayed[2],{{"conversation_id",A},{"request_id",A},{"items",QJsonArray{QJsonObject{{"status","failed"}}}}});
        QTest::qWait(80); QVERIFY(c.mcpToolsRequest().isEmpty()); QVERIFY(c.mcpTools().isEmpty());
        for(const auto &url:QStringList{"file:///C:/private","javascript:alert(1)","data:text/html,x","https://user:key@example.com","https://example.com/a b","https://example.com\\evil"})
            QVERIFY(!ConversationController::safeSourceUrl(url));
        QVERIFY(ConversationController::safeSourceUrl("https://doc.qt.io/qt-6/"));
    }
    void mcpCallRecordsFailuresIdentityAndClose() {
        Source source; QList<Request> delayed;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/mcp-tools")) delayed.append(r);
            else if(r.path.startsWith("/api/v1/conversations/")) Source::json(r,{{"conversation_id",A},{"messages",QJsonArray{}}});
            else Source::json(r,{{"items",QJsonArray{}},{"total",0}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks); c.selectConversation(A);
        QTRY_VERIFY(!c.loading()); c.showMcpTools(A); QTRY_COMPARE(delayed.size(),1);
        Source::json(delayed[0],{{"error",QJsonObject{{"message","读取失败"}}}},503);
        QTRY_VERIFY(!c.mcpToolsLoading()); QVERIFY(c.mcpToolsError().contains("读取失败"));
        c.refreshMcpTools(); QTRY_COMPARE(delayed.size(),2);
        Source::json(delayed[1],{{"conversation_id",B},{"request_id",A},{"items",QJsonArray{}}});
        QTRY_VERIFY(!c.mcpToolsLoading()); QVERIFY(c.mcpToolsError().contains("无效"));
        c.refreshMcpTools(); QTRY_COMPARE(delayed.size(),3); c.closeMcpTools();
        Source::json(delayed[2],{{"conversation_id",A},{"request_id",A},{"items",QJsonArray{QJsonObject{{"status","cancelled"}}}}});
        QTest::qWait(80); QVERIFY(c.mcpTools().isEmpty()); QVERIFY(c.mcpToolsError().isEmpty());
        c.showMcpTools(A); QTRY_COMPARE(delayed.size(),4);
        Source other; other.handle=[](const Request &r){Source::json(r,{{"items",QJsonArray{}},{"total",0}});};
        tasks.setBaseUrl(other.url()); Source::json(delayed[3],{{"conversation_id",A},{"request_id",A},{"items",QJsonArray{QJsonObject{{"status","succeeded"}}}}});
        QTest::qWait(80); QVERIFY(c.mcpTools().isEmpty()); QVERIFY(c.mcpToolsRequest().isEmpty());
    }
    void mcpValidationAndSecretErrors_data() {
        QTest::addColumn<QString>("errorCode");
        QTest::newRow("authentication") << QString("mcp_auth");
        QTest::newRow("unknown-error") << QString("unexpected");
    }
    void mcpValidationAndSecretErrors() {
        QFETCH(QString,errorCode);
        Source source; bool recovered=false; QList<QJsonObject> probes;
        const QString secret="fixture-private-do-not-echo";
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/servers")) Source::json(r,{{"items",QJsonArray{}},{"disabled",false}});
            else if(r.path.endsWith("/presets")) Source::json(r,{{"items",QJsonArray{}}});
            else if(r.path.endsWith("/probe")) {
                probes.append(r.body);
                if(!recovered) Source::json(r,{{"error",QJsonObject{{"code",errorCode},{"message",secret}}}},422);
                else Source::json(r,{{"connection",r.body.value("connection")},{"catalog_hash","sha256:fixture"},
                    {"tools",QJsonArray{QJsonObject{{"name","query"},{"compatible",true}}}},{"default_selected_tools",QJsonArray{"query"}}});
            } else Source::json(r,{{"error",QJsonObject{{"code","unexpected"}}}},404);
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); McpController mcp(&tasks); mcp.open(); QTRY_VERIFY(!mcp.loading());
        mcp.edit(); mcp.setKind(1); const auto id=mcp.serverId();
        for(const auto &endpoint:QStringList{"http://public.example/mcp","https://user:pass@example.com/mcp","https://example.com/mcp?key=value","https://example.com/mcp#fragment"}) {
            mcp.update("endpoint",endpoint); const auto count=source.requests.size(); mcp.probe();
            QVERIFY(!mcp.editorError().isEmpty()); QCOMPARE(source.requests.size(),count); QCOMPARE(mcp.serverId(),id);
        }
        mcp.update("endpoint","http://127.0.0.1/mcp"); mcp.update("auth_type","bearer");
        mcp.setSecret("api_key","******"); mcp.probe(); QVERIFY(mcp.editorError().contains("掩码"));
        mcp.setSecret("api_key",secret); const auto draft=mcp.draft();
        QSignalSpy cleared(&mcp,&McpController::clearSensitiveInputs);
        mcp.probe(); QTRY_VERIFY(!mcp.editorBusy()); QCOMPARE(probes.size(),1);
        QVERIFY(!mcp.editorError().isEmpty()); QVERIFY(!mcp.editorError().contains(secret));
        QVERIFY(!QJsonDocument::fromVariant(mcp.draft()).toJson().contains(secret.toUtf8()));
        QCOMPARE(mcp.draft(),draft); QCOMPARE(mcp.serverId(),id); QCOMPARE(mcp.state(),QString("editing"));
        QCOMPARE(mcp.credentialState().value("api_key").toString(),QString("replacement"));
        QVERIFY(!mcp.canSave()); QVERIFY(!mcp.uncertain()); QVERIFY(!mcp.pendingSave()); QCOMPARE(cleared.size(),0);

        // F7 keeps the candidate in this editor so an explicit retry needs no re-entry.
        recovered=true; mcp.probe(); QTRY_COMPARE(mcp.state(),QString("discovered"));
        QCOMPARE(probes.size(),2); QCOMPARE(probes[0].value("server_id").toString(),id); QCOMPARE(probes[1],probes[0]);
        QCOMPARE(probes[1].value("secret_updates").toObject().value("api_key").toString(),secret);
        QVERIFY(mcp.editorError().isEmpty()); QVERIFY(mcp.canSave()); QCOMPARE(cleared.size(),0);

        mcp.closeEditor(); QVERIFY(mcp.draft().isEmpty()); QVERIFY(mcp.serverId().isEmpty());
        QVERIFY(mcp.credentialState().isEmpty()); QCOMPARE(cleared.size(),1);
        mcp.edit(); mcp.setKind(1); mcp.update("endpoint","http://127.0.0.1/mcp"); mcp.update("auth_type","bearer");
        const auto count=source.requests.size(); mcp.probe();
        QVERIFY(mcp.editorError().contains("填写")); QCOMPARE(source.requests.size(),count); QCOMPARE(probes.size(),2);
    }
    void mcpUnknownSaveMustResolveSameId() {
        Source source; bool missing=false; QString savedId; int puts=0;
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/servers")) Source::json(r,{{"items",QJsonArray{}},{"disabled",false}});
            else if(r.path.endsWith("/presets")) Source::json(r,{{"items",QJsonArray{}}});
            else if(r.path.endsWith("/probe")) Source::json(r,{{"connection",r.body.value("connection")},{"catalog_hash","sha256:fixture"},
                {"tools",QJsonArray{QJsonObject{{"name","query"},{"compatible",true}}}},{"default_selected_tools",QJsonArray{}}});
            else if(r.method=="PUT") { ++puts; savedId=QString::fromUtf8(r.path.mid(r.path.lastIndexOf('/')+1)); Source::json(r,{{"error",QJsonObject{{"code","api_internal"}}}},503); }
            else Source::json(r,{{"error",QJsonObject{{"code",missing?"unknown_mcp_server":"api_internal"}}}},missing?404:503);
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); McpController mcp(&tasks); mcp.open(); QTRY_VERIFY(!mcp.loading());
        mcp.edit(); mcp.setKind(1); mcp.update("endpoint","http://127.0.0.1/mcp"); const auto id=mcp.serverId();
        mcp.probe(); QTRY_COMPARE(mcp.state(),QString("discovered")); mcp.selectTool("query",true); mcp.save();
        QTRY_VERIFY(mcp.uncertain()); QTRY_VERIFY(!mcp.editorBusy()); QCOMPARE(savedId,id); QCOMPARE(puts,1);
        mcp.probe(); mcp.save(); mcp.update("endpoint","https://another.example/mcp");
        QCOMPARE(mcp.draft().value("connection").toMap().value("endpoint").toString(),QString("http://127.0.0.1/mcp"));
        QCOMPARE(puts,1); QVERIFY(mcp.editorError().contains("确认保存状态"));
        missing=true; mcp.resolveSave(); QTRY_VERIFY(!mcp.editorBusy()); QVERIFY(!mcp.uncertain()); QCOMPARE(mcp.serverId(),id); QCOMPARE(mcp.state(),QString("editing"));
    }
    void mcpClosedAndBackendGenerations() {
        Source oldSource,newSource; QList<Request> delayed;
        oldSource.handle=[&](const Request &r) {
            if(r.path.endsWith("/servers")) Source::json(r,{{"items",QJsonArray{}},{"disabled",false}});
            else if(r.path.endsWith("/presets")) Source::json(r,{{"items",QJsonArray{}}});
            else delayed.append(r);
        };
        newSource.handle=[](const Request &r) { Source::json(r,{{"items",QJsonArray{}},{"disabled",true}}); };
        TaskController tasks; tasks.setBaseUrl(oldSource.url()); McpController mcp(&tasks); mcp.open(); QTRY_VERIFY(!mcp.loading());
        mcp.edit(); mcp.setKind(1); mcp.update("endpoint","http://127.0.0.1/mcp"); mcp.probe(); mcp.probe();
        QTRY_COMPARE(delayed.size(),1); mcp.closeEditor(); mcp.edit(); const auto next=mcp.serverId();
        Source::json(delayed.first(),{{"tools",QJsonArray{QJsonObject{{"name","late"},{"compatible",true}}}}});
        QTest::qWait(40); QCOMPARE(mcp.serverId(),next); QVERIFY(mcp.tools().isEmpty());
        mcp.setKind(1); mcp.update("endpoint","http://127.0.0.1/mcp"); mcp.probe(); QTRY_COMPARE(delayed.size(),2);
        tasks.setBaseUrl(newSource.url()); QTRY_VERIFY(!mcp.loading()); QVERIFY(mcp.disabled()); QVERIFY(!mcp.editorOpen());
        Source::json(delayed.last(),{{"tools",QJsonArray{}}}); QTest::qWait(40); QVERIFY(mcp.tools().isEmpty());
        const auto count=newSource.requests.size(); mcp.edit(); mcp.probe(); mcp.save(); mcp.test(A,1); mcp.setEnabled(A,1,true);
        QCOMPARE(newSource.requests.size(),count); QVERIFY(!mcp.editorOpen());
    }
    void jsonFailures_data() {
        QTest::addColumn<QByteArray>("body");
        QTest::addColumn<int>("status");
        QTest::addColumn<bool>("truncated");
        QTest::addColumn<QString>("code");
        QTest::addColumn<QString>("message");
        QTest::newRow("malformed") << QByteArray("{") << 200 << false << QString("invalid_response") << QString("响应不完整");
        QTest::newRow("array") << QByteArray("[]") << 200 << false << QString("invalid_response") << QString("响应不完整");
        QTest::newRow("truncated") << QByteArray("{}") << 200 << true << QString("network_error") << QString("响应不完整");
        QTest::newRow("redirect") << QByteArray("{}") << 302 << false << QString("http_error") << QString("对话请求失败");
        QTest::newRow("server-error") << QByteArray("{\"error\":{\"code\":\"busy\",\"message\":\"try later\"}}") << 409 << false << QString("busy") << QString("try later");
    }
    void jsonFailures() {
        QFETCH(QByteArray,body); QFETCH(int,status); QFETCH(bool,truncated); QFETCH(QString,code); QFETCH(QString,message);
        Source source, redirect;
        redirect.handle=[](const Request &r){Source::json(r,{{"items",QJsonArray{}}});};
        source.handle=[&](const Request &r){
            r.socket->write("HTTP/1.1 "+QByteArray::number(status)+" Result\r\nContent-Type: application/json\r\nConnection: close\r\nLocation: "+redirect.url().toEncoded()+"\r\nContent-Length: "+QByteArray::number(body.size()+(truncated?10:0))+"\r\n\r\n"+body);
            r.socket->disconnectFromHost();
        };
        AgentApiClient api; QVERIFY(api.setBaseUrl(source.url()));
        QSignalSpy errors(&api,&AgentApiClient::requestError), replies(&api,&AgentApiClient::responseReceived);
        const auto request=api.getTask(A);
        QTRY_COMPARE(errors.size(),1); QCOMPARE(replies.size(),0);
        QCOMPARE(errors[0][0].toULongLong(),request); QCOMPARE(errors[0][3].toInt(),status); QCOMPARE(errors[0][4].toString(),code);
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.refreshHistory(); QTRY_VERIFY(!c.historyLoading()); QVERIFY2(c.error().contains(message),qPrintable(c.error()));
        QTest::qWait(30); QCOMPARE(source.requests.size(),2); QCOMPARE(redirect.requests.size(),0); QCOMPARE(errors.size(),1);
    }
    void mcpOversizeResponse_data() {
        QTest::addColumn<bool>("advertised");
        QTest::newRow("headers-before-body") << true;
        QTest::newRow("unannounced-stream") << false;
    }
    void mcpOversizeResponse() {
        QFETCH(bool,advertised);
        Source source; QPointer<QTcpSocket> peer;
        constexpr qsizetype limit=512*1024;
        source.handle=[&](const Request &r) {
            peer=r.socket;
            QByteArray headers="HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n";
            if(advertised) headers+="Content-Length: "+QByteArray::number(limit+1)+"\r\n";
            r.socket->write(headers+"\r\n");
            if(!advertised) r.socket->write(QByteArray(limit+1,'x'));
            // Leave the connection open: both limits must abort without EOF or
            // the advertised body, instead of waiting for a transfer timeout.
        };
        QNetworkAccessManager network; int finished=0; bool exceeded=false;
        JsonHttp::send(network,this,"GET",source.url(),{},limit,[&](QNetworkReply*,const JsonHttp::Response &r) {
            ++finished; exceeded=r.exceededLimit; QVERIFY(r.body.isEmpty());
        });
        QTRY_COMPARE(finished,1); QVERIFY(exceeded);
        QTRY_VERIFY(!peer || peer->state()==QAbstractSocket::UnconnectedState);
        QCOMPARE(source.requests.size(),1);
    }
    void jsonSizeLimits_data() {
        QTest::addColumn<int>("size");
        QTest::newRow("conversation-boundary") << 16*1024*1024;
        QTest::newRow("conversation-overflow") << 16*1024*1024+1;
        QTest::newRow("task-overflow") << 64*1024*1024+1;
    }
    void jsonSizeLimits() {
        QFETCH(int,size);
        QByteArray body="{\"items\":[],\"padding\":\"";
        body.append(QByteArray(size-body.size()-2,'a')); body.append("\"}");
        Source source;
        source.handle=[&](const Request &r){
            r.socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n");
            r.socket->write(body); r.socket->disconnectFromHost();
        };
        AgentApiClient api; QVERIFY(api.setBaseUrl(source.url()));
        QSignalSpy errors(&api,&AgentApiClient::requestError), replies(&api,&AgentApiClient::responseReceived);
        api.getTask(A); QTRY_COMPARE(errors.size()+replies.size(),1);
        if(size>64*1024*1024) { QCOMPARE(errors.size(),1); QCOMPARE(errors[0][4].toString(),QString("response_limit")); }
        else { QCOMPARE(replies.size(),1); QVERIFY(!replies[0][3].toJsonObject().value("padding").toString().isEmpty()); }
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.refreshHistory(); QTRY_VERIFY(!c.historyLoading());
        if(size>16*1024*1024) QVERIFY2(c.error().contains("16 MiB"),qPrintable(c.error()));
        else QVERIFY2(c.error().isEmpty(),qPrintable(c.error()));
        QTest::qWait(30); QCOMPARE(source.requests.size(),2); QCOMPARE(errors.size()+replies.size(),1);
    }
    void fragmentedJsonPreservesUnicode() {
        Source source;
        const QString note="中文🙂";
        const auto body=QJsonDocument(QJsonObject{{"items",QJsonArray{}},{"note",note}}).toJson(QJsonDocument::Compact);
        source.handle=[&](const Request &r){
            r.socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n");
            auto *timer=new QTimer(r.socket); auto position=std::make_shared<qsizetype>(0);
            connect(timer,&QTimer::timeout,r.socket,[=]{
                r.socket->write(body.mid((*position)++,1));
                if(*position==body.size()) { timer->stop(); r.socket->disconnectFromHost(); }
            });
            timer->start(1);
        };
        AgentApiClient api; QVERIFY(api.setBaseUrl(source.url()));
        QSignalSpy errors(&api,&AgentApiClient::requestError), replies(&api,&AgentApiClient::responseReceived);
        api.getTask(A); QTRY_COMPARE(replies.size(),1); QCOMPARE(errors.size(),0);
        QCOMPARE(replies[0][3].toJsonObject().value("note").toString(),note);
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.refreshHistory(); QTRY_VERIFY(!c.historyLoading()); QVERIFY2(c.error().isEmpty(),qPrintable(c.error()));
        QCOMPARE(source.requests.size(),2);
    }
    void memoryScopeChangeIgnoresLateListsAndSummaries() {
        Source source; Request oldMemories,oldSummary;
        source.handle=[&](const Request &r) {
            if(r.path.startsWith("/api/v1/memories")) {
                if(r.path.contains("FixtureA")) oldMemories=r;
                else Source::json(r,{{"items",QJsonArray{QJsonObject{{"memory_id",B},{"content","项目乙偏好"}}}},{"total",1}});
            } else if(r.path.endsWith("/context")) {
                if(r.path.contains(A.toUtf8())) oldSummary=r;
                else Source::json(r,{{"conversation_id",B},{"revision",2},{"enabled",false},{"summary",QJsonValue::Null}});
            } else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else {
                const auto id=r.path.endsWith(A.toUtf8())?A:B;
                Source::json(r,{{"conversation_id",id},{"repository_path",id==A?"C:/FixtureA":"C:/FixtureB"},{"messages",QJsonArray{}}});
            }
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks); auto *m=c.memory();
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); m->open(); QTRY_VERIFY(oldMemories.socket); QTRY_VERIFY(oldSummary.socket);
        c.selectConversation(B); QTRY_VERIFY(!c.loading()); QTRY_VERIFY(!m->loading());
        QCOMPARE(m->items().first().toMap().value("content").toString(),QString("项目乙偏好"));
        Source::json(oldMemories,{{"items",QJsonArray{QJsonObject{{"memory_id",A},{"content","项目甲旧偏好"}}}}});
        Source::json(oldSummary,{{"conversation_id",A},{"revision",1},{"summary",QJsonObject{{"content","旧摘要"}}}});
        QTest::qWait(60); QCOMPARE(m->items().size(),1); QCOMPARE(m->items().first().toMap().value("memory_id").toString(),B);
        QCOMPARE(m->summary().value("conversation_id").toString(),B); QVERIFY(m->summary().value("summary").toMap().isEmpty());
    }
    void memoryMutationCapturesScopeAndRejectsDuplicateClicks() {
        Source source; QList<Request> mutations;
        source.handle=[&](const Request &r) {
            if(r.method=="POST" || r.method=="PATCH") mutations.append(r);
            else if(r.path.startsWith("/api/v1/memories") || r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.endsWith("/context")) Source::json(r,{{"summary",QJsonValue::Null},{"enabled",true},{"revision",0}});
            else Source::json(r,{{"conversation_id",r.path.endsWith(A.toUtf8())?A:B},{"repository_path","C:/Fixture"},{"messages",QJsonArray{}}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks); auto *m=c.memory(); QSignalSpy saved(m,&MemoryController::saved);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); m->open(); QTRY_VERIFY(!m->loading());
        m->save({},"甲的记忆","project",0,true); m->save({},"重复点击","global",0,false);
        QTRY_COMPARE(mutations.size(),1); QVERIFY(m->busy()); QCOMPARE(mutations[0].body.value("source_conversation_id").toString(),A);
        QCOMPARE(mutations[0].body.value("repository_path").toString(),QString("C:/Fixture"));
        c.selectConversation(B); QTRY_VERIFY(!c.loading()); QTRY_VERIFY(!m->loading());
        Source::json(mutations[0],{{"memory_id",A}},201); QTest::qWait(50); QCOMPARE(saved.size(),0); QVERIFY(!m->busy());
        m->save(B,"编辑已查看版本","global",7,false); QTRY_COMPARE(mutations.size(),2);
        QCOMPARE(mutations[1].method,"PATCH"); QCOMPARE(mutations[1].body.value("revision").toInt(),7);
        QVERIFY(!mutations[1].body.contains("source_conversation_id")); QVERIFY(!mutations[1].body.contains("repository_path"));
        Source::json(mutations[1],{{"error",QJsonObject{{"code","memory_conflict"},{"message","记忆已变化，请刷新"}}}},409);
        QTRY_VERIFY(!m->busy()); QVERIFY(m->error().contains("记忆已变化")); QCOMPARE(saved.size(),0);
    }
    void deleteCurrentClearsTaskSubscriptionAndPagination() {
        Source source; Request stream, removal; bool removed=false;
        source.handle=[&](const Request &r) {
            if(r.method=="DELETE") removal=r;
            else if(r.path.startsWith("/api/v1/conversations?")) Source::json(r,{{"items",removed?QJsonArray{}:QJsonArray{QJsonObject{{"conversation_id",A},{"title","待删除"}}}},{"total",removed?0:1}});
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{snapshot(B)}},{"total",1}});
            else if(r.path.endsWith("/events")) { stream=r; Source::stream(r,sseEvent(1,"message",{{"role","assistant"},{"content","执行日志"}},B),false); }
            else if(r.path.contains("/tasks/")) Source::json(r,snapshot(B));
            else Source::json(r,{{"conversation_id",A},{"title","待删除"},{"messages",QJsonArray{QJsonObject{{"message_id",1},{"role","user"},{"content","删除后不能复现"}}}},
                {"active_request",QJsonObject{{"request_id",A},{"mode","task"},{"task_id",B},{"status","running"}}}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.refreshHistory(); QTRY_COMPARE(tasks.state(),"waiting_approval"); QTRY_COMPARE(tasks.eventSequence(),1);
        QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.historyTotal(),1);
        c.prepareDelete(A,"待删除"); QTRY_VERIFY(!c.deleteLoading()); QVERIFY(c.deleteActive());
        c.confirmDelete(); c.confirmDelete(); QTRY_VERIFY(removal.socket); QVERIFY(c.deleting());
        QCOMPARE(removal.path,"/api/v1/conversations/"+A.toUtf8()+"?stop_active=true");
        QVERIFY(tasks.taskId().isEmpty()); QCOMPARE(tasks.eventSequence(),0);
        QCOMPARE(tasks.traces()->count(),0); QCOMPARE(tasks.tasks().size(),0);
        if(stream.socket) stream.socket->write(sseEvent(2,"message",{{"content","迟到消息"}},B));
        removed=true; Source::json(removal,{{"conversation_id",A},{"deleted",true}});
        QTRY_VERIFY(!c.deleting()); QTRY_VERIFY(!c.historyLoading()); QVERIFY(c.conversationId().isEmpty());
        QCOMPARE(c.messages()->count(),0); QCOMPARE(c.history().size(),0); QCOMPARE(c.historyTotal(),0); QVERIFY(!c.historyHasMore());
        const auto count=source.requests.size(); tasks.reconnectEvents(); tasks.refreshTasks(); c.selectConversation(A); c.refresh();
        QTest::qWait(80); QCOMPARE(source.requests.size(),count); QVERIFY(tasks.taskId().isEmpty());
    }
    void deleteAfterSwitchDiscardsLateSendAndOldHistory() {
        Source source; Request post, oldDetail, oldPage, removal; int aReads=0; bool removed=false;
        source.handle=[&](const Request &r) {
            if(r.method=="DELETE") removal=r;
            else if(r.method=="POST") post=r;
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.path.contains("/tasks/")) Source::json(r,snapshot(B));
            else if(r.path.startsWith("/api/v1/conversations?")) {
                if(!removed) oldPage=r;
                else Source::json(r,{{"items",QJsonArray{QJsonObject{{"conversation_id",B},{"title","保留"}}}},{"total",1}});
            } else if(r.path.endsWith(A.toUtf8()) && ++aReads==2) oldDetail=r;
            else {
                auto id=r.path.endsWith(A.toUtf8())?A:B;
                QJsonObject detail{{"conversation_id",id},{"title",id==A?"待删除":"保留"},{"messages",QJsonArray{QJsonObject{{"message_id",1},{"role","assistant"},{"content",id}}}}};
                if(id==B) detail["active_request"]=QJsonObject{{"task_id",B},{"request_id",B},{"mode","task"},{"status","running"}};
                Source::json(r,detail);
            }
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); c.sendMessage("在途请求"); QTRY_VERIFY(post.socket);
        c.refresh(); QTRY_VERIFY(oldDetail.socket); c.refreshHistory(); QTRY_VERIFY(oldPage.socket);
        c.prepareDelete(A,"待删除"); QTRY_VERIFY(!c.deleteLoading()); c.confirmDelete(); QTRY_VERIFY(removal.socket);
        c.selectConversation(B); QTRY_COMPARE(c.messages()->count(),1); QTRY_VERIFY(tasks.canApprove()); QCOMPARE(c.messages()->get(0).value("content").toString(),B);
        removed=true; Source::json(removal,{{"conversation_id",A},{"deleted",true}}); QTRY_VERIFY(!c.deleting()); QTRY_VERIFY(!c.historyLoading());
        Source::json(post,{{"task_id",A},{"status","running"}});
        Source::json(oldDetail,{{"conversation_id",A},{"title","旧回复"},{"messages",QJsonArray{}}});
        Source::json(oldPage,{{"items",QJsonArray{QJsonObject{{"conversation_id",A}}}},{"total",100},{"next_cursor",A}});
        QTest::qWait(80); QCOMPARE(c.conversationId(),B); QCOMPARE(c.title(),QString("保留")); QCOMPARE(c.historyTotal(),1);
        QCOMPARE(c.history().size(),1); QVERIFY(!c.historyHasMore()); QVERIFY(!c.retryPending()); QCOMPARE(tasks.taskId(),B); QVERIFY(tasks.canApprove());
        c.selectConversation(A); QCOMPARE(c.conversationId(),B); c.retrySend(); QTest::qWait(30);
        QCOMPARE(std::count_if(source.requests.cbegin(),source.requests.cend(),[](const Request &r){return r.method=="POST";}),1);
    }
    void deleteRequiresExplicitStopAndCanRetryUncertainResponse() {
        Source source; QList<Request> deletes; bool removed=false;
        source.handle=[&](const Request &r) {
            if(r.method=="DELETE") {
                deletes.append(r);
                if(deletes.size()==1) Source::json(r,{{"error",QJsonObject{{"code","conversation_busy"},{"message","请停止任务并删除"}}}},409);
                else if(deletes.size()==2) Source::json(r,{},503);
                else { removed=true; Source::json(r,{{"conversation_id",A},{"deleted",true}}); }
            } else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.startsWith("/api/v1/conversations?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else Source::json(r,{{"conversation_id",A},{"title","运行状态变化"},{"messages",QJsonArray{}}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); c.prepareDelete(A,"运行状态变化"); QTRY_VERIFY(!c.deleteLoading());
        QVERIFY(!c.deleteActive()); c.confirmDelete(); QTRY_VERIFY(!c.deleting());
        QCOMPARE(deletes.size(),1); QVERIFY(c.deleteActive()); QVERIFY(!c.deleteError().isEmpty()); QVERIFY(!removed);
        QCOMPARE(deletes[0].path,"/api/v1/conversations/"+A.toUtf8());
        c.confirmDelete(); QTRY_COMPARE(deletes.size(),2); QTRY_VERIFY(!c.deleting());
        QVERIFY(c.deleteError().contains("安全重试")); QVERIFY(!removed);
        c.confirmDelete(); QTRY_COMPARE(deletes.size(),3); QTRY_VERIFY(!c.deleting());
        QCOMPARE(deletes[1].path,deletes[2].path); QVERIFY(deletes[2].path.endsWith("?stop_active=true"));
        QVERIFY(removed); QVERIFY(c.conversationId().isEmpty()); QTRY_VERIFY(!c.historyLoading());
    }
    void deletionRefreshesPagesWithoutDuplicates() {
        Source source; bool removed=false; int more=0;
        const QString C="33333333-3333-4333-8333-333333333333";
        auto item=[](const QString &id){return QJsonObject{{"conversation_id",id},{"title",id}};};
        source.handle=[&](const Request &r) {
            if(r.method=="DELETE") {removed=true; Source::json(r,{{"conversation_id",B},{"deleted",true}});}
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.startsWith("/api/v1/conversations?")) {
                if(r.path.contains("before=")) { ++more; Source::json(r,{{"items",QJsonArray{item(C)}},{"total",removed?2:3}}); }
                else Source::json(r,{{"items",removed?QJsonArray{item(A)}:QJsonArray{item(A),item(B)}},{"total",removed?2:3},{"next_cursor","p1.fixture"}});
            } else Source::json(r,{{"conversation_id",r.path.endsWith(A.toUtf8())?A:B},{"messages",QJsonArray{}}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.refreshHistory(); QTRY_COMPARE(c.history().size(),2); QTRY_VERIFY(!c.loading());
        c.refreshHistory(true); QTRY_COMPARE(c.history().size(),3); QCOMPARE(more,1);
        c.prepareDelete(B,"分页边界"); QTRY_VERIFY(!c.deleteLoading()); c.confirmDelete(); QTRY_VERIFY(!c.deleting());
        QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.conversationId(),A); QCOMPARE(c.historyTotal(),2); QVERIFY(c.historyHasMore());
        c.refreshHistory(true); QTRY_COMPARE(c.history().size(),2); QCOMPARE(more,2); QVERIFY(!c.historyHasMore());
        QCOMPARE(c.history()[0].toMap().value("conversation_id").toString(),A); QCOMPARE(c.history()[1].toMap().value("conversation_id").toString(),C);
    }
    void conversationSwitchIgnoresLateReplies() {
        Source source; Request old;
        source.handle=[&](const Request&r) {
            if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path=="/api/v1/conversations/"+A.toUtf8()) old=r;
            else Source::json(r,{{"conversation_id",B},{"title","第二个对话"},{"messages",QJsonArray{QJsonObject{{"message_id",2},{"role","assistant"},{"content","第二个回答"}}}}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(old.socket);
        c.selectConversation(B); QTRY_COMPARE(c.messages()->count(),1);
        QCOMPARE(c.title(),QString("第二个对话"));
        Source::json(old,{{"conversation_id",A},{"title","旧对话"},{"messages",QJsonArray{QJsonObject{{"message_id",1},{"role","assistant"},{"content","旧回答"}}}}});
        QTest::qWait(50); QCOMPARE(c.messages()->get(0).value("content").toString(),QString("第二个回答"));
        c.refresh(); QTest::qWait(50); QCOMPARE(c.messages()->count(),1);
        QVERIFY(std::any_of(source.requests.cbegin(),source.requests.cend(),[](const Request &r){ return r.path.contains("conversation_id="+B.toUtf8()); }));
        QVERIFY(tasks.taskId().isEmpty());
    }
    void conversationRetryUsesSameIdentityAndPayload() {
        Source source; QList<Request> posts; bool accepted=false;
        source.handle=[&](const Request&r) {
            if(r.method=="POST") { posts.append(r); if(posts.size()==1) Source::json(r,{},503); }
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path=="/api/v1/conversations/"+A.toUtf8()) {
                QJsonObject detail{{"conversation_id",A},{"title","重试测试"},{"messages",QJsonArray{}}};
                if(accepted) detail["last_request"]=QJsonObject{{"request_id",posts.last().body.value("request_id")},{"status","completed"}};
                Source::json(r,detail);
            } else Source::json(r,{{"items",QJsonArray{}}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); QCOMPARE(c.mode(),"chat");
        QCOMPARE(c.toolMode(),QString("auto")); c.setToolMode("invalid"); QCOMPARE(c.toolMode(),QString("auto"));
        c.setToolMode("web"); QCOMPARE(c.toolMode(),QString("web"));
        c.sendMessage("原问题"); c.sendMessage("不能重复发送");
        QTRY_COMPARE(posts.size(),1); QTRY_VERIFY(!c.busy()); QVERIFY(c.retryPending());
        c.sendMessage("不能替换原请求"); QCOMPARE(posts.size(),1);
        c.retrySend(); QTRY_COMPARE(posts.size(),2); QCOMPARE(posts[0].body,posts[1].body);
        QVERIFY(Protocol::validTaskId(posts[0].body.value("request_id").toString()));
        QCOMPARE(posts[0].body.value("mode").toString(),"chat");
        QCOMPARE(posts[0].body.value("tool_mode").toString(),"web");
        accepted=true;
        Source::json(posts[1],{{"request_id",posts[1].body.value("request_id")},{"status","completed"}});
        QTRY_VERIFY(!c.retryPending()); QTRY_VERIFY(!c.loading()); QVERIFY(!c.active()); QVERIFY(tasks.taskId().isEmpty());
    }
    void conversationLateMutationDoesNotSelectOldTask() {
        Source source; Request pending;
        source.handle=[&](const Request&r) {
            if(r.method=="POST") pending=r;
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else Source::json(r,{{"conversation_id",r.path.endsWith(A.toUtf8())?A:B},{"title","对话"},{"messages",QJsonArray{}},{"repository_path","C:/fixture"}});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController c(&tasks);
        c.selectConversation(A); QTRY_VERIFY(!c.loading()); c.setMode("task"); c.sendMessage("修改代码");
        QTRY_VERIFY(pending.socket); c.selectConversation(B); QTRY_VERIFY(!c.loading());
        Source::json(pending,{{"status","running"},{"task_id",A}});
        QTest::qWait(50); QCOMPARE(c.conversationId(),B); QCOMPARE(c.mode(),"chat"); QVERIFY(tasks.taskId().isEmpty()); QVERIFY(!c.active());
    }
    void disconnectedReadDoesNotImplyAMutation() {
        Source reservation;
        const auto url = reservation.url();
        reservation.close();
        AgentApiClient api; QVERIFY(api.setBaseUrl(url));
        QSignalSpy errors(&api, &AgentApiClient::requestError);
        api.listTasks();
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors[0][4].toString(), "network_error");
        QVERIFY(errors[0][5].toString().contains("后端"));
        QVERIFY(!errors[0][5].toString().contains("可能已经执行"));
        api.createTask("C:/fixture", "fixture");
        QTRY_COMPARE(errors.size(), 2);
        QCOMPARE(errors[1][4].toString(), "network_error");
        QVERIFY(errors[1][5].toString().contains("可能已经执行"));
    }
    void lateHistoryPageKeepsRealtimeState() {
        Source source; Request listing, stream;
        source.handle=[&](const Request&r) {
            if(r.path.startsWith("/api/v1/tasks?")) listing=r;
            else if(r.path.endsWith("/events")) { stream=r; Source::stream(r,{},false); }
            else Source::json(r,snapshot());
        };
        TaskController controller; controller.setBaseUrl(source.url()); controller.selectTask(A);
        QTRY_COMPARE(controller.state(),"waiting_approval");
        controller.refreshTasks(); QTRY_VERIFY(listing.socket);
        QTRY_VERIFY(stream.socket);
        // Replay preceding records before delivering the newer state on the real TCP stream.
        stream.socket->write(sseEvent(1,"message")+sseEvent(2,"message")+sseEvent(3,"message")+
            sseEvent(4,"task_state_changed",{{"state","modifying"}}));
        QTRY_COMPARE(controller.state(),"modifying");
        Source::json(listing,{{"items",QJsonArray{snapshot()}},{"total",1}});
        QTRY_COMPARE(controller.tasks().size(),1);
        QCOMPARE(controller.tasks()[0].toMap().value("state").toString(),"modifying");
        QCOMPARE(controller.state(),"modifying");
    }
    void historyPaginationAndOldTraceReplyIsolation() {
        Source source;
        source.handle=[](const Request&r) {
            if(r.path.startsWith("/api/v1/tasks?")) {
                const bool more=r.path.contains("before=");
                Source::json(r,{{"items",more?QJsonArray{snapshot(B)}:QJsonArray{snapshot(A)}},{"total",2},{"next_cursor",more?QJsonValue{}:QJsonValue(A)}});
            } else if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(!r.path.contains("/trace")) Source::json(r,snapshot(r.path.contains(B.toUtf8())?B:A));
        };
        TaskController controller; controller.setBaseUrl(source.url());
        controller.refreshTasks(); QTRY_COMPARE(controller.tasks().size(),1); QVERIFY(controller.historyHasMore());
        controller.loadMoreTasks(); QTRY_COMPARE(controller.tasks().size(),2); QVERIFY(!controller.historyHasMore());
        QCOMPARE(controller.historyTotal(),2); QVERIFY(source.requests[1].path.contains("before="+A.toUtf8()));
        controller.viewHistoryTask(A); QTRY_COMPARE(controller.state(),"waiting_approval");
        controller.refreshTrace(); QTRY_VERIFY(source.requests.last().path.contains("/trace?")); const auto old=source.requests.last();
        controller.viewHistoryTask(B); QTRY_COMPARE(controller.state(),"waiting_approval");
        Source::json(old,{{"items",QJsonArray{QJsonObject{{"sequence",1},{"kind","old-secret-free-fixture"}}}}});
        QTest::qWait(40); QCOMPARE(controller.storedTrace()->count(),0); QVERIFY(controller.traceDetailText().isEmpty());
        controller.refreshTrace(); QTRY_VERIFY(source.requests.last().path.contains(B.toUtf8()+"/trace?"));
        Source::json(source.requests.last(),{{"items",QJsonArray{QJsonObject{{"sequence",7},{"kind","llm_request"}}}},{"next_cursor",7}});
        QTRY_COMPARE(controller.storedTrace()->count(),1); QVERIFY(controller.traceHasMore());
        controller.loadTraceDetail(7); QTRY_VERIFY(source.requests.last().path.endsWith("/trace/7"));
        Source::json(source.requests.last(),{{"task_id",B},{"sequence",7},{"data",QJsonObject{{"fixture","current"}}}});
        QTRY_VERIFY(controller.traceDetailText().contains("current"));
    }
    void repositoryFilteringSelectionAndSwitch() {
        QTemporaryDir directory; QVERIFY(directory.isValid()); QDir root(directory.path());
        for(const auto &name:QStringList{".git","src","build",".venv","node_modules"}) QVERIFY(root.mkdir(name));
        for(const auto &name:QStringList{"main.cpp","src/中文.h","build/hidden.cpp",".env","private.key",".venv/hidden.cpp","node_modules/hidden.js"}) {
            QFile f(root.filePath(name)); QVERIFY(f.open(QIODevice::WriteOnly)); f.write("fixture");
        }
        RepositoryModel model; model.setRoot(directory.path()); QTRY_VERIFY(!model.loading());
        QVERIFY2(model.error().isEmpty(),qPrintable(model.error())); QCOMPARE(model.count(),2);
        QCOMPARE(model.data(model.index(0),Qt::UserRole+1).toString(),"src");
        model.activate(0); QCOMPARE(model.count(),3);
        QCOMPARE(model.data(model.index(1),Qt::UserRole+1).toString(),QString("中文.h"));
        model.activate(1); QVERIFY(model.selectedPath().endsWith(QString("src/中文.h")));
        model.activate(0); QCOMPARE(model.count(),2);
        model.setRoot(root.filePath("missing")); model.setRoot(directory.path()); QTRY_VERIFY(!model.loading());
        QCOMPARE(model.count(),2); QVERIFY(model.error().isEmpty());
        model.setRoot(root.filePath("src")); QTRY_VERIFY(!model.loading()); QVERIFY(!model.error().isEmpty()); QCOMPARE(model.count(),0);
        model.setRoot({}); QCOMPARE(model.count(),0); QVERIFY(model.selectedPath().isEmpty());
    }
    void historyReadOnlyAndMutationGuard() {
        Source source;
        source.handle=[](const Request&r) {
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.method=="GET") Source::json(r,snapshot());
        };
        TaskController controller; controller.setBaseUrl(source.url()); controller.viewHistoryTask(A);
        QTRY_COMPARE(controller.state(),"waiting_approval");
        QVERIFY(controller.historyReadOnly()); QVERIFY(!controller.canApprove()); QVERIFY(!controller.canCancel());
        controller.approvePlan(); controller.rejectPlan(); controller.cancelTask();
        QTest::qWait(30); QCOMPARE(source.requests.size(),2);
        controller.openExecutionView(); QVERIFY(controller.canApprove());
        controller.approvePlan(); QVERIFY(controller.busy()); QVERIFY(!controller.canApprove());
        controller.approvePlan();
        QTRY_COMPARE(source.requests.size(),3); QCOMPARE(source.requests.last().body.value("plan_version").toInt(),1);
    }
    void presentationReplayToolsAndTaskDiff() {
        Source source;
        const QJsonObject diff{{"path","main.cpp"},{"created",false},{"diff","@@ -1 +1 @@\n-old\n+new\n user change"}};
        source.handle=[&](const Request&r) {
            if(r.path.endsWith("/events") && r.headers.toLower().contains("last-event-id:")) {
                r.socket->write("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"); r.socket->disconnectFromHost();
            } else if(r.path.endsWith("/events")) Source::stream(r,
                sseEvent(1,"message",{{"role","assistant"},{"content","中文结果"}})
                +sseEvent(2,"tool_started",{{"name","read_file"},{"tool_call_id","x"},{"arguments","{\"path\":\"main.cpp\"}"}})
                +sseEvent(3,"tool_finished",{{"name","read_file"},{"tool_call_id","x"},{"success",true},{"output","<data>\nlarge log"},{"duration_ms",12}})
                +sseEvent(4,"tool_started",{{"name","cmake_build"},{"repair_round",0},{"arguments",QJsonObject{}}})
                +sseEvent(5,"task_state_changed",{{"state","cancelled"}}));
            else { auto body=snapshot(A,"cancelled",5); body.insert("prompt","user request"); body.insert("task_diff",QJsonObject{{"files",QJsonArray{diff}}}); Source::json(r,body); }
        };
        TaskController controller; controller.setBaseUrl(source.url()); controller.viewHistoryTask(A);
        QTRY_COMPARE(controller.connectionState(),"ended"); QTRY_COMPARE(controller.eventSequence(),5);
        QCOMPARE(controller.traces()->count(),2); auto row=controller.traces()->get(0);
        QCOMPARE(row.value("status").toString(),"succeeded"); QCOMPARE(row.value("durationMs").toInt(),12);
        QCOMPARE(row.value("output").toString(),"<data>\nlarge log"); QVERIFY(!row.value("arguments").toString().isEmpty());
        QCOMPARE(controller.traces()->get(1).value("status").toString(),"cancelled");
        QCOMPARE(controller.diffFiles(),QJsonArray{diff}.toVariantList());
        controller.reconnectEvents(); QTRY_COMPARE(controller.connectionState(),"ended");
        QCOMPARE(controller.eventSequence(),5); QCOMPARE(controller.traces()->count(),2);
    }
    void presentationValidationStatuses_data() {
        QTest::addColumn<QString>("status"); QTest::addColumn<QString>("testStatus"); QTest::addColumn<QString>("expected");
        QTest::newRow("passed") << QString("succeeded") << QString("passed") << QString("succeeded");
        QTest::newRow("failed") << QString("failed") << QString("failed") << QString("failed");
        QTest::newRow("cancelled-zero-total") << QString("cancelled") << QString("cancelled") << QString("cancelled");
        QTest::newRow("not-run") << QString("not_run") << QString("not_run") << QString("not_run");
        QTest::newRow("no-tests") << QString("failed") << QString("no_tests") << QString("no_tests");
    }
    void presentationValidationStatuses() {
        QFETCH(QString,status); QFETCH(QString,testStatus); QFETCH(QString,expected);
        Source source;
        source.handle=[&](const Request&r) {
            if(r.path.endsWith("/events")) { Source::stream(r,{},false); return; }
            auto body=snapshot();
            QJsonObject report{{"repair_round",1},{"stages",QJsonObject{{"cmake_configure","succeeded"},{"cmake_build","succeeded"},{"run_tests",status}}},
                {"tests",QJsonObject{{"status",testStatus},{"total",0}}},
                {"commands",QJsonArray{QJsonObject{{"phase","run_tests"},{"returncode",8},{"duration_ms",25},{"stderr","真实错误日志"}}}}};
            auto initial=report; initial.insert("repair_round",0);
            body.insert("validation_history",QJsonArray{initial}); body.insert("build_result",report); Source::json(r,body);
        };
        TaskController controller; controller.setBaseUrl(source.url()); controller.viewHistoryTask(A);
        QTRY_COMPARE(controller.buildReports().size(),2);
        QCOMPARE(controller.buildReports()[0].toMap().value("label").toString(),QString("首次修改"));
        QCOMPARE(controller.buildReports()[1].toMap().value("label").toString(),QString("第 1 轮自动修复"));
        const auto row=controller.buildReports()[1].toMap().value("stages").toList()[2].toMap();
        QCOMPARE(row.value("status").toString(),expected); QCOMPARE(row.value("exitCode").toInt(),8);
        QCOMPARE(row.value("stderr").toString(),QString("真实错误日志")); QCOMPARE(row.value("durationMs").toInt(),25);
    }
    void parserAllBoundaries() {
        const auto input = QByteArray("\xEF\xBB\xBF: comment\r\nretry: 250\r\nid: one\r\nevent: message\r\ndata: ")
            + QString("中文🙂").toUtf8() + "\r\ndata: second\r\n\r\n";
        for (qsizetype split=0; split<=input.size(); ++split) {
            SseParser parser;
            auto frames = parser.feed(input.left(split)); frames += parser.feed(input.mid(split));
            QCOMPARE(frames.size(), 2); QCOMPARE(frames[0].retry,250);
            QCOMPARE(frames[1].id, "one"); QCOMPARE(QString::fromUtf8(frames[1].data),QString("中文🙂\nsecond"));
            QVERIFY(parser.error().isEmpty());
        }
    }
    void parserBytewiseAndLineEndings() {
        for (const QByteArray ending : {QByteArray("\n"),QByteArray("\r"),QByteArray("\r\n")}) {
            const auto bytes = "id: 1" + ending + "data: " + QString("跨包中文").toUtf8() + ending + ending
                + "data: next" + ending + ending;
            SseParser parser; QVector<SseFrame> frames;
            for(char c: bytes) frames += parser.feed(QByteArray(1,c));
            QCOMPARE(frames.size(),2); QCOMPARE(QString::fromUtf8(frames[0].data),QString("跨包中文"));
            QCOMPARE(frames[1].id,"1");
        }
    }
    void parserResetCommentsAndFields() {
        SseParser p;
        QVERIFY(p.feed(": heartbeat\n\nunknown: value\n\ndata: unfinished").isEmpty());
        p.reset();
        auto frames=p.feed("id: good\nid: bad\0id\ndata: x\n\n"_ba);
        QCOMPARE(frames.size(),1); QCOMPARE(frames[0].id,"good");
        frames=p.feed("id:\ndata\n\nretry: -1\nretry: 5x\nretry: 0\n\n");
        QCOMPARE(frames.size(),2); QVERIFY(frames[0].id.isEmpty()); QVERIFY(frames[0].data.isEmpty()); QCOMPARE(frames[1].retry,250);
    }
    void parserLimit() {
        SseParser p; p.feed(QByteArray(32*1024*1024+1,'a')); QVERIFY(!p.error().isEmpty());
    }
    void apiRequestsAndErrors() {
        Source source;
        source.handle=[](const Request &r) {
            if(r.path.endsWith("/approve")) Source::json(r,{{"error",QJsonObject{{"code","stale_plan"},{"message","Review current plan"}}}},409);
            else Source::json(r, snapshot(), r.method=="POST" ? 202 : 200);
        };
        AgentApiClient client; QVERIFY(client.setBaseUrl(source.url()));
        QSignalSpy ok(&client,&AgentApiClient::responseReceived), error(&client,&AgentApiClient::requestError);
        client.createTask("D:/中文 仓库","修复问题"); QTRY_COMPARE(ok.size(),1);
        QCOMPARE(source.requests[0].body.value("prompt").toString(),QString("修复问题"));
        client.approveTask(A,7); QTRY_COMPARE(error.size(),1);
        QCOMPARE(error[0][3].toInt(),409); QCOMPARE(error[0][4].toString(),"stale_plan");
        QCOMPARE(source.requests[1].body.value("plan_version").toInt(),7);
        client.cancelTask(A,true,7); QTRY_COMPARE(ok.size(),2);
        QCOMPARE(source.requests[2].body.value("reason").toString(),"reject");
        client.cancelTask(A); QTRY_COMPARE(ok.size(),3); QVERIFY(source.requests[3].body.isEmpty());
        client.getTask(A); client.listTasks(); QTRY_COMPARE(ok.size(),5);
        QVERIFY(!client.setBaseUrl(QUrl("http://remote.example")));
    }
    void apiAbortDoesNotCancelTask() {
        Source source; AgentApiClient client; client.setBaseUrl(source.url());
        QSignalSpy responses(&client,&AgentApiClient::responseReceived), errors(&client,&AgentApiClient::requestError);
        client.getTask(A); QTRY_COMPARE(source.requests.size(),1);
        client.abortPending(); Source::json(source.requests[0],snapshot());
        QTest::qWait(50); QCOMPARE(responses.size(),0); QCOMPARE(errors.size(),0); QCOMPARE(source.requests.size(),1);
    }
    void conversationStreamReconnectSnapshotAndUnicode() {
        Source source; int connections=0;
        source.handle=[&](const Request &r) {
            ++connections;
            if(connections==1) Source::stream(r,"retry: 250\n\n"+conversationSse(1,"answer_started",{{"segment_id","s1"}})
                +conversationSse(3,"answer_delta",{{"segment_id","s1"},{"previous_revision",2},{"text","gap"}}),false);
            else Source::stream(r,conversationSse(3,"answer_snapshot",{{"segment_id","s1"},{"content","快照🙂"},{"status","streaming"},{"terminal",false}})
                +conversationSse(4,"answer_delta",{{"segment_id","s1"},{"previous_revision",3},{"text","继续😀"}})
                +conversationSse(5,"answer_completed",{{"segment_id","s1"},{"content","快照🙂继续😀"},{"message_id",7}}));
        };
        ConversationStreamClient client; QVERIFY(client.setBaseUrl(source.url()));
        QSignalSpy events(&client,&ConversationStreamClient::eventReceived),ended(&client,&ConversationStreamClient::streamEnded);
        client.connectToRequest(A,B); QTRY_COMPARE_WITH_TIMEOUT(ended.size(),1,5000);
        QCOMPARE(connections,2); QCOMPARE(events.size(),4);
        QCOMPARE(events[1][0].toString(),QString("answer_snapshot"));
        QCOMPARE(events[1][1].toJsonObject().value("content").toString(),QString("快照🙂"));
        QCOMPARE(events[2][1].toJsonObject().value("text").toString(),QString("继续😀"));
        QVERIFY(source.requests[1].headers.toLower().contains("last-event-id: "+B.toUtf8()+":1"));
        QCOMPARE(client.lastEventId(),B+":5"); QCOMPARE(client.connectionState(),QString("ended"));
    }
    void conversationStreamRejectsBadWire_data() {
        QTest::addColumn<QString>("kind");
        QTest::newRow("content-type")<<QString("content-type");
        QTest::newRow("json")<<QString("json");
        QTest::newRow("limit")<<QString("limit");
    }
    void conversationStreamRejectsBadWire() {
        QFETCH(QString,kind); Source source;
        source.handle=[&](const Request &r) {
            if(kind=="content-type") Source::json(r,{{"not","sse"}});
            else if(kind=="json") Source::stream(r,"id: "+B.toUtf8()+":1\nevent: answer_started\ndata: broken\n\n");
            else Source::stream(r,"id: "+B.toUtf8()+":1\nevent: answer_delta\ndata: "+QByteArray(130*1024,'x')+"\n\n");
        };
        ConversationStreamClient client; client.setBaseUrl(source.url());
        QSignalSpy errors(&client,&ConversationStreamClient::fatalError),events(&client,&ConversationStreamClient::eventReceived);
        client.connectToRequest(A,B); QTRY_COMPARE(client.connectionState(),QString("error"));
        QCOMPARE(errors.size(),1); QCOMPARE(events.size(),0); QCOMPARE(source.requests.size(),1);
    }
    void conversationStreamPollingAndLegacyFallback() {
        Source source; Request stream; int eventGets=0,polls=0,posts=0; bool running=true,holdReconnect=false;
        auto requestState=[&] { return QJsonObject{{"conversation_id",A},{"request_id",B},{"mode","chat"},
            {"status",running?"running":"completed"},{"task_id",QJsonValue::Null},{"context_notice",""}}; };
        auto detail=[&] {
            QJsonArray messages{QJsonObject{{"message_id",1},{"conversation_id",A},{"request_id",B},
                {"role","user"},{"content","question"}}};
            if(!running) messages.append(QJsonObject{{"message_id",2},{"conversation_id",A},{"request_id",B},
                {"role","assistant"},{"content","legacy final"}});
            return QJsonObject{{"conversation_id",A},{"title","compatibility"},{"messages",messages},{"next_before",0},
                {"context_notice",""},{"last_request",requestState()},
                {"active_request",running?QJsonValue(requestState()):QJsonValue::Null}};
        };
        source.handle=[&](const Request &r) {
            if(r.method=="POST") { ++posts; Source::json(r,{}); }
            else if(r.path.endsWith("/events")) {
                ++eventGets;
                if(holdReconnect) return;
                stream=r; Source::stream(r,conversationSse(1,"answer_started",{{"segment_id","s1"}}),false);
            } else if(r.path.endsWith(("/requests/"+B).toUtf8())) { ++polls; Source::json(r,requestState()); }
            else if(r.path=="/api/v1/conversations/"+A.toUtf8()) Source::json(r,detail());
            else if(r.path.startsWith("/api/v1/conversations?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else Source::json(r,{});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController controller(&tasks);
        controller.selectConversation(A); QTRY_VERIFY(stream.socket); QTRY_COMPARE(eventGets,1);
        QTest::qWait(1200); QCOMPARE(polls,0); QCOMPARE(posts,0);
        QCOMPARE(controller.toolActivity(),QVariantMap{});
        stream.socket->write(conversationSse(2,"tool_activity",{{"segment_id","s1"},{"activity","started"},
            {"tool_call_id","call-1"},{"tool_name","read_file"}}));
        QTRY_COMPARE(controller.toolActivity().value("status").toString(),QString("running"));
        holdReconnect=true; stream.socket->disconnectFromHost();
        QTRY_COMPARE(controller.answerStatus(),QString("正在重连…"));
        QCOMPARE(controller.requestPollInterval(),5000);
        QTRY_VERIFY_WITH_TIMEOUT(polls>=1,6500);

        Source legacy; int legacyEvents=0,legacyPolls=0;
        legacy.handle=[&](const Request &r) {
            if(r.path.endsWith("/events")) { ++legacyEvents; Source::json(r,{{"error",QJsonObject{{"code","route_not_found"}}}},404); }
            else if(r.path.endsWith(("/requests/"+B).toUtf8())) {
                ++legacyPolls; running=false; Source::json(r,requestState());
            } else if(r.path=="/api/v1/conversations/"+A.toUtf8()) Source::json(r,detail());
            else if(r.path.startsWith("/api/v1/conversations?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else Source::json(r,{});
        };
        tasks.setBaseUrl(legacy.url()); controller.selectConversation(A);
        QTRY_COMPARE(legacyEvents,1); QTRY_VERIFY(legacyPolls>=1);
        QTRY_COMPARE(controller.messages()->count(),2);
        QCOMPARE(controller.messages()->get(1).value("content").toString(),QString("legacy final"));
        QVERIFY(controller.error().isEmpty()); QCOMPARE(posts,0);

        // Capability failure is scoped to one Base URL; switching back retries SSE.
        running=true; holdReconnect=false; tasks.setBaseUrl(source.url()); controller.selectConversation(A);
        QTRY_VERIFY(eventGets>=2);
    }
    void conversationControllerStreamsThrottleDiscardAndReplace() {
        Source source; Request stream; bool running=true;
        auto detail=[&] {
            QJsonObject request{{"conversation_id",A},{"request_id",B},{"mode","chat"},
                {"status",running?"running":"completed"},{"task_id",QJsonValue::Null},{"context_notice",""}};
            QJsonArray messages{QJsonObject{{"message_id",1},{"conversation_id",A},{"request_id",B},
                {"role","user"},{"content","question"}}};
            if(!running) messages.append(QJsonObject{{"message_id",2},{"conversation_id",A},{"request_id",B},
                {"role","assistant"},{"content",QString(1000,'x')}});
            return QJsonObject{{"conversation_id",A},{"title","stream"},{"messages",messages},{"next_before",0},
                {"context_notice",""},{"last_request",request},{"active_request",running?QJsonValue(request):QJsonValue::Null}};
        };
        source.handle=[&](const Request &r) {
            if(r.path.endsWith("/events")) { stream=r; Source::stream(r,{},false); }
            else if(r.path=="/api/v1/conversations/"+A.toUtf8()) Source::json(r,detail());
            else if(r.path.startsWith("/api/v1/conversations?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else if(r.path.startsWith("/api/v1/tasks?")) Source::json(r,{{"items",QJsonArray{}},{"total",0}});
            else Source::json(r,{});
        };
        TaskController tasks; tasks.setBaseUrl(source.url()); ConversationController controller(&tasks);
        controller.selectConversation(A); QTRY_VERIFY(stream.socket); QTRY_COMPARE(controller.messages()->count(),1);
        stream.socket->write(conversationSse(1,"answer_started",{{"segment_id","s1"}})
            +conversationSse(2,"answer_delta",{{"segment_id","s1"},{"previous_revision",1},{"text","旧草稿🙂"}}));
        QTRY_COMPARE(controller.messages()->count(),2);
        QTRY_COMPARE(controller.messages()->get(1).value("content").toString(),QString("旧草稿🙂"));
        stream.socket->write(conversationSse(5,"answer_snapshot",{{"segment_id","s1"},{"content","快照😀"},{"status","streaming"},{"terminal",false}}));
        QTRY_COMPARE(controller.messages()->get(1).value("content").toString(),QString("快照😀"));
        stream.socket->write(conversationSse(6,"answer_segment_discarded",{{"segment_id","s1"}})
            +conversationSse(7,"tool_activity",{{"segment_id","s1"},{"activity","started"},{"tool_name","read_file"}})
            +conversationSse(8,"answer_started",{{"segment_id","s2"}}));
        QTRY_COMPARE(controller.messages()->get(1).value("content").toString(),QString{});
        QCOMPARE(controller.answerStatus(),QString("正在生成…"));
        QSignalSpy updates(controller.messages(),&QAbstractItemModel::dataChanged);
        QByteArray deltas;
        for(int revision=9;revision<1009;++revision)
            deltas+=conversationSse(revision,"answer_delta",{{"segment_id","s2"},{"previous_revision",revision-1},{"text","x"}});
        stream.socket->write(deltas); QTRY_COMPARE_WITH_TIMEOUT(controller.messages()->get(1).value("content").toString().size(),1000,5000);
        QVERIFY2(updates.size()<=4,qPrintable(QString("1000 deltas caused %1 model updates").arg(updates.size())));
        running=false;
        stream.socket->write(conversationSse(1009,"answer_segment_committed",{{"segment_id","s2"},{"content",QString(1000,'x')}})
            +conversationSse(1010,"answer_completed",{{"segment_id","s2"},{"content",QString(1000,'x')},{"message_id",2}}));
        QTRY_COMPARE_WITH_TIMEOUT(controller.messages()->count(),2,5000);
        QTRY_COMPARE(controller.messages()->get(1).value("message_id").toLongLong(),qint64(2));
        QCOMPARE(controller.messages()->get(1).value("content").toString(),QString(1000,'x'));
        QVERIFY(!controller.messages()->get(1).value("streaming").toBool());
    }
    void sseFragmentReconnectReplayTerminal() {
        Source source; int connections=0;
        source.handle=[&](const Request &r) {
            if (++connections==1) Source::stream(r,"retry: 250\n\n: heartbeat\n\n"+sseEvent(1,"message",{{"content","中文分包🙂"}})+"id: incomplete\ndata: partial",true,true);
            else Source::stream(r,sseEvent(1,"message",{{"content","duplicate"}})+sseEvent(2,"task_state_changed",{{"state","completed"}})+sseEvent(3,"task_completed"));
        };
        SseClient client; client.setBaseUrl(source.url());
        QSignalSpy events(&client,&SseClient::eventApplied), end(&client,&SseClient::streamEnded);
        client.connectToTask(A);
        QTRY_COMPARE_WITH_TIMEOUT(end.size(),1,10000);
        QCOMPARE(events.size(),3); QCOMPARE(connections,2);
        QCOMPARE(events[0][1].toJsonObject().value("data").toObject().value("content").toString(),QString("中文分包🙂"));
        QVERIFY(source.requests[1].headers.toLower().contains("last-event-id: "+A.toUtf8()+":1"));
        QCOMPARE(client.lastEventId(),A+":3"); QCOMPARE(client.connectionState(),"ended");
        QTest::qWait(300); QCOMPARE(connections,2);
    }
    void sseInvalid_data() {
        QTest::addColumn<QByteArray>("bytes");
        QTest::newRow("gap") << sseEvent(2,"message");
        QTest::newRow("wrong-task") << sseEvent(1,"message",{},B);
        QTest::newRow("invalid-json") << ("id: "+A.toUtf8()+":1\ndata: broken\n\n");
        QTest::newRow("unknown-event") << sseEvent(1,"injected_authority");
    }
    void sseInvalid() {
        QFETCH(QByteArray,bytes); Source source; source.handle=[&](const Request &r){ Source::stream(r,bytes); };
        SseClient client; client.setBaseUrl(source.url()); QSignalSpy errors(&client,&SseClient::connectionError), events(&client,&SseClient::eventApplied);
        client.connectToTask(A); QTRY_COMPARE(client.connectionState(),"error");
        QCOMPARE(events.size(),0); QCOMPARE(errors.size(),1); QVERIFY(client.lastEventId().isEmpty());
    }
    void sse204AndCancelled() {
        Source source; source.handle=[](const Request&r) {
            if(r.headers.toLower().contains("last-event-id:")) { r.socket->write("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"); r.socket->disconnectFromHost(); }
            else Source::stream(r,sseEvent(1,"task_state_changed",{{"state","cancelled"}}));
        };
        SseClient client; client.setBaseUrl(source.url()); QSignalSpy end(&client,&SseClient::streamEnded);
        client.connectToTask(A); QTRY_COMPARE(end.size(),1);
        client.connectToTask(A,A+":1"); QTRY_COMPARE(end.size(),2);
        QCOMPARE(source.requests.size(),2);
    }
    void sseSwitchAndDisconnect() {
        Source source; source.handle=[](const Request&r) { Source::stream(r,{},false); };
        SseClient client; client.setBaseUrl(source.url()); QSignalSpy events(&client,&SseClient::eventApplied);
        client.connectToTask(A); QTRY_COMPARE(source.requests.size(),1);
        client.connectToTask(B); QTRY_COMPARE(source.requests.size(),2);
        if(source.requests[0].socket) source.requests[0].socket->write(sseEvent(1,"message",{{"content","old"}}));
        source.requests[1].socket->write(sseEvent(1,"message",{{"content","new"}},B));
        QTRY_COMPARE(events.size(),1); QCOMPARE(events[0][1].toJsonObject().value("task_id").toString(),B);
        client.disconnect(); QTest::qWait(300); QCOMPARE(source.requests.size(),2);
    }
    void controllerSnapshotReplayAndApproval_data() {
        QTest::addColumn<bool>("snapshotFirst");
        QTest::newRow("snapshot-before-replay") << true;
        QTest::newRow("events-before-snapshot") << false;
    }
    void controllerSnapshotReplayAndApproval() {
        QFETCH(bool,snapshotFirst);
        Source source; Request detail, stream; bool replaying=false;
        const auto events=sseEvent(1,"task_state_changed",{{"state","analyzing"}})
            +sseEvent(2,"plan_created",{{"plan_version",1},{"plan",QJsonObject{{"summary","old"}}}})
            +sseEvent(3,"task_state_changed",{{"state","waiting_approval"}});
        source.handle=[&](const Request&r) {
            if(r.path.endsWith("/events")) { stream=r; Source::stream(r,replaying?events:QByteArray{},false); }
            else if(r.path.endsWith("/approve")) Source::json(r,snapshot(A,"modifying",4,2));
            else detail=r;
        };
        TaskController controller; controller.setBaseUrl(source.url()); controller.setRepositoryPath("C:/fixture");
        QSignalSpy plans(&controller,&TaskController::planAvailable), approval(&controller,&TaskController::approvalRequired);
        controller.selectTask(A); QTRY_VERIFY(detail.socket); QTRY_VERIFY(stream.socket);
        if(snapshotFirst) {
            Source::json(detail,snapshot(A,"waiting_approval",3,2)); QTRY_COMPARE(controller.planVersion(),2);
            stream.socket->write(events); QTRY_COMPARE(controller.eventSequence(),3);
        } else {
            stream.socket->write(events); QTRY_COMPARE(controller.eventSequence(),3); QCOMPARE(controller.planVersion(),1);
            Source::json(detail,snapshot(A,"waiting_approval",3,2)); QTRY_COMPARE(controller.planVersion(),2);
        }
        QCOMPARE(controller.state(),"waiting_approval"); QCOMPARE(controller.planVersion(),2);
        QCOMPARE(plans.size(),snapshotFirst?1:2); QCOMPARE(approval.size(),snapshotFirst?1:2);
        replaying=true; controller.reconnectEvents(); QTRY_COMPARE(source.requests.size(),3); QTest::qWait(30);
        QCOMPARE(controller.eventSequence(),3); QCOMPARE(controller.planVersion(),2);
        QCOMPARE(plans.size(),snapshotFirst?1:2); QCOMPARE(approval.size(),snapshotFirst?1:2);
        controller.approvePlan(); QTRY_COMPARE(controller.state(),"modifying");
        QCOMPARE(source.requests.last().body.value("plan_version").toInt(),2);
    }
    void controllerSwitchIgnoresOldHttp() {
        Source source;
        source.handle=[](const Request&r) {
            if(r.path.endsWith("/events")) Source::stream(r,{},false);
            else if(r.path.contains(B.toUtf8())) Source::json(r,snapshot(B));
        };
        TaskController controller; controller.setBaseUrl(source.url());
        controller.selectTask(A); QTRY_COMPARE(source.requests.size(),2);
        const auto old=source.requests[0];
        controller.selectTask(B); QTRY_COMPARE(controller.state(),"waiting_approval");
        Source::json(old,snapshot(A,"failed",30)); QTest::qWait(100);
        QCOMPARE(controller.taskId(),B); QCOMPARE(controller.state(),"waiting_approval");
    }
    void sseHttpErrors_data() {
        QTest::addColumn<int>("status");
        QTest::newRow("cursor-conflict") << 409;
        QTest::newRow("not-found") << 404;
        QTest::newRow("wrong-content-type") << 200;
    }
    void sseHttpErrors() {
        QFETCH(int,status); Source source; source.handle=[=](const Request&r){Source::json(r,{{"error","bad stream"}},status);};
        SseClient client; client.setBaseUrl(source.url()); QSignalSpy errors(&client,&SseClient::connectionError);
        client.connectToTask(A,A+":1"); QTRY_COMPARE(client.connectionState(),"error");
        QCOMPARE(errors.size(),1); QCOMPARE(client.lastEventId(),A+":1");
        QTest::qWait(300); QCOMPARE(source.requests.size(),1);
    }
    void controllerOlderQueryCannotRegressLiveState() {
        Source source;
        source.handle=[](const Request&r) {
            if(r.path.endsWith("/events")) Source::stream(r,sseEvent(1,"plan_created",{{"plan_version",2},{"plan",QJsonObject{{"summary","current"}}}})
                +sseEvent(2,"task_state_changed",{{"state","waiting_approval"}}),false);
        };
        TaskController controller; controller.setBaseUrl(source.url()); controller.selectTask(A);
        QTRY_COMPARE(controller.state(),"waiting_approval"); QCOMPARE(controller.planVersion(),2);
        for(const auto&r:source.requests) if(!r.path.endsWith("/events")) Source::json(r,snapshot(A,"analyzing",1,0));
        QTest::qWait(60); QCOMPARE(controller.state(),"waiting_approval"); QCOMPARE(controller.planVersion(),2);
    }
};
QTEST_GUILESS_MAIN(CommunicationTest)
#include "tst_communication.moc"

