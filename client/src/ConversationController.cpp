#include "ConversationController.h"
#include "Protocol.h"
#include <QJsonArray>
#include "JsonHttp.h"
#include <QNetworkProxy>
#include <QUuid>
#include <QDesktopServices>
#include <QLoggingCategory>

Q_LOGGING_CATEGORY(conversationStreamLog,"forgepilot.conversation.stream",QtInfoMsg)

ConversationController::ConversationController(TaskController *tasks,QObject *parent):QObject(parent),m_tasks(tasks)
{
    m_memory=new MemoryController(this);
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_poll.setInterval(1000);
    connect(&m_poll,&QTimer::timeout,this,[this]{
        if(busy()) return;
        if(!m_detail.value("active_request").toObject().value("request_id").toString().isEmpty()) {
            if(!streamOwnsPolling()) pollActiveRequest();
        }
        else if(active() || retryPending()) refresh();
    });
    m_poll.start();
    m_stream.setBaseUrl(tasks->baseUrl());
    m_streamFlush.setSingleShot(true); m_streamFlush.setInterval(40);
    connect(&m_streamFlush,&QTimer::timeout,this,&ConversationController::flushStreamBuffer);
    connect(&m_stream,&ConversationStreamClient::eventReceived,this,&ConversationController::applyStreamEvent);
    connect(&m_stream,&ConversationStreamClient::connectionStateChanged,this,[this](const QString &state,quint64 generation){
        if(generation!=m_streamGeneration || m_streamRequest.isEmpty()) return;
        if(state=="connecting" && m_streamDraft.isEmpty()) m_answerStatus="正在思考…";
        else if(state=="connected") { m_conversationStreamSupported=true; restorePollInterval(); }
        else if(state=="reconnecting") { m_poll.setInterval(5000); m_answerStatus="正在重连…"; }
        emit changed();
    });
    connect(&m_stream,&ConversationStreamClient::transientError,this,[this](const QString &,quint64 generation){
        if(generation==m_streamGeneration && !m_streamRequest.isEmpty()) { m_answerStatus="正在重连…"; emit changed(); }
    });
    connect(&m_stream,&ConversationStreamClient::unsupported,this,[this](const QString &code,quint64 generation){
        if(generation!=m_streamGeneration || m_streamRequest.isEmpty()) return;
        m_conversationStreamSupported=false; restorePollInterval();
        m_answerStatus=m_streamDraft.isEmpty()?"正在思考…":"正在生成…";
        recordClientStreamMetrics(code); resetRequestPolling(); pollActiveRequest(); emit changed();
    });
    connect(&m_stream,&ConversationStreamClient::fatalError,this,[this](const QString &code,quint64 generation){
        if(generation!=m_streamGeneration || m_streamRequest.isEmpty()) return;
        m_streamSuppressedRequest=m_streamRequest; restorePollInterval();
        m_answerStatus=m_streamDraft.isEmpty()?"正在思考…":"回答未完整生成";
        recordClientStreamMetrics(code); resetRequestPolling(); refresh(); emit changed();
    });
    connect(&m_stream,&ConversationStreamClient::streamEnded,this,[this](quint64 generation){
        if(generation!=m_streamGeneration || m_streamRequest.isEmpty()) return;
        restorePollInterval(); recordClientStreamMetrics("none");
        ++m_generation; m_loading=false; resetRequestPolling(); refresh();
    });
    connect(tasks,&TaskController::taskFinished,this,[this]{
        const auto activeRequest=m_detail.value("active_request").toObject();
        // Task completion can precede publication of the conversation's final message.
        // Confirm the request's terminal state, then use the same finalizer as the timer.
        if(!activeRequest.isEmpty() && activeRequest.value("task_id").toString()==m_tasks->taskId()) pollActiveRequest();
    });
    connect(tasks,&TaskController::baseUrlChanged,this,[this]{
        stopAnswerStream(true); m_stream.setBaseUrl(m_tasks->baseUrl());
        m_conversationStreamSupported=true; restorePollInterval();
        m_serverStatus="disconnected";
        ++m_deleteGeneration; m_deleteTarget.clear(); m_deleteError.clear(); m_deleting=false; m_deleteLoading=false;
        ++m_listGeneration; m_historyLoading=false; m_history.clear(); m_cursor.clear();
        m_historyTotal=0; emit historyChanged();
        selectConversation({}); refreshHistory();
    });
}
ConversationController::~ConversationController()
{ for(auto *reply:std::as_const(m_replies)) { reply->disconnect(this); reply->abort(); } }
void ConversationController::request(const QByteArray &method,const QString &path,const QJsonObject &body,Callback callback,qint64 limit)
{
    QUrl url=m_tasks->baseUrl().resolved(QUrl(path));
    const auto base=m_tasks->baseUrl();
    const auto generation=m_generation;
    const auto cid=m_id;
    if(m_serverStatus!="connected") { m_serverStatus="checking"; emit changed(); }
    auto *reply=JsonHttp::send(m_network,this,method,url,body,limit,
        [=,this](QNetworkReply *completed,const JsonHttp::Response &response){
        m_replies.remove(completed);
        const int status=response.status;
        QString error;
        if(response.exceededLimit) error=limit==16*1024*1024 ? "对话响应超过 16 MiB 上限，请缩小查询范围。" : "工具记录响应超过 128 KiB 上限，请重试。";
        else if(status<200 || status>=300) error="对话请求失败。"+response.body.value("error").toObject().value("message").toString("请检查后端连接后刷新。");
        else if(response.networkError!=QNetworkReply::NoError || response.invalidJson) error="对话响应不完整，请刷新重试。";
        if(base==m_tasks->baseUrl() && generation==m_generation && cid==m_id) { m_serverStatus=status>0?"connected":"error"; emit changed(); }
        callback(response.body,status,error);
    });
    m_replies.insert(reply);
}
void ConversationController::setMode(const QString &mode)
{ if((mode=="chat" || mode=="task") && !busy() && !retryPending()) { m_mode=mode; emit changed(); } }
void ConversationController::refreshHistory(bool more)
{
    if(m_historyLoading || (more && m_cursor.isEmpty())) return;
    m_historyLoading=true; const auto generation=m_listGeneration; emit historyChanged();
    request("GET","/api/v1/conversations?limit=20"+(more?"&before="+QString::fromLatin1(QUrl::toPercentEncoding(m_cursor)):QString{}),{},[=,this](const QJsonObject &body,int,const QString &error){
        if(generation!=m_listGeneration) return;
        m_historyLoading=false;
        if(error.isEmpty()) {
            if(!more) m_history.clear();
            for(const auto &item:body.value("items").toArray()) {
                const auto row=item.toObject().toVariantMap(); const auto id=row.value("conversation_id").toString();
                if(m_deleted.contains(m_tasks->baseUrl().toString()+'/'+id)) continue;
                bool found=false;
                for(auto &existing:m_history) if(existing.toMap().value("conversation_id")==id) { existing=row; found=true; break; }
                if(!found) m_history.append(row);
            }
            m_historyTotal=body.value("total").toInt();
            m_cursor=body.value("next_cursor").toString();
            if(m_id.isEmpty() && !m_creating && !m_history.isEmpty()) selectConversation(m_history.first().toMap().value("conversation_id").toString());
        } else { m_error=error; emit changed(); }
        emit historyChanged();
    });
}
void ConversationController::selectConversation(const QString &id)
{
    if(!id.isEmpty() && !Protocol::validTaskId(id)) return;
    if(m_deleted.contains(m_tasks->baseUrl().toString()+'/'+id)) return;
    stopAnswerStream(true);
    ++m_generation; m_id=id; m_mode="chat"; m_detail={}; m_rows.clear(); m_messages.clear(); m_before=0;
    resetRequestPolling(); m_finishedRequest.clear();
    closeMcpTools();
    m_loading=false; m_editing=false; m_error.clear();
    m_tasks->setConversationFilter(id); m_tasks->setRepositoryPath({});
    emit changed(); emit selectionChanged();
    if(!id.isEmpty()) refresh();
}
void ConversationController::refresh(bool older)
{
    if(m_id.isEmpty() || m_loading || m_removing.contains(pendingKey()) || (older && !m_before)) return;
    // A newer detail read owns state; an earlier status response must not overwrite it.
    resetRequestPolling();
    m_loading=true; const auto generation=m_generation; emit changed();
    request("GET",path()+(older?"?before="+QString::number(m_before):QString{}),{},[=,this](const QJsonObject &body,int status,const QString &error){
        if(generation!=m_generation) return;
        m_loading=false;
        if(status==404) { forgetConversation(m_id); return; }
        if(error.isEmpty()) { m_error.clear(); applyDetail(body,older); }
        else m_error=error;
        emit changed();
    });
}
void ConversationController::resetRequestPolling()
{ ++m_pollGeneration; m_pollingRequest=false; }
bool ConversationController::streamOwnsPolling() const
{
    const auto active=m_detail.value("active_request").toObject();
    if(!m_conversationStreamSupported || active.value("mode").toString()!="chat"
       || active.value("request_id").toString()!=m_streamRequest) return false;
    const auto state=m_stream.connectionState();
    return state=="connecting" || state=="connected";
}
void ConversationController::restorePollInterval()
{ if(m_poll.interval()!=1000) m_poll.setInterval(1000); }
void ConversationController::recordClientStreamMetrics(const QString &errorCode)
{
    if(m_streamMetricsRecorded) return;
    m_streamMetricsRecorded=true;
    qCDebug(conversationStreamLog).nospace()
        << "conversation_stream_metrics stream_reconnect_count=" << m_stream.reconnectCount()
        << " ui_model_updates=" << m_uiModelUpdates
        << " stream_error_code=" << errorCode;
}
void ConversationController::pollActiveRequest()
{
    const auto previous=m_detail.value("active_request").toObject();
    const auto rid=previous.value("request_id").toString(),cid=m_id;
    if(cid.isEmpty() || rid.isEmpty() || busy() || m_loading || m_pollingRequest || m_removing.contains(pendingKey())) return;
    const auto base=m_tasks->baseUrl();
    const auto generation=m_generation,pollGeneration=m_pollGeneration;
    m_pollingRequest=true;
    request("GET",path()+"/requests/"+rid,{},[=,this](const QJsonObject &body,int status,const QString &error){
        if(generation!=m_generation || pollGeneration!=m_pollGeneration || cid!=m_id || base!=m_tasks->baseUrl() ||
            rid!=m_detail.value("active_request").toObject().value("request_id").toString()) return;
        m_pollingRequest=false;
        if(status==404) { forgetConversation(cid); return; }
        if(!error.isEmpty()) { m_error=error; emit changed(); return; }
        const auto state=body.value("status").toString();
        if(body.value("conversation_id").toString()!=cid || body.value("request_id").toString()!=rid ||
            (state!="running" && state!="completed" && state!="failed" && state!="cancelled" && state!="interrupted")) {
            m_error="请求状态响应无效，请刷新重试。"; emit changed(); return;
        }
        m_error.clear(); m_detail["context_notice"]=body.value("context_notice");
        if(state=="running") {
            m_detail["active_request"]=body;
            bindRequestTask(previous,body);
            if(!m_mcpToolsRequest.isEmpty()) refreshMcpTools();
        } else {
            // Bind even when task_id first becomes available in the terminal response.
            bindRequestTask(previous,body); finishRequest(body);
        }
        emit changed();
    });
}
void ConversationController::bindRequestTask(const QJsonObject &previous,const QJsonObject &request)
{
    const auto task=request.value("task_id").toString();
    if(!task.isEmpty() && previous.value("task_id").toString()!=task) {
        m_tasks->selectTask(task); m_tasks->refreshTasks();
    }
}
void ConversationController::finishRequest(const QJsonObject &request,bool detailLoaded)
{
    const auto rid=request.value("request_id").toString();
    if(rid.isEmpty() || rid==m_finishedRequest) return;
    m_finishedRequest=rid;
    m_detail["active_request"]=QJsonValue::Null; m_detail["last_request"]=request;
    resetRequestPolling();
    // A tool-record read started while running may return after the final detail.
    // Retire it so applyDetail can fetch the final records even if that read is held.
    if(!m_mcpToolsRequest.isEmpty()) { ++m_mcpToolsGeneration; m_mcpToolsLoading=false; }
    if(!detailLoaded) refresh();
    m_tasks->refreshTasks(); refreshHistory();
}

void ConversationController::beginAnswerStream(const QJsonObject &request)
{
    const auto rid=request.value("request_id").toString();
    if(request.value("mode").toString()!="chat" || request.value("status").toString()!="running"
       || !Protocol::validTaskId(rid) || m_id.isEmpty() || rid==m_streamSuppressedRequest
       || !m_conversationStreamSupported) return;
    if(rid==m_streamRequest && m_stream.connectionState()!="disconnected" && m_stream.connectionState()!="error") return;
    stopAnswerStream(true);
    m_streamRequest=rid; m_streamKey="stream:"+rid; m_streamRevision=0;
    m_streamDraft.clear(); m_streamBuffer.clear(); m_streamSegment.clear(); m_answerStatus="正在思考…";
    m_streamToolActivity.clear(); m_uiModelUpdates=0; m_streamMetricsRecorded=false;
    m_streamGeneration=m_stream.connectToRequest(m_id,rid);
    emit changed();
}

void ConversationController::stopAnswerStream(bool removeRow)
{
    m_streamFlush.stop(); m_stream.disconnectStream(); m_streamGeneration=m_stream.sessionGeneration();
    if(removeRow && !m_streamKey.isEmpty()) m_messages.remove(m_streamKey);
    if(removeRow) {
        m_streamRequest.clear(); m_streamSegment.clear(); m_streamDraft.clear();
        m_streamBuffer.clear(); m_streamKey.clear(); m_streamSuppressedRequest.clear();
        m_streamRevision=0; m_answerStatus.clear();
        m_streamToolActivity.clear(); restorePollInterval();
    }
}

void ConversationController::updateStreamRow()
{
    if(m_streamKey.isEmpty()) return;
    m_messages.upsert(m_streamKey,{{"role","assistant"},{"content",m_streamDraft},
        {"request_id",m_streamRequest},{"segment_id",m_streamSegment},
        {"revision",m_streamRevision},{"streaming",true},{"stream_key",m_streamKey}});
    ++m_uiModelUpdates;
}

void ConversationController::flushStreamBuffer()
{
    if(m_streamBuffer.isEmpty()) return;
    m_streamDraft+=m_streamBuffer; m_streamBuffer.clear(); updateStreamRow();
}

void ConversationController::applyStreamEvent(const QString &type,const QJsonObject &payload,quint64 generation)
{
    if(generation!=m_streamGeneration || generation!=m_stream.sessionGeneration()
       || m_tasks->baseUrl()!=m_stream.baseUrl()
       || payload.value("conversation_id").toString()!=m_id
       || payload.value("request_id").toString()!=m_streamRequest) return;
    const auto revision=payload.value("revision").toInteger();
    const auto segment=payload.value("segment_id").toString();
    bool notify=true;
    if(type=="answer_snapshot") {
        const auto content=payload.value("content").toString();
        if(content.size()>32768) {
            m_stream.disconnectStream(); m_streamGeneration=m_stream.sessionGeneration();
            m_answerStatus="回答未完整生成"; ++m_generation; m_loading=false; resetRequestPolling(); refresh();
            emit changed(); return;
        }
        m_streamFlush.stop(); m_streamBuffer.clear(); m_streamDraft=content;
        m_streamSegment=segment; m_streamRevision=revision; updateStreamRow();
        m_answerStatus=payload.value("status").toString()=="working"?"正在调用工具…":"正在生成…";
        if(payload.value("terminal").toBool()) {
            m_stream.disconnectStream(); ++m_generation; m_loading=false; resetRequestPolling(); refresh();
        }
    } else {
        if(revision!=m_streamRevision+1) { m_stream.requestRecovery(); return; }
        if(type=="answer_started") {
            m_streamFlush.stop(); m_streamBuffer.clear(); m_streamDraft.clear(); m_streamSegment=segment;
            m_streamRevision=revision; m_answerStatus="正在生成…"; updateStreamRow();
        } else if(type=="answer_delta") {
            if(segment!=m_streamSegment || payload.value("previous_revision").toInteger()!=m_streamRevision) {
                m_stream.requestRecovery(); return;
            }
            const auto text=payload.value("text").toString();
            if(m_streamDraft.size()+m_streamBuffer.size()+text.size()>32768) {
                m_stream.disconnectStream(); m_streamGeneration=m_stream.sessionGeneration();
                m_answerStatus="回答未完整生成"; ++m_generation; m_loading=false; resetRequestPolling(); refresh();
                emit changed(); return;
            }
            m_streamRevision=revision; m_streamBuffer+=payload.value("text").toString();
            if(!m_streamFlush.isActive()) m_streamFlush.start();
            notify=m_answerStatus!="正在生成…"; m_answerStatus="正在生成…";
        } else if(type=="answer_segment_discarded") {
            if(segment!=m_streamSegment) return;
            m_streamFlush.stop(); m_streamBuffer.clear(); m_streamDraft.clear(); m_streamRevision=revision;
            m_answerStatus="正在调用工具…"; updateStreamRow();
        } else if(type=="answer_segment_committed") {
            if(segment!=m_streamSegment) return;
            flushStreamBuffer(); m_streamRevision=revision; updateStreamRow();
        } else if(type=="tool_activity") {
            m_streamRevision=revision; m_answerStatus="正在调用工具…";
            m_streamToolActivity={{"request_id",m_streamRequest},{"segment_id",segment},
                {"status",payload.value("activity").toString()=="started"?"running":
                    payload.value("success").toBool()?"succeeded":"failed"},
                {"tool_call_id",payload.value("tool_call_id").toString()},
                {"tool_name",payload.value("tool_name").toString()}};
        } else if(type=="answer_completed" || type=="answer_cancelled" || type=="answer_failed") {
            flushStreamBuffer(); m_streamRevision=revision;
            m_answerStatus=type=="answer_cancelled"?"已停止":type=="answer_failed"?"回答未完整生成":"";
            recordClientStreamMetrics(type=="answer_failed"?payload.value("error_code").toString("stream_failed"):"none");
            restorePollInterval();
            m_stream.disconnectStream();
            ++m_generation; m_loading=false; resetRequestPolling(); refresh();
        } else return;
    }
    if(notify) emit changed();
}

bool ConversationController::reconcileStreamMessage(const QJsonArray &messages)
{
    if(m_streamRequest.isEmpty()) return false;
    for(const auto &value:messages) {
        const auto message=value.toObject();
        if(message.value("role").toString()=="assistant" && message.value("request_id").toString()==m_streamRequest
           && message.value("message_id").isDouble()) {
            stopAnswerStream(true); return true;
        }
    }
    return false;
}
void ConversationController::applyDetail(const QJsonObject &body,bool older)
{
    if(body.value("conversation_id").toString()!=m_id || m_removing.contains(pendingKey()) || m_deleted.contains(pendingKey())) return;
    const auto oldActive=m_detail.value("active_request").toObject();
    m_detail=body;
    const auto last=body.value("last_request").toObject();
    const auto activeRequest=body.value("active_request").toObject();
    const auto pending=m_pending.value(pendingKey()).value("request_id").toString();
    if(!pending.isEmpty() && (pending==last.value("request_id").toString() || pending==activeRequest.value("request_id").toString())) m_pending.remove(pendingKey());
    const bool initial=m_rows.isEmpty(); bool changedRows=false;
    const auto messages=body.value("messages").toArray();
    for(const auto &value:messages) {
        auto row=value.toObject(); const auto key=row.value("message_id").toInteger();
        if(m_rows.value(key)!=row.toVariantMap()) { m_rows[key]=row.toVariantMap(); changedRows=true; }
    }
    if(initial || older) m_before=body.value("next_before").toInteger();
    if(changedRows) {
        if(older) m_messages.clear();
        for(auto it=m_rows.cbegin();it!=m_rows.cend();++it) m_messages.upsert(QString::number(it.key()),it.value());
    }
    const bool reconciled=reconcileStreamMessage(messages);
    if(!reconciled && !m_streamRequest.isEmpty() && !m_streamKey.isEmpty()
       && !m_streamSegment.isEmpty()) updateStreamRow();
    m_tasks->setRepositoryPath(repositoryPath());
    if(!activeRequest.isEmpty()) {
        bindRequestTask(oldActive,activeRequest);
        if(!reconciled && activeRequest.value("mode").toString()=="chat") beginAnswerStream(activeRequest);
    }
    else if(!oldActive.isEmpty()) { bindRequestTask(oldActive,last); finishRequest(last,!older); }
    if(!m_mcpToolsRequest.isEmpty()) refreshMcpTools();
}

QVariantMap ConversationController::toolActivity() const
{
    if(!m_streamToolActivity.isEmpty() && !m_streamRequest.isEmpty()) return m_streamToolActivity;
    auto request=m_detail.value("active_request").toObject();
    if(request.isEmpty()) request=m_detail.value("last_request").toObject();
    auto activity=request.value("tool_activity").toObject().toVariantMap();
    if(!activity.isEmpty()) activity["request_id"]=request.value("request_id").toString();
    return activity;
}
void ConversationController::showMcpTools(const QString &requestId)
{
    if(m_id.isEmpty() || m_removing.contains(pendingKey()) || !Protocol::validTaskId(requestId)) return;
    closeMcpTools(); m_mcpToolsRequest=requestId; refreshMcpTools();
}
void ConversationController::closeMcpTools()
{
    ++m_mcpToolsGeneration; m_mcpToolsRequest.clear(); m_mcpTools.clear();
    m_mcpToolsError.clear(); m_mcpToolsLoading=false; emit mcpToolsChanged();
}
void ConversationController::refreshMcpTools()
{
    if(m_id.isEmpty() || m_mcpToolsRequest.isEmpty() || m_mcpToolsLoading) return;
    const auto generation=m_mcpToolsGeneration;
    const auto base=m_tasks->baseUrl();
    const auto cid=m_id,rid=m_mcpToolsRequest;
    m_mcpToolsLoading=true; m_mcpToolsError.clear(); emit mcpToolsChanged();
    request("GET",path()+"/requests/"+rid+"/mcp-tools",{},[=,this](const QJsonObject &body,int,const QString &error){
        if(generation!=m_mcpToolsGeneration || base!=m_tasks->baseUrl() || cid!=m_id || rid!=m_mcpToolsRequest) return;
        m_mcpToolsLoading=false; m_mcpToolsError=error;
        if(error.isEmpty()) {
            if(body.value("conversation_id").toString()!=cid || body.value("request_id").toString()!=rid ||
                !body.value("items").isArray() || body.value("items").toArray().size()>8) {
                m_mcpTools.clear(); m_mcpToolsError="工具记录响应无效，请重试。";
            } else {
                m_mcpTools.clear();
                for(const auto &value:body.value("items").toArray()) {
                    auto record=value.toObject(); QJsonArray sources;
                    for(const auto &item:record.value("sources").toArray()) {
                        auto source=item.toObject();
                        if(sources.size()<10 && safeSourceUrl(source.value("url").toString())) sources.append(source);
                    }
                    record["sources"]=sources; m_mcpTools.append(record.toVariantMap());
                }
            }
        }
        emit mcpToolsChanged();
    },128*1024);
}
bool ConversationController::safeSourceUrl(const QString &text)
{
    if(text.isEmpty() || text.size()>2048 || text.contains('\\')) return false;
    for(const auto c:text) if(c.isSpace() || c.unicode()<32 || c=='<' || c=='>' || c=='"') return false;
    const QUrl url(text,QUrl::StrictMode);
    return url.isValid() && !url.host().isEmpty() && url.userInfo().isEmpty() && url.port()!=0 &&
        (url.scheme()=="http" || url.scheme()=="https");
}
void ConversationController::openMcpSource(const QString &url)
{
    if(!safeSourceUrl(url)) return;
    for(const auto &record:std::as_const(m_mcpTools))
        for(const auto &source:record.toMap().value("sources").toList())
            if(source.toMap().value("url").toString()==url) { QDesktopServices::openUrl(QUrl(url)); return; }
}
void ConversationController::create(const QString &prompt,const QString &repository)
{
    if(m_creating) return;
    const auto requestedMode=m_mode;
    QJsonObject creation;
    if(!repository.isEmpty()) creation["repository_path"]=repository;
    m_creating=true; m_error.clear(); const auto generation=m_generation; emit changed();
    request("POST","/api/v1/conversations",creation,[=,this](const QJsonObject &body,int,const QString &error){
        m_creating=false;
        if(generation==m_generation) {
            if(error.isEmpty()) {
                selectConversation(body.value("conversation_id").toString()); applyDetail(body);
                setMode(requestedMode);
                if(!prompt.isEmpty()) sendMessage(prompt);
            } else m_error=error;
            emit changed();
        }
        refreshHistory();
    });
}
void ConversationController::newConversation() { create(); }
void ConversationController::patch(const QJsonObject &body)
{
    if(m_id.isEmpty() || busy() || retryPending()) return;
    m_editing=true; const auto generation=++m_generation; m_loading=false; emit changed();
    resetRequestPolling();
    request("PATCH",path(),body,[=,this](const QJsonObject &reply,int,const QString &error){
        if(generation!=m_generation) return;
        ++m_generation; m_loading=false; m_editing=false; m_error=error;
        if(error.isEmpty()) { applyDetail(reply); refreshHistory(); }
        emit changed();
    });
}
void ConversationController::renameConversation(const QString &title)
{ if(!title.trimmed().isEmpty()) patch({{"title",title.trimmed().left(80)}}); }
void ConversationController::chooseRepository(const QUrl &folder)
{
    if(!folder.isLocalFile() || active() || busy() || retryPending()) return;
    if(m_id.isEmpty()) create({},folder.toLocalFile());
    else patch({{"repository_path",folder.toLocalFile()}});
}
void ConversationController::clearRepository()
{ if(!active()) patch({{"repository_path",QJsonValue::Null}}); }
void ConversationController::sendMessage(const QString &prompt)
{
    if(busy() || active() || retryPending() || prompt.trimmed().isEmpty()) return;
    if(prompt.size()>16384) { m_error="问题超过 16384 字符上限，请缩短后发送。"; emit changed(); return; }
    if(m_mode=="task" && repositoryPath().isEmpty()) { m_error="执行任务前必须关联项目。"; emit changed(); return; }
    if(m_id.isEmpty()) { create(prompt); return; }
    stopAnswerStream(true);
    m_pending.insert(pendingKey(),{{"request_id",QUuid::createUuid().toString(QUuid::WithoutBraces)},{"mode",m_mode},{"prompt",prompt}});
    postPending();
}
void ConversationController::retrySend() { if(!busy() && retryPending()) postPending(); }
void ConversationController::postPending()
{
    const auto key=pendingKey(),id=m_id;
    const auto payload=m_pending.value(key); m_sending.insert(id); m_error.clear(); emit changed();
    // Invalidate older detail reads: they must not overwrite post-admission state.
    ++m_generation; m_loading=false;
    resetRequestPolling();
    const auto admittedGeneration=m_generation;
    request("POST",path()+"/requests",payload,[=,this](const QJsonObject &body,int status,const QString &error){
        m_sending.remove(id);
        if(m_deleted.contains(key) || m_removing.contains(key)) return;
        if(error.isEmpty() || (status>=400 && status<500)) m_pending.remove(key);
        if(error.isEmpty()) emit submitted(id,payload.value("prompt").toString());
        if(admittedGeneration!=m_generation) { if(id==m_id) emit changed(); return; }
        ++m_generation; m_loading=false;
        if(error.isEmpty()) {
            m_error.clear(); m_detail["active_request"]=body.value("status").toString()=="running"?QJsonValue(body):QJsonValue::Null;
            const auto task=body.value("task_id").toString();
            if(!task.isEmpty()) { m_tasks->selectTask(task); m_tasks->refreshTasks(); }
            else if(body.value("status").toString()=="running" && body.value("mode").toString()=="chat") beginAnswerStream(body);
            refresh(); refreshHistory();
        } else {
            m_error=error+(m_pending.contains(key)?" 未确认是否收到；请刷新或重试原请求，系统会使用同一标识避免重复发送。":"");
            refresh();
        }
        emit changed();
    });
}
void ConversationController::stopAnswer()
{
    if(busy() || activeMode()!="chat") return;
    const auto rid=m_detail.value("active_request").toObject().value("request_id").toString();
    recordClientStreamMetrics("cancelled"); restorePollInterval();
    m_stream.disconnectStream(); m_streamGeneration=m_stream.sessionGeneration();
    m_streamSuppressedRequest=rid; m_answerStatus="已停止";
    m_editing=true; const auto generation=++m_generation; m_loading=false; emit changed();
    resetRequestPolling();
    request("POST",path()+"/requests/"+rid+"/stop",{},[=,this](const QJsonObject&,int,const QString &error){
        if(generation!=m_generation) return;
        m_editing=false; m_error=error; refresh(); emit changed();
    });
}
void ConversationController::showActiveTask()
{
    const auto id=m_detail.value("active_request").toObject().value("task_id").toString();
    if(!id.isEmpty()) m_tasks->selectTask(id);
}

void ConversationController::renameConversationById(const QString &id,const QString &title)
{
    if(id==m_id) { renameConversation(title); return; }
    const auto key=m_tasks->baseUrl().toString()+'/'+id;
    if(!Protocol::validTaskId(id) || title.trimmed().isEmpty() || m_deleted.contains(key) || m_removing.contains(key)) return;
    const auto base=m_tasks->baseUrl();
    request("PATCH","/api/v1/conversations/"+id,{{"title",title.trimmed().left(80)}},[=,this](const QJsonObject&,int,const QString &error){
        if(base!=m_tasks->baseUrl() || m_deleted.contains(key) || m_removing.contains(key)) return;
        m_error=error; refreshHistory(); emit changed();
    });
}
void ConversationController::prepareDelete(const QString &id,const QString &title)
{
    if(m_deleting || !Protocol::validTaskId(id)) return;
    m_deleteTarget=id; m_deleteTitle=title; m_deleteError.clear(); m_deleteLoading=true; m_deleteActive=false;
    const auto generation=++m_deleteGeneration; emit changed();
    request("GET","/api/v1/conversations/"+id,{},[=,this](const QJsonObject &body,int status,const QString &error){
        if(generation!=m_deleteGeneration) return;
        m_deleteLoading=false;
        m_deleteActive=!body.value("active_request").toObject().isEmpty();
        m_deleteError=status==404?QString{}:error;
        emit changed();
    });
}
void ConversationController::confirmDelete()
{
    if(m_deleteTarget.isEmpty() || m_deleteLoading || m_deleting) return;
    const auto id=m_deleteTarget,base=m_tasks->baseUrl().toString(),key=base+'/'+id;
    const auto generation=m_deleteGeneration;
    m_removing.insert(key); m_deleting=true; m_deleteError.clear();
    ++m_listGeneration; m_historyLoading=false;
    if(m_id==id) {
        stopAnswerStream(true);
        ++m_generation; m_loading=false; m_editing=false;
        resetRequestPolling(); m_finishedRequest.clear();
        closeMcpTools();
        m_tasks->setConversationFilter({}); // Detach HTTP/SSE and all task projections before deletion.
    }
    emit changed(); emit historyChanged();
    request("DELETE","/api/v1/conversations/"+id+(m_deleteActive?"?stop_active=true":""),{},[=,this](const QJsonObject &body,int status,const QString &error){
        m_removing.remove(key);
        const bool success=error.isEmpty() && body.value("deleted").toBool() && body.value("conversation_id").toString()==id;
        if(success) { m_deleted.insert(key); m_pending.remove(key); }
        if(generation!=m_deleteGeneration || base!=m_tasks->baseUrl().toString()) return;
        m_deleting=false;
        if(success) {
            m_deleteTarget.clear(); m_deleteTitle.clear(); m_deleteError.clear();
            forgetConversation(id);
        } else {
            m_deleteError=error.isEmpty()?"删除响应无效，请重试以确认结果。":error;
            if(body.value("error").toObject().value("code").toString()=="api_internal") m_deleteError="删除未完成，后端处理发生错误，请重试。";
            if(status==409 && body.value("error").toObject().value("code").toString()=="conversation_busy") m_deleteActive=true;
            if(status==0 || status>=500 || error.isEmpty()) m_deleteError+=" 删除结果尚未确认，可以安全重试。";
            if(m_id==id) { m_detail["active_request"]=QJsonValue::Null; m_tasks->setConversationFilter(id); refresh(); }
            emit changed();
        }
    });
}
void ConversationController::cancelDelete()
{
    if(m_deleting) return;
    ++m_deleteGeneration; m_deleteTarget.clear(); m_deleteTitle.clear(); m_deleteError.clear(); m_deleteLoading=false;
    emit changed();
}
void ConversationController::forgetConversation(const QString &identity)
{
    const QString id=identity; // Caller may pass m_id, which selection reset mutates.
    const auto key=m_tasks->baseUrl().toString()+'/'+id;
    m_deleted.insert(key); m_pending.remove(key); m_sending.remove(id); m_removing.remove(key);
    ++m_listGeneration; m_historyLoading=false;
    for(qsizetype i=m_history.size();i>0;--i) if(m_history[i-1].toMap().value("conversation_id").toString()==id) m_history.removeAt(i-1);
    m_cursor.clear(); m_historyTotal=qMax(0,m_historyTotal-1);
    if(m_id==id) selectConversation({});
    emit conversationDeleted(id); emit historyChanged(); emit changed();
    refreshHistory();
}
