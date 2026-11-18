#include "ApprovalController.h"
#include "MissionProtocol.h"

ApprovalController::ApprovalController(AgentApiClient *api, QObject *parent) : QObject(parent), m_api(api)
{
    connect(api, &AgentApiClient::responseReceived, this,
        [this](quint64 request, const QString &, const QString &task, const QJsonObject &body) {
        if (request != m_request || task != m_mission.value("task_id").toString()) return;
        const auto current = continuation();
        m_request = 0; m_enabled = false;
        emit snapshotReceived(body);
        if (!current()) return;
        emit changed();
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
    m_request = 0; m_mission = {}; m_enabled = false; m_stale = false; m_error.clear(); emit changed();
}
void ApprovalController::setMission(const QJsonObject &mission)
{
    ++m_generation;
    if (mission.value("task_id") != m_mission.value("task_id")) {
        if (m_api) m_api->abortRequest(m_request);
        m_request = 0; m_enabled = false; m_error.clear();
    }
    m_mission = mission; m_stale = false; emit changed();
}
void ApprovalController::setEnabled(bool enabled)
{ if (m_enabled != enabled) { m_enabled = enabled; emit changed(); } }
bool ApprovalController::available() const
{
    const auto review = m_mission.value("review").toObject();
    return m_api && m_enabled && !m_stale && !busy()
        && m_mission.value("status") == "awaiting_human"
        && !m_mission.value("recovery_required").toBool()
        && !review.isEmpty() && review.value("revision") == m_mission.value("current_revision");
}
void ApprovalController::approve() { submit("approved"); }
void ApprovalController::reject() { submit("rejected"); }
void ApprovalController::submit(const QString &decision)
{
    if (!available()) { m_error = "请先刷新并审阅当前版本的任务、计划和范围。"; emit changed(); return; }
    const auto review = m_mission.value("review").toObject();
    QJsonObject body{{"approval_type", "execution"}, {"expected_revision", m_mission.value("current_revision")},
        {"task_spec_hash", review.value("task_spec_hash")}, {"plan_hash", review.value("plan_hash")},
        {"scope_hash", review.value("scope_hash")}, {"decision", decision}};
    m_error.clear();
    m_request = m_api->approveMission(m_mission.value("task_id").toString(), body);
    emit changed();
}
