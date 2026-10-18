#include "TaskController.h"
#include "Protocol.h"
#include <QJsonArray>
#include <QJsonDocument>
#include <QSet>

TaskController::TaskController(QObject *parent) : QObject(parent)
{
    connect(this, &TaskController::taskChanged, this, &TaskController::feedbackChanged);
    connect(&m_api, &AgentApiClient::responseReceived, this,
        [this](quint64 request, const QString &operation, const QString &requestedTask, const QJsonObject &body) {
        if (!m_requests.contains(request) || m_requests.take(request) != m_generation) return;
        if (m_mutation == request) m_mutation = 0;
        const bool feedbackReply = operation == "feedback" && request == m_feedbackRequest;
        if (feedbackReply) m_feedbackRequest = 0;
        if (operation == "list") {
            if (request != m_lastListRequest) return;
            m_historyLoading=false;
            if (!body.value("items").isArray()) { setError("任务列表响应格式无效。"); return; }
            if(!m_appendHistory) m_tasks.clear();
            for(const auto &item:body.value("items").toArray()) {
                auto row=item.toObject().toVariantMap();
                if(row.value("task_id").toString()==m_taskId &&
                    Protocol::sequence(row.value("last_event_id").toString(),m_taskId)<m_revision) {
                    // A list request may complete after newer SSE data has updated this task.
                    for(const auto &key:{"state","outcome","error","last_event_id"})
                        row.insert(key,m_snapshot.value(key).toVariant());
                }
                bool found=false;
                for(auto &existing:m_tasks) if(existing.toMap().value("task_id")==row.value("task_id")) { existing=row; found=true; break; }
                if(!found) m_tasks.append(row);
            }
            m_historyCursor=body.value("next_cursor").toString(); m_historyTotal=body.value("total").toInt(int(m_tasks.size()));
            m_serverStatus="connected"; emit serverStatusChanged(); emit tasksChanged(); return;
        }
        if(operation=="trace" || operation=="trace_detail") {
            if(requestedTask!=m_taskId) return;
            if(operation=="trace" && request==m_traceRequest) {
                m_traceLoading=false;
                for(const auto &item:body.value("items").toArray()) {
                    const auto row=item.toObject(); m_storedTrace.upsert(QString::number(row.value("sequence").toInteger()),row.toVariantMap());
                }
                m_traceCursor=body.value("next_cursor").toInteger();
            } else if(operation=="trace_detail" && request==m_traceDetailRequest) {
                m_traceDetailText=QString::fromUtf8(QJsonDocument(body).toJson(QJsonDocument::Indented));
            }
            emit traceChanged(); return;
        }
        const auto id = body.value("task_id").toString();
        if (!Protocol::validTaskId(id) || !Protocol::validState(body.value("state").toString())
            || id != m_taskId || requestedTask != m_taskId) {
            if (feedbackReply) {
                m_feedbackUnconfirmed = m_feedbackNeedsRefresh = true;
                refreshTask();
            }
            setError("任务响应中的标识或状态无效。"); emit taskChanged(); return;
        }
        if (feedbackReply) {
            if (body.value("state") != "planning" || body.value("plan_version").toInt() != m_feedbackSourceVersion
                || Protocol::sequence(body.value("last_event_id").toString(), id) < 0) {
                m_feedbackUnconfirmed = m_feedbackNeedsRefresh = true;
                setError("反馈受理响应与本次提交不符，请核对完整执行记录。");
                refreshTask(); emit taskChanged(); return;
            }
            clearFeedback(); m_feedbackNeedsRefresh = false; m_hasFeedback = true;
        }
        const bool applied = applySnapshot(body);
        if (operation == "get" && applied) m_feedbackNeedsRefresh = false;
        emit taskChanged();
    });
    connect(&m_api, &AgentApiClient::requestError, this,
        [this](quint64 request, const QString &operation, const QString &, int status, const QString &code, const QString &message) {
        if (!m_requests.contains(request) || m_requests.take(request) != m_generation) return;
        if (m_mutation == request) m_mutation = 0;
        if (operation == "feedback" && request == m_feedbackRequest) {
            m_feedbackRequest = 0;
            m_feedbackUnconfirmed = m_feedbackUnconfirmed || status == 0 || code == "network_error"
                || code == "invalid_response" || code == "response_limit";
            if (m_feedbackUnconfirmed || status == 409) {
                m_feedbackNeedsRefresh = true;
                refreshTask(); // A GET synchronizes state; it never acknowledges this draft.
            }
        }
        if(operation=="list" && request!=m_lastListRequest) return;
        if(operation=="list") { m_historyLoading=false; emit tasksChanged(); }
        if(operation=="trace" && request==m_traceRequest) { m_traceLoading=false; emit traceChanged(); }
        if(operation=="list" || status==0) { m_serverStatus="error"; emit serverStatusChanged(); }
        setError(errorSummary(code) + "\n原始错误（" + code + "）：" + message); emit taskChanged();
    });
    connect(&m_sse, &SseClient::eventApplied, this, &TaskController::applyEvent);
    connect(&m_sse, &SseClient::connectionError, this, [this](const QString &message) {
        m_connectionWarning=message; emit connectionStateChanged();
    });
    connect(&m_sse, &SseClient::connectionStateChanged, this, [this] {
        if(m_sse.connectionState()=="connected" || m_sse.connectionState()=="ended") m_connectionWarning.clear();
        emit connectionStateChanged();
    });
    connect(&m_sse, &SseClient::streamEnded, this, &TaskController::refreshTask);
}
void TaskController::setError(const QString &message)
{ if (m_error != message) { m_error = message; emit errorChanged(); } }
void TaskController::setBaseUrl(const QUrl &url)
{
    if (url == baseUrl()) return;
    if (!Protocol::validBaseUrl(url)) { setError("请使用本机 HTTP 地址，不包含凭据、路径、查询参数或片段。"); return; }
    resetSelection(); m_api.setBaseUrl(url); m_sse.setBaseUrl(url);
    m_serverStatus="disconnected"; emit serverStatusChanged();
    m_tasks.clear(); m_historyCursor.clear(); m_historyTotal=0; emit tasksChanged(); emit baseUrlChanged();
}
void TaskController::setRepositoryPath(const QString &path)
{ if (path != m_repository) { m_repository = path; m_repositoryModel.setRoot(path); emit repositoryPathChanged(); emit taskChanged(); } }
void TaskController::resetSelection()
{
    ++m_generation; m_requests.clear(); m_mutation = 0;
    m_feedbackRequest = 0; m_hasFeedback = false; m_feedbackNeedsRefresh = false; m_feedbackPlanArrived = false; clearFeedback();
    m_api.abortPending(); m_sse.forgetTask(); m_eventSequence = 0;
    m_traces.clear(); m_openTools.clear(); m_historyReadOnly=false; m_connectionWarning.clear();
    m_storedTrace.clear(); m_traceCursor=0; m_traceDetailText.clear(); m_traceLoading=false; emit traceChanged();
    m_historyLoading=false; emit tasksChanged();
    m_taskId.clear(); m_snapshot = {}; m_snapshotTime.clear(); m_revision = 0;
    setError({}); emit taskChanged();
}
void TaskController::watch(quint64 request, bool mutation)
{
    m_requests.insert(request, m_generation);
    if (mutation) { m_mutation = request; emit taskChanged(); }
}
bool TaskController::canCancel() const
{ return !m_historyReadOnly && (m_mutation == 0 || m_mutation == m_feedbackRequest) && !m_taskId.isEmpty() && !Protocol::terminal(state()); }
void TaskController::setConversationFilter(const QString &id)
{
    resetSelection(); m_conversationFilter=id; m_conversationScoped=true;
    m_tasks.clear(); m_historyCursor.clear(); m_historyTotal=0; emit tasksChanged();
    // An empty conversation view must not expose unrelated task history.
    if(!id.isEmpty()) refreshTasks();
}
void TaskController::viewHistoryTask(const QString &id)
{ if (!Protocol::validTaskId(id)) { setError("任务标识无效。"); return; } selectTask(id); m_historyReadOnly=true; emit taskChanged(); }
void TaskController::openExecutionView()
{ if(!m_taskId.isEmpty() && !Protocol::terminal(state()) && !m_snapshot.value("read_only").toBool()) { m_historyReadOnly=false; emit taskChanged(); } }
bool TaskController::configureBackend(const QString &url)
{
    const QUrl parsed(url);
    if(!Protocol::validBaseUrl(parsed)) { setError("请输入本机 HTTP 地址，例如 http://127.0.0.1:8765。"); return false; }
    setBaseUrl(parsed); clearError(); refreshTasks(); return true;
}
void TaskController::approvePlan()
{
    if (!canApprove()) { setError("当前没有可批准的待审批方案。"); return; }
    setError({}); watch(m_api.approveTask(m_taskId, planVersion()), true);
}
void TaskController::clearFeedback()
{
    m_feedbackOpen = false; m_feedbackDraft.clear(); m_feedbackTask.clear();
    m_feedbackSourceVersion = 0; m_feedbackUnconfirmed = false;
    emit feedbackChanged();
}
void TaskController::openPlanFeedback()
{
    if (!canFeedback() || m_feedbackOpen) return;
    m_feedbackOpen = true; m_feedbackTask = m_taskId; m_feedbackSourceVersion = planVersion();
    emit feedbackChanged();
}
void TaskController::closePlanFeedback()
{ if (!feedbackSending()) clearFeedback(); }
void TaskController::setFeedbackDraft(const QString &text)
{
    if (!m_feedbackOpen || feedbackSending() || text == m_feedbackDraft) return;
    m_feedbackDraft = text; emit feedbackChanged();
}
int TaskController::feedbackCharacterCount(const QString &text) const
{ return int(text.toUcs4().size()); }
QString TaskController::feedbackValidationError(const QString &text) const
{
    const auto points = text.toUcs4(); // Python len counts Unicode code points, not UTF-16 units.
    if (points.size() > 4000) return "修改意见最多 4000 字符。";
    if (text.contains(QChar(0))) return "修改意见不能包含 NUL 字符。";
    bool nonblank = false;
    for (auto c : points) {
        // Match Python str.isspace(), including the four C0 information separators.
        const bool space = (c >= 9 && c <= 13) || (c >= 28 && c <= 32) || c == 0x85 || c == 0xa0
            || c == 0x1680 || (c >= 0x2000 && c <= 0x200a) || c == 0x2028 || c == 0x2029
            || c == 0x202f || c == 0x205f || c == 0x3000;
        nonblank |= !space;
    }
    return nonblank ? QString{} : QString("请输入非空白的修改意见。");
}
QString TaskController::feedbackNotice() const
{
    if (!m_feedbackOpen) return {};
    QStringList lines;
    if (m_feedbackUnconfirmed)
        lines << "未收到本次意见的受理确认。请到完整执行记录核对；任务状态或新版本不能证明本次意见已受理。草稿将保留到确认成功或您收起为止。";
    if (m_feedbackSourceVersion != planVersion())
        lines << "方案版本已变化，旧意见不能提交到新版本。请先复制需要保留的内容，收起后重新打开反馈入口。";
    else if (m_feedbackNeedsRefresh)
        lines << "正在核对任务状态；读取失败时请刷新任务，确认仍为原待审批版本后再显式重试。";
    return lines.join('\n');
}
void TaskController::submitPlanFeedback(const QString &feedback)
{
    if (!canFeedback() || !m_feedbackOpen || m_feedbackTask != m_taskId || m_feedbackSourceVersion != planVersion()) {
        setError("只能向打开意见输入时的待审批版本提交；版本变化后请收起并重新打开。"); return;
    }
    const auto problem = feedbackValidationError(feedback);
    if (!problem.isEmpty()) { setError(problem); return; }
    setFeedbackDraft(feedback); setError({}); m_hasFeedback = true; m_feedbackPlanArrived = false;
    m_feedbackRequest = m_api.feedbackTask(m_taskId, m_feedbackSourceVersion, feedback);
    watch(m_feedbackRequest, true);
}
void TaskController::cancelTask()
{
    if (!canCancel()) { setError("当前没有可取消的活动任务。"); return; }
    setError({}); watch(m_api.cancelTask(m_taskId), true);
}
void TaskController::rejectPlan()
{
    if (!canApprove()) { setError("当前没有可拒绝的待审批方案。"); return; }
    setError({}); watch(m_api.cancelTask(m_taskId, true, planVersion()), true);
}
void TaskController::selectTask(const QString &id)
{
    if (!Protocol::validTaskId(id)) { setError("任务标识无效。"); return; }
    resetSelection(); m_taskId = id; emit taskChanged();
    refreshTask(); m_sse.connectToTask(id);
}
void TaskController::refreshTask()
{ if (!m_taskId.isEmpty()) watch(m_api.getTask(m_taskId)); }
void TaskController::refreshTasks()
{
    if(m_conversationScoped && m_conversationFilter.isEmpty()) return;
    m_serverStatus="checking"; emit serverStatusChanged(); m_appendHistory=false; m_historyLoading=true; emit tasksChanged();
    m_lastListRequest = m_api.listTasks({},m_conversationFilter); watch(m_lastListRequest);
}
void TaskController::loadMoreTasks()
{
    if(m_historyLoading || m_historyCursor.isEmpty()) return;
    m_appendHistory=true; m_historyLoading=true; emit tasksChanged(); m_lastListRequest=m_api.listTasks(m_historyCursor,m_conversationFilter); watch(m_lastListRequest);
}
void TaskController::refreshTrace()
{
    if(m_taskId.isEmpty()) return;
    m_storedTrace.clear(); m_traceCursor=0; m_traceLoading=true; m_traceDetailText.clear(); emit traceChanged();
    m_traceRequest=m_api.getTrace(m_taskId); watch(m_traceRequest);
}
void TaskController::loadMoreTrace()
{
    if(m_taskId.isEmpty() || m_traceLoading || m_traceCursor<=0) return;
    m_traceLoading=true; emit traceChanged(); m_traceRequest=m_api.getTrace(m_taskId,m_traceCursor); watch(m_traceRequest);
}
void TaskController::loadTraceDetail(qint64 sequence)
{
    if(m_taskId.isEmpty() || sequence<=0) return;
    m_traceDetailText.clear(); emit traceChanged(); m_traceDetailRequest=m_api.getTraceDetail(m_taskId,sequence); watch(m_traceDetailRequest);
}
void TaskController::reconnectEvents()
{
    if (!m_taskId.isEmpty()) { setError({}); m_sse.connectToTask(m_taskId, m_sse.lastEventId()); }
}
void TaskController::notifyTransition(const QString &oldState, int oldVersion)
{
    for(auto &item:m_tasks) {
        auto row=item.toMap();
        if(row.value("task_id").toString()==m_taskId) {
            row.insert("state",state()); row.insert("outcome",m_snapshot.value("outcome").toVariant()); item=row;
            emit tasksChanged(); break;
        }
    }
    emit taskChanged();
    if (planVersion() != oldVersion && !plan().isEmpty()) emit planAvailable();
    if (canApprove() && (state() != oldState || planVersion() != oldVersion)) emit approvalRequired();
    if (state() != oldState && Protocol::terminal(state())) emit taskFinished(state());
}
bool TaskController::applySnapshot(const QJsonObject &body)
{
    const auto revision = Protocol::sequence(body.value("last_event_id").toString(), m_taskId);
    const auto timestamp = body.value("updated_at").toString();
    if (revision < 0) { setError("任务快照的事件游标无效。"); return false; }
    if (revision < m_revision || (revision == m_revision && timestamp < m_snapshotTime) || body.value("plan_version").toInt() < planVersion()) return false;
    const auto oldState = state(); const auto oldVersion = planVersion();
    if (body.value("state") == "planning") {
        if (oldState == "waiting_approval") { m_hasFeedback = true; m_feedbackPlanArrived = false; }
        if (body.value("plan_version").toInt() > 1) m_hasFeedback = true;
        if (m_hasFeedback && oldVersion > 0 && body.value("plan_version").toInt() > oldVersion) m_feedbackPlanArrived = true;
    }
    m_snapshot = body; m_revision = revision; m_snapshotTime = timestamp;
    if(body.value("read_only").toBool()) m_historyReadOnly=true;
    setRepositoryPath(body.value("repository_path").toString());
    notifyTransition(oldState, oldVersion);
    return true;
}
void TaskController::applyEvent(const QString &id, const QJsonObject &payload)
{
    const auto revision = Protocol::sequence(id, m_taskId);
    if (payload.value("task_id").toString() != m_taskId || revision <= m_eventSequence) return;
    // SseClient validates contiguous IDs. Keep a projection cursor separate from the
    // snapshot revision so replay fills tool traces without retaining every payload.
    m_eventSequence = revision;
    projectEvent(id,payload);
    // Replay can establish the reason for a planning snapshot, without regressing it.
    const auto transition = payload.value("data").toObject();
    if (payload.value("event") == "task_state_changed" && transition.value("from") == "waiting_approval"
        && transition.value("state") == "planning") {
        m_hasFeedback = true; m_feedbackPlanArrived = false; emit feedbackChanged();
    } else if (payload.value("event") == "plan_created" && m_hasFeedback) {
        // Host can publish the new plan before MCP cleanup permits approval.
        m_feedbackPlanArrived = true; emit feedbackChanged();
    }
    if (revision <= m_revision) return;
    const auto oldState = state(); const auto oldVersion = planVersion();
    const auto event = payload.value("event").toString();
    const auto data = payload.value("data").toObject();
    m_revision = revision; m_snapshotTime.clear();
    m_snapshot.insert("last_event_id", id);
    if (data.contains("repair_round")) m_snapshot.insert("repair_round", data.value("repair_round"));
    if (event == "task_state_changed") {
        for (const auto &key : {"state", "outcome", "error"}) m_snapshot.insert(key, data.value(key));
    } else if (event == "plan_created") {
        if (data.value("plan_version").toInt() >= planVersion()) {
            m_snapshot.insert("plan", data.value("plan")); m_snapshot.insert("plan_version", data.value("plan_version"));
        }
    } else if (event == "rag_usage") {
        const auto status = data.value("rag_status").toString();
        static const QSet<QString> allowed{"not_requested", "unavailable", "available", "used", "degraded"};
        if (data.value("rag_used").isBool() && allowed.contains(status)) {
            m_snapshot.insert("rag_used", data.value("rag_used"));
            m_snapshot.insert("rag_status", status);
        }
    } else if (event == "build_started" || event=="test_started") {
        auto report=m_snapshot.value("build_result").toObject();
        if(data.value("repair_round").toInt()>report.value("repair_round").toInt()) {
            report=QJsonObject{{"selection",report.value("selection")},{"commands",QJsonArray{}},
                {"stages",QJsonObject{{"cmake_configure","not_run"},{"cmake_build","not_run"},{"run_tests","not_run"}}},
                {"tests",QJsonObject{{"status","not_run"}}}};
        }
        report.insert("repair_round",data.value("repair_round"));
        auto stages=report.value("stages").toObject(); stages.insert(data.value("phase").toString(),"running");
        report.insert("stages",stages); m_snapshot.insert("build_result",report);
    }
    else if (event == "build_finished" || event == "test_finished") {
        m_snapshot.insert("build_result", data.value("result"));
        const auto result=data.value("result").toObject();
        auto history=m_snapshot.value("validation_history").toArray();
        bool replaced=false;
        for(qsizetype i=0;i<history.size();++i) {
            if(history[i].toObject().value("repair_round")==result.value("repair_round")) {
                history[i]=result; replaced=true; break;
            }
        }
        if(!replaced) history.append(result);
        m_snapshot.insert("validation_history",history);
        if (event == "build_finished") emit buildFinished(data.value("success").toBool());
        else emit testFinished(data.value("success").toBool());
    } else if (event == "task_completed" || event == "task_failed") {
        m_snapshot.insert("state", event == "task_completed" ? "completed" : "failed");
        for (const auto &key : {"final_result", "error", "outcome"}) m_snapshot.insert(key, data.value(key));
    }
    notifyTransition(oldState, oldVersion);
}
