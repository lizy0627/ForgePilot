#include "ApprovalController.h"
#include "MissionProtocol.h"
#include "MissionSnapshotLoader.h"
#include <QJsonArray>

ApprovalController::ApprovalController(AgentApiClient *api, QObject *parent) : QObject(parent), m_api(api)
{
    connect(api, &AgentApiClient::responseReceived, this,
        [this](quint64 request, const QString &, const QString &task, const QJsonObject &body) {
        if (request != m_request || task != m_mission.value("task_id").toString()) return;
        const auto current = continuation();
        const bool scopeApproved = m_approvingScope;
        m_request = 0; m_enabled = false;
        emit snapshotReceived(body);
        if (!current()) return;
        if (scopeApproved) emit scopeExpansionApproved(task);
        if (current()) emit changed();
    });
    connect(api, &AgentApiClient::requestError, this,
        [this](quint64 request, const QString &, const QString &, int status, const QString &code, const QString &) {
        if (request != m_request) return;
        const auto current = continuation();
        m_request = 0; m_enabled = false; m_stale = true;
        m_error = MissionProtocol::errorMessage(code, status);
        // Even an ambiguous transport failure must be inspected, never resubmitted.
        emit refreshRequired();
        if (current()) emit changed();
    });
}
ApprovalController::~ApprovalController() { if (m_api) m_api->abortRequest(m_request); }
std::function<bool()> ApprovalController::continuation()
{
    QPointer<ApprovalController> guard(this); const auto generation = m_generation;
    return [guard, generation] { return guard && guard->m_generation == generation; };
}
void ApprovalController::clear()
{
    ++m_generation;
    if (m_api) m_api->abortRequest(m_request);
    m_request = 0; m_mission = {}; m_scopeExpansion = {};
    m_executionPending = m_scopePending = false;
    m_enabled = false; m_stale = false; m_error.clear(); emit changed();
}
void ApprovalController::setSnapshot(const MissionSnapshot &snapshot)
{
    const auto &mission = snapshot.mission;
    ++m_generation;
    if (mission.value("task_id") != m_mission.value("task_id")) {
        if (m_api) m_api->abortRequest(m_request);
        m_request = 0; m_enabled = false; m_error.clear();
    }
    m_mission = mission;
    m_executionPending = m_scopePending = false;
    m_scopeExpansion = {};
    const auto &scope = snapshot.scopeExpansion;
    for (const auto &entry : snapshot.graph.value("nodes").toArray()) {
        const auto node = entry.toObject();
        if (node.value("status") != "awaiting_human") continue;
        const auto action = MissionProtocol::pendingAction(node);
        if (action == "execution") m_executionPending = true;
        // The endpoint returns the latest proposal even when a later writer pauses without one.
        if (scope.value("task_id") != mission.value("task_id") || scope.value("revision") != mission.value("current_revision")
            || scope.value("node_id") != node.value("node_id") || scope.value("attempt") != node.value("attempt"))
            continue;
        if (action == "scope_expansion") {
            m_scopeExpansion = scope;
            m_scopePending = node.contains("pending_action") || !mission.value("recovery_required").toBool();
        } else if (node.value("pending_action").isNull() && scope.value("status") == "rejected") {
            m_scopeExpansion = scope;
        }
    }
    m_stale = false; emit changed();
}
void ApprovalController::setEnabled(bool enabled)
{ if (m_enabled != enabled) { m_enabled = enabled; emit changed(); } }
bool ApprovalController::canDecide() const
{
    return m_api && m_enabled && !m_stale && !busy()
        && m_mission.value("status") == "awaiting_human";
}
bool ApprovalController::available() const
{
    const auto review = m_mission.value("review").toObject();
    return canDecide() && m_executionPending && !m_mission.value("recovery_required").toBool()
        && !review.isEmpty() && review.value("revision") == m_mission.value("current_revision");
}
bool ApprovalController::scopeAvailable() const
{
    return canDecide() && m_scopePending && m_scopeExpansion.value("status") == "pending";
}
void ApprovalController::approve() { submitExecution(true); }
void ApprovalController::reject() { submitExecution(false); }
void ApprovalController::submitExecution(bool approved)
{
    if (busy()) return;
    if (!available()) { m_error = "请先刷新并审阅当前版本的任务、计划和范围。"; emit changed(); return; }
    submit(m_mission.value("review").toObject(), "execution", approved);
}
void ApprovalController::decideScopeExpansion(const QVariantMap &reviewed, bool approved)
{
    if (busy()) return;
    if (!scopeAvailable() || QJsonObject::fromVariantMap(reviewed) != m_scopeExpansion) {
        const auto current = continuation();
        m_error = "扩围审批已失效，请查看刷新后的节点详情并重新决定。";
        m_stale = true;
        emit refreshRequired();
        if (current()) emit changed();
        return;
    }
    submit(m_scopeExpansion, "scope_expansion", approved);
}
void ApprovalController::submit(const QJsonObject &review, const QString &type, bool approved)
{
    QJsonObject body{{"approval_type", type}, {"expected_revision", review.value("revision")},
        {"task_spec_hash", review.value("task_spec_hash")}, {"plan_hash", review.value("plan_hash")},
        {"scope_hash", review.value("scope_hash")}, {"decision", approved ? "approved" : "rejected"}};
    m_error.clear();
    m_approvingScope = type == "scope_expansion" && approved;
    m_request = m_api->approveMission(m_mission.value("task_id").toString(), body);
    emit changed();
}
