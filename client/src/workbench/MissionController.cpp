#include "MissionController.h"
#include "MissionProtocol.h"
#include "Protocol.h"
#include <QRegularExpression>
#include <algorithm>

MissionController::MissionController(QObject *parent) : QObject(parent), m_approval(&m_api), m_delivery(&m_api)
{
    m_refreshTimer.setSingleShot(true);
    connect(&m_refreshTimer, &QTimer::timeout, this, &MissionController::beginRefresh);
    connect(&m_api, &AgentApiClient::responseReceived, this,
        [this](quint64 id, const QString &, const QString &task, const QJsonObject &body) { received(id, task, body); });
    connect(&m_api, &AgentApiClient::requestError, this,
        [this](quint64 id, const QString &, const QString &, int status, const QString &code, const QString &) { failed(id, status, code); });
    connect(&m_approval, &ApprovalController::snapshotReceived, this, &MissionController::mutationSnapshot);
    connect(&m_delivery, &DeliveryController::snapshotReceived, this, &MissionController::mutationSnapshot);
    connect(&m_approval, &ApprovalController::refreshRequired, this, &MissionController::refresh);
    connect(&m_delivery, &DeliveryController::refreshRequired, this, &MissionController::refresh);
    connect(&m_approval, &ApprovalController::changed, this, &MissionController::updateAvailability);
    connect(&m_delivery, &DeliveryController::changed, this, &MissionController::updateAvailability);
    connect(&m_sse, &SseClient::snapshotRequired, this, &MissionController::refresh);
    connect(&m_sse, &SseClient::connectionStateChanged, this, [this] {
        const auto current = continuation();
        if (m_sse.connectionState() == "recovering" || m_sse.connectionState() == "error") {
            m_synchronized = false; m_delivery.invalidatePreview();
            if (!current()) return;
            updateAvailability();
            if (!current()) return;
        }
        emit connectionStateChanged();
    });
    connect(&m_sse, &SseClient::connectionError, this,
        [this](const QString &message) { setError(message, true); });
    connect(&m_sse, &SseClient::streamEnded, this, &MissionController::refresh);
    connect(&m_sse, &SseClient::eventApplied, this, [this](const QString &id, const QJsonObject &event) {
        if (event.value("task_id").toString() != m_task) return;
        const auto current = continuation();
        const auto eventRevision = event.value("task_revision").toInteger();
        m_floor = std::max(m_floor, eventRevision);
        m_synchronized = false;
        // HTTP can publish a preview before SSE delivers the event that created it.
        // That same revision is already covered by the preview, not a new conflict.
        if (eventRevision > m_recovery.value("revision").toInteger()) m_recovery = {};
        m_delivery.invalidatePreviewBefore(eventRevision);
        if (!current()) return;
        scheduleRefresh();
        if (current()) emit eventReceived(id, event);
    });
}
MissionController::~MissionController()
{
    m_refreshTimer.stop(); m_requests.clear();
    QObject::disconnect(&m_sse, nullptr, this, nullptr);
    m_api.abortPending(); m_sse.disconnect();
}
std::function<bool()> MissionController::continuation()
{
    QPointer<MissionController> guard(this); const auto generation = m_generation;
    return [guard, generation] { return guard && guard->m_generation == generation; };
}
void MissionController::setError(const QString &message, bool streamError)
{
    m_streamError = streamError && !message.isEmpty();
    if (m_error != message) { m_error = message; emit errorChanged(); }
}
void MissionController::updateAvailability()
{
    if (m_updatingAvailability) return;
    const auto current = continuation();
    m_updatingAvailability = true;
    m_approval.setEnabled(m_synchronized && !busy());
    if (!current()) return;
    m_delivery.setEnabled(m_synchronized && !busy());
    if (!current()) return;
    m_updatingAvailability = false; emit availabilityChanged();
}
bool MissionController::canControl() const { return m_synchronized && !busy() && !m_task.isEmpty(); }
void MissionController::setBaseUrl(const QUrl &url)
{
    if (url == baseUrl()) return;
    if (!Protocol::validBaseUrl(url)) { setError("请输入不含凭据的本机 HTTP 后端地址。"); return; }
    QPointer<MissionController> guard(this); const auto nextGeneration = m_generation + 1;
    clearSelection();
    if (!guard || m_generation != nextGeneration) return;
    const auto current = continuation();
    m_missions.clear();
    if (!current()) return;
    m_api.setBaseUrl(url); m_sse.setBaseUrl(url);
    if (current()) emit baseUrlChanged();
}
void MissionController::clearSelection()
{
    ++m_generation;
    const auto current = continuation();
    m_refreshTimer.stop(); m_requests.clear(); m_mutation = 0; m_artifactRequest = 0;
    m_refreshing = false; m_synchronized = false; m_streamSelected = false;
    m_updatingAvailability = false;
    m_task.clear(); m_snapshot = {}; m_start = {}; m_recovery = {}; m_floor = 0; m_refreshAttempts = 0;
    m_api.abortPending(); m_sse.forgetTask();
    if (!current()) return;
    m_approval.clear();
    if (!current()) return;
    m_delivery.clear();
    if (!current()) return;
    m_graph.clear();
    if (!current()) return;
    m_artifacts.clear();
    if (!current()) return;
    m_evidence.clear();
    if (!current()) return;
    m_diff.clear();
    if (!current()) return;
    m_validation.clear();
    if (!current()) return;
    setError({});
    if (!current()) return;
    emit missionChanged();
    if (current()) updateAvailability();
}
void MissionController::selectMission(const QString &taskId)
{
    if (!Protocol::validTaskId(taskId)) { setError("任务标识无效。"); return; }
    QPointer<MissionController> guard(this); const auto nextGeneration = m_generation + 1;
    clearSelection();
    if (!guard || m_generation != nextGeneration) return;
    const auto current = continuation();
    m_task = taskId; emit missionChanged();
    if (current()) refresh();
}
void MissionController::createMission(const QVariantMap &request)
{
    if (busy()) return;
    QPointer<MissionController> guard(this); const auto nextGeneration = m_generation + 1;
    clearSelection();
    if (!guard || m_generation != nextGeneration) return;
    track(m_api.createMission(QJsonObject::fromVariantMap(request)), RequestKind::Create);
}
void MissionController::track(quint64 request, RequestKind kind)
{
    m_requests.insert(request, kind);
    if (kind == RequestKind::Mutation || kind == RequestKind::Create) m_mutation = request;
    updateAvailability();
}
void MissionController::refresh()
{
    if (m_task.isEmpty()) return;
    const auto current = continuation();
    setError({});
    if (current()) scheduleRefresh(0);
}
void MissionController::scheduleRefresh(int delay)
{
    if (m_task.isEmpty()) return;
    const auto current = continuation();
    m_synchronized = false; updateAvailability();
    if (!current()) return;
    if (!m_refreshing && !m_refreshTimer.isActive()) m_refreshTimer.start(delay);
}
void MissionController::beginRefresh()
{
    if (m_task.isEmpty() || m_refreshing) return;
    m_refreshing = true; m_synchronized = false; m_parts = 0;
    m_start = {}; m_graphSnapshot = {}; m_artifactSnapshot = {}; m_diffSnapshot = {}; m_validationSnapshot = {};
    track(m_api.getMission(m_task), RequestKind::Start);
}
void MissionController::abortRefresh()
{
    for (auto it = m_requests.begin(); it != m_requests.end();) {
        if (it.value() == RequestKind::Mutation || it.value() == RequestKind::Create || it.value() == RequestKind::Artifact) { ++it; continue; }
        m_api.abortRequest(it.key()); it = m_requests.erase(it);
    }
    m_refreshing = false; m_synchronized = false;
}
void MissionController::received(quint64 request, const QString &task, const QJsonObject &body)
{
    if (!m_requests.contains(request)) return; // Cancelled, switched or superseded.
    const auto current = continuation();
    const auto kind = m_requests.take(request);
    if (kind == RequestKind::Create) {
        m_mutation = 0; m_task = body.value("task_id").toString();
        m_missions.upsert(body);
        if (!current()) return;
        emit missionChanged();
        if (current()) mutationSnapshot(body);
        return;
    }
    if (task != m_task) return;
    if (kind == RequestKind::Artifact) {
        if (request == m_artifactRequest) { m_artifactRequest = 0; emit artifactLoaded(body.toVariantMap()); }
        return;
    }
    if (kind == RequestKind::Mutation) {
        m_mutation = 0;
        const auto mission = body.contains("mission") ? body.value("mission").toObject() : body;
        if (body.contains("recovery")) m_recovery = body.value("recovery").toObject();
        mutationSnapshot(mission); return;
    }
    if (kind == RequestKind::Start) {
        m_start = body;
        track(m_api.getMissionGraph(m_task), RequestKind::Graph);
        if (!current()) return;
        track(m_api.getMissionArtifacts(m_task), RequestKind::Artifacts);
        if (!current()) return;
        track(m_api.getMissionDiff(m_task), RequestKind::Diff);
        if (!current()) return;
        track(m_api.getMissionValidation(m_task), RequestKind::Validation); return;
    }
    if (kind == RequestKind::Fence) {
        const auto revision = body.value("current_revision").toInteger();
        if (revision < m_floor || revision != m_start.value("current_revision").toInteger()
            || revision != m_graphSnapshot.value("current_revision").toInteger()
            || body.value("last_event_id") != m_start.value("last_event_id")) {
            abortRefresh();
            scheduleRefresh(std::min(100 * ++m_refreshAttempts, 2000)); return;
        }
        commitSnapshot(body); return;
    }
    if (kind == RequestKind::Graph) m_graphSnapshot = body;
    if (kind == RequestKind::Artifacts) m_artifactSnapshot = body;
    if (kind == RequestKind::Diff) m_diffSnapshot = body;
    if (kind == RequestKind::Validation) m_validationSnapshot = body;
    if (++m_parts == 4) track(m_api.getMission(m_task), RequestKind::Fence);
}
void MissionController::commitSnapshot(const QJsonObject &mission)
{
    const auto current = continuation();
    m_snapshot = mission; m_floor = revision();
    m_graph.setRecoveryRequired(mission.value("recovery_required").toBool());
    if (!current()) return;
    m_graph.setSnapshot(m_graphSnapshot);
    if (!current()) return;
    m_artifacts.setSnapshot(m_artifactSnapshot);
    if (!current()) return;
    m_evidence.setSnapshot(m_artifactSnapshot);
    if (!current()) return;
    m_diff.setSnapshot(m_diffSnapshot);
    if (!current()) return;
    m_validation.setSnapshot(m_validationSnapshot);
    if (!current()) return;
    m_missions.upsert(mission);
    if (!current()) return;
    m_approval.setMission(mission);
    if (!current()) return;
    m_delivery.setMission(mission);
    if (!current()) return;
    if (m_recovery.value("revision").toInteger() != revision()) m_recovery = {};
    m_refreshing = false; m_refreshAttempts = 0; m_synchronized = true;
    // A read already in flight can recover the stream before its retry timer fires.
    // Clear only the connection warning; a later business error must remain visible.
    if (m_streamError) setError({});
    if (!current()) return;
    emit missionChanged();
    if (!current()) return;
    updateAvailability();
    if (!current()) return;
    if (MissionProtocol::terminal(status())) { m_streamSelected = false; m_sse.disconnect(); return; }
    const auto cursor = mission.value("last_event_id").toString();
    if (!m_streamSelected || m_sse.connectionState() == "disconnected" || m_sse.connectionState() == "ended" || m_sse.connectionState() == "error") {
        m_streamSelected = true; m_sse.connectToMission(m_task, cursor, revision());
    } else m_sse.adoptMissionSnapshot(m_task, cursor, revision());
}
void MissionController::mutationSnapshot(const QJsonObject &mission)
{
    if (mission.value("task_id").toString() != m_task) return;
    m_floor = std::max(m_floor, mission.value("current_revision").toInteger());
    // Discard the pre-mutation read bundle, even if its final GET arrives late.
    abortRefresh(); m_refreshTimer.stop(); scheduleRefresh(0);
}
void MissionController::failed(quint64 request, int status, const QString &code)
{
    if (!m_requests.contains(request)) return;
    const auto current = continuation();
    const auto kind = m_requests.take(request);
    setError(MissionProtocol::errorMessage(code, status));
    if (!current()) return;
    if (kind == RequestKind::Artifact) { m_artifactRequest = 0; return; }
    if (kind == RequestKind::Create) { m_mutation = 0; updateAvailability(); return; }
    if (kind == RequestKind::Mutation) {
        m_mutation = 0; m_synchronized = false; m_recovery = {}; m_delivery.invalidatePreview();
        if (!current()) return;
        abortRefresh(); m_refreshTimer.stop(); scheduleRefresh(0); return;
    }
    abortRefresh();
    if (status == 0 || status == 429 || status >= 500) scheduleRefresh(std::min(250 * ++m_refreshAttempts, 5000));
    if (current()) updateAvailability();
}
void MissionController::cancel()
{
    if (!canControl() || MissionProtocol::terminal(status())) return;
    track(m_api.cancelMission(m_task, revision()), RequestKind::Mutation);
}
void MissionController::retry(const QString &nodeId)
{
    if (!canControl() || m_snapshot.value("recovery_required").toBool()) return;
    track(m_api.retryMission(m_task, revision(), nodeId), RequestKind::Mutation);
}
void MissionController::prepareRecovery()
{
    if (!canControl() || MissionProtocol::terminal(status())) return;
    m_recovery = {}; track(m_api.resumeMission(m_task, revision()), RequestKind::Mutation);
}
void MissionController::confirmRecovery()
{
    static const QRegularExpression hash("^[0-9a-f]{64}$");
    if (!canControl() || m_recovery.value("task_id").toString() != m_task
        || m_recovery.value("revision").toInteger() != revision()
        || !m_recovery.value("blocked_reason").isNull()
        || !hash.match(m_recovery.value("token").toString()).hasMatch()) {
        setError("恢复预览已失效或存在阻塞，请重新检查恢复计划。"); return;
    }
    const auto token = m_recovery.value("token").toString(); m_recovery = {};
    track(m_api.resumeMission(m_task, revision(), token), RequestKind::Mutation);
}
void MissionController::loadArtifact(const QString &artifactId)
{
    if (m_task.isEmpty()) return;
    m_api.abortRequest(m_artifactRequest); m_requests.remove(m_artifactRequest);
    m_artifactRequest = m_api.getMissionArtifact(m_task, artifactId); track(m_artifactRequest, RequestKind::Artifact);
}
