#include "DeliveryController.h"
#include "MissionProtocol.h"
#include <QRegularExpression>

DeliveryController::DeliveryController(AgentApiClient *api, QObject *parent) : QObject(parent), m_api(api)
{
    connect(api, &AgentApiClient::responseReceived, this,
        [this](quint64 request, const QString &, const QString &task, const QJsonObject &body) {
        if (request != m_request || task != m_mission.value("task_id").toString()) return;
        const auto current = continuation();
        m_request = 0; m_enabled = false;
        const auto mission = body.value("mission").toObject();
        if (m_confirming) { m_result = body.value("delivery").toObject(); m_preview = {}; m_previewRevision = 0; }
        else { m_preview = body.value("delivery").toObject(); m_previewRevision = mission.value("current_revision").toInteger(); }
        // Invalidate the coordinator's snapshot before any availability notification.
        emit snapshotReceived(mission);
        if (current()) emit changed();
    });
    connect(api, &AgentApiClient::requestError, this,
        [this](quint64 request, const QString &, const QString &, int status, const QString &code, const QString &) {
        if (request != m_request) return;
        const auto current = continuation();
        m_request = 0; m_enabled = false; m_preview = {}; m_previewRevision = 0;
        m_error = MissionProtocol::errorMessage(code, status);
        emit refreshRequired();
        if (current()) emit changed();
    });
}
DeliveryController::~DeliveryController() { if (m_api) m_api->abortRequest(m_request); }
std::function<bool()> DeliveryController::continuation()
{
    QPointer<DeliveryController> guard(this); const auto generation = m_generation;
    return [guard, generation] { return guard && guard->m_generation == generation; };
}
void DeliveryController::clear()
{
    ++m_generation;
    if (m_api) m_api->abortRequest(m_request);
    m_request = 0; m_enabled = false; m_mission = {}; m_preview = {}; m_result = {};
    m_previewRevision = 0; m_mode.clear(); m_target.clear(); m_error.clear(); emit changed();
}
void DeliveryController::invalidatePreview()
{
    if (m_preview.isEmpty() && m_previewRevision == 0) return;
    m_preview = {}; m_previewRevision = 0; emit changed();
}
void DeliveryController::invalidatePreviewBefore(qint64 revision)
{ if (revision > m_previewRevision) invalidatePreview(); }
void DeliveryController::setMission(const QJsonObject &mission)
{
    ++m_generation;
    if (mission.value("task_id") != m_mission.value("task_id")) {
        if (m_api) m_api->abortRequest(m_request);
        m_request = 0; m_enabled = false; m_preview = {}; m_result = {};
        m_previewRevision = 0; m_mode.clear(); m_target.clear(); m_error.clear();
    }
    if (mission.value("current_revision").toInteger() != m_previewRevision) { m_preview = {}; m_previewRevision = 0; }
    m_mission = mission; emit changed();
}
void DeliveryController::setEnabled(bool enabled)
{ if (m_enabled != enabled) { m_enabled = enabled; emit changed(); } }
bool DeliveryController::canPreview() const
{ return m_api && m_enabled && !busy() && !m_mission.isEmpty() && !m_mission.value("recovery_required").toBool(); }
bool DeliveryController::canConfirm() const
{
    static const QRegularExpression hash("^[0-9a-f]{64}$");
    return canPreview() && !m_mode.isEmpty() && m_previewRevision == m_mission.value("current_revision").toInteger()
        && hash.match(m_preview.value("token").toString()).hasMatch()
        && m_preview.value("task_id") == m_mission.value("task_id");
}
QString DeliveryController::abandonBlockReason() const
{
    if (!m_mission.isEmpty() && !MissionProtocol::terminal(m_mission.value("status").toString()))
        return MissionProtocol::errorMessage("mission_cancel_required", 409);
    return {};
}
// Keep the selected operation visible while its preview or confirmation is in flight.
QString DeliveryController::previewMode() const
{ return busy() || !m_preview.isEmpty() ? m_mode : QString{}; }
QString DeliveryController::previewTarget() const
{ return busy() || !m_preview.isEmpty() ? m_target : QString{}; }
void DeliveryController::prepare(const QString &mode, const QString &targetBranch)
{
    const QStringList modes{"squash", "keep_branch", "patch", "bundle", "abandon"};
    if (mode == "abandon" && !abandonBlockReason().isEmpty()) {
        m_error = abandonBlockReason(); emit changed(); return;
    }
    if (!canPreview() || !modes.contains(mode) || (mode != "abandon" && m_mission.value("status") != "succeeded")) {
        m_error = "当前任务不可执行所选交付操作，请刷新任务状态。"; emit changed(); return;
    }
    m_preview = {}; m_previewRevision = 0; m_result = {}; m_mode = mode;
    m_target = targetBranch; m_error.clear(); m_confirming = false;
    QJsonObject body{{"expected_revision", m_mission.value("current_revision")}, {"mode", mode}};
    if (!targetBranch.isEmpty()) body.insert("target_branch", targetBranch);
    m_request = m_api->deliverMission(m_mission.value("task_id").toString(), body); emit changed();
}
void DeliveryController::confirm()
{
    if (!canConfirm()) { m_error = "交付预览已失效，请重新预览后确认。"; emit changed(); return; }
    QJsonObject body{{"expected_revision", m_mission.value("current_revision")}, {"mode", m_mode}, {"token", m_preview.value("token")}};
    if (!m_target.isEmpty()) body.insert("target_branch", m_target);
    m_confirming = true; m_error.clear();
    m_request = m_api->deliverMission(m_mission.value("task_id").toString(), body);
    m_preview = {}; m_previewRevision = 0; emit changed();
}
