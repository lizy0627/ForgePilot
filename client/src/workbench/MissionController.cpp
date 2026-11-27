#include "MissionController.h"
#include "MissionProtocol.h"
#include "Protocol.h"
#include <QJsonArray>
#include <QRegularExpression>
#include <algorithm>

MissionController::MissionController(QObject *parent) : QObject(parent), m_loader(&m_api), m_approval(&m_api), m_delivery(&m_api)
{
    m_skillDrafts.setContext(baseUrl(), m_task);
    const auto updateSkillDraftContext = [this] { m_skillDrafts.setContext(baseUrl(), m_task); };
    connect(this, &MissionController::missionChanged, this, updateSkillDraftContext);
    connect(this, &MissionController::baseUrlChanged, this, updateSkillDraftContext);
    m_recommendationTimer.setSingleShot(true);
    m_recommendationTimer.setInterval(250);
    connect(&m_recommendationTimer, &QTimer::timeout, this, [this] {
        m_recommendationBackend = baseUrl();
        m_recommendationRequest = m_creationApi.recommendSkills(m_recommendationBody, m_recommendationBackend);
    });
    connect(&m_creationApi, &AgentApiClient::responseReceived, this,
        [this](quint64 id, const QString &operation, const QString &, const QJsonObject &body) {
            if (operation == "mission.recommendations") {
                if (!currentRecommendation(id)) return;
                m_recommendationRequest = 0;
                m_recommendations = body.value("recommendations").toArray();
                QPointer<MissionController> guard(this);
                emit skillRecommendationsChanged();
                if (guard) emit recommendationsChanged();
                return;
            }
            if (operation != "mission.preview") return;
            const auto backend = m_previewBackends.take(id);
            if (id != m_previewRequest) {
                m_creationApi.discardMissionPreview(body.value("preview_token").toString(), backend);
                return;
            }
            m_creationPreviewBackend = backend;
            ++m_previewGeneration;
            m_previewRequest = 0; m_creationPreview = body; m_creationReady = true; m_creationError.clear();
            emit creationChanged();
        });
    connect(&m_creationApi, &AgentApiClient::requestError, this,
        [this](quint64 id, const QString &operation, const QString &, int status, const QString &, const QString &message) {
            if (operation == "mission.recommendations") {
                if (!currentRecommendation(id)) return;
                m_recommendationRequest = 0;
                m_recommendationError = status == 404 || status == 405
                    ? tr("当前后端未提供技能推荐，可直接创建普通任务。")
                    : tr("技能推荐读取失败，可直接创建普通任务。");
                emit recommendationsChanged();
                return;
            }
            if (operation != "mission.preview") return;
            m_previewBackends.remove(id);
            if (id != m_previewRequest) return;
            m_previewRequest = 0; m_creationReady = false; m_creationError = message;
            emit creationChanged();
        });
    connect(&m_historyApi, &AgentApiClient::responseReceived, this,
        [this](quint64 id, const QString &, const QString &, const QJsonObject &body) {
            if (id != m_historyRequest) return;
            const auto generation = m_historyGeneration;
            QPointer<MissionController> guard(this);
            m_historyRequest = 0; m_historyCursor = body.value("next_cursor").toString();
            m_historyTotal = body.value("total").toInt(); m_historyLoaded = true; m_historyError.clear();
            auto items = body.value("items").toArray();
            QJsonArray currentItems;
            for (const auto &item : items) {
                auto summary = item.toObject();
                if (summary.value("task_id").toString() == m_task && revision() > summary.value("current_revision").toInteger())
                    summary = m_snapshot;
                if (m_statusFilter.isEmpty() || summary.value("status").toString() == m_statusFilter) currentItems.append(summary);
            }
            m_missions.setPage(currentItems, m_historyAppend);
            if (guard && generation == m_historyGeneration) emit historyChanged();
        });
    connect(&m_historyApi, &AgentApiClient::requestError, this,
        [this](quint64 id, const QString &, const QString &, int status, const QString &code, const QString &) {
            if (id != m_historyRequest) return;
            m_historyRequest = 0; m_historyError = MissionProtocol::errorMessage(code, status);
            emit historyChanged();
        });
    connect(&m_loader, &MissionSnapshotLoader::snapshotReady, this, &MissionController::commitSnapshot);
    connect(&m_loader, &MissionSnapshotLoader::loadFailed, this, [this](int status, const QString &code) {
        const auto current = continuation();
        setError(MissionProtocol::errorMessage(code, status));
        if (current()) updateAvailability();
    });
    connect(&m_api, &AgentApiClient::responseReceived, this,
        [this](quint64 id, const QString &, const QString &task, const QJsonObject &body) {
            if (id == m_missionSkillsRequest && task == m_task) {
                m_missionSkillsRequest = 0;
                m_missionSkills = body;
                emit missionSkillsChanged();
                return;
            }
            received(id, task, body);
        });
    connect(&m_api, &AgentApiClient::requestError, this,
        [this](quint64 id, const QString &, const QString &task, int status, const QString &code, const QString &message) {
            if (id == m_missionSkillsRequest && task == m_task) {
                m_missionSkillsRequest = 0;
                m_missionSkillsError = (status == 404 && code != "unknown_mission") || status == 405
                    ? tr("当前后端未提供技能使用记录。") : MissionProtocol::errorMessage(code, status);
                emit missionSkillsChanged();
                return;
            }
            if (m_requests.value(id) == RequestKind::Create && m_requests.contains(id))
                creationFailed(id, status, code, message);
            else
                failed(id, status, code);
        });
    connect(&m_approval, &ApprovalController::snapshotReceived, this, &MissionController::mutationSnapshot);
    connect(&m_delivery, &DeliveryController::snapshotReceived, this, &MissionController::mutationSnapshot);
    connect(&m_approval, &ApprovalController::refreshRequired, this, [this] {
        m_loader.invalidate();
        refresh();
    });
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
        m_loader.observeRevision(eventRevision);
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
    m_loader.invalidate(); m_requests.clear();
    QObject::disconnect(&m_sse, nullptr, this, nullptr);
    m_api.abortPending(); m_historyApi.abortPending(); m_creationApi.abortPending(); m_sse.disconnect();
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
    if (url == baseUrl()) { if (!m_historyLoaded && !historyLoading()) refreshMissions(); return; }
    if (!Protocol::validBaseUrl(url)) { setError("请输入不含凭据的本机 HTTP 后端地址。"); return; }
    QPointer<MissionController> guard(this); const auto nextGeneration = m_generation + 1;
    ++m_historyGeneration; m_historyRequest = 0; m_historyApi.abortPending();
    m_historyCursor.clear(); m_historyError.clear(); m_historyTotal = 0; m_historyLoaded = false;
    clearSelection();
    if (!guard || m_generation != nextGeneration) return;
    const auto current = continuation();
    m_missions.clear();
    if (!current()) return;
    m_api.setBaseUrl(url); m_historyApi.setBaseUrl(url); m_sse.setBaseUrl(url);
    resetSkillContext();
    if (!current()) return;
    invalidateCreationPreview(true);
    if (!current()) return;
    emit baseUrlChanged();
    if (current()) refreshMissions();
}
void MissionController::setRepositoryFilter(const QString &path)
{
    const auto filter = path.trimmed();
    if (filter == m_repositoryFilter) return;
    m_repositoryFilter = filter;
    QPointer<MissionController> guard(this);
    m_missions.clear();
    if (!guard) return;
    m_historyLoaded = false; m_historyTotal = 0; refreshMissions();
}
void MissionController::setStatusFilter(const QString &status)
{
    if (status == m_statusFilter) return;
    if (!status.isEmpty() && !MissionProtocol::validState(status)) return;
    m_statusFilter = status;
    QPointer<MissionController> guard(this);
    m_missions.clear();
    if (!guard) return;
    m_historyLoaded = false; m_historyTotal = 0; refreshMissions();
}
void MissionController::refreshMissions()
{
    ++m_historyGeneration; m_historyRequest = 0; m_historyApi.abortPending();
    m_historyCursor.clear(); requestHistory(false);
}
void MissionController::loadMoreMissions()
{
    if (!historyLoading() && historyHasMore()) requestHistory(true);
}
void MissionController::requestHistory(bool append)
{
    m_historyAppend = append; m_historyError.clear();
    m_historyRequest = m_historyApi.listMissions(append ? m_historyCursor : QString{}, m_repositoryFilter, m_statusFilter);
    emit historyChanged();
}
void MissionController::observeMission(const QJsonObject &mission)
{
    // Repository identity is immutable and normalized by the server's history filter.
    // Comparing display paths here would reject aliases such as Windows slash/case variants.
    if (!m_statusFilter.isEmpty() && mission.value("status").toString() != m_statusFilter) {
        QPointer<MissionController> guard(this);
        m_missions.removeMission(mission.value("task_id").toString());
        if (guard && historyLoading()) refreshMissions();
        return;
    }
    // Detail selection never injects rows into a paginated result or a different filter.
    m_missions.upsert(mission, false);
}
void MissionController::clearSelection()
{
    ++m_generation;
    const auto current = continuation();
    m_loader.selectTask({}); m_requests.clear(); m_mutation = 0; m_artifactRequest = 0;
    m_synchronized = false; m_streamSelected = false;
    m_updatingAvailability = false;
    m_task.clear(); m_snapshot = {}; m_recovery = {};
    m_missionSkillsRequest = 0; m_missionSkills = {}; m_missionSkillsError.clear();
    m_api.abortPending(); m_sse.forgetTask();
    if (!current()) return;
    emit missionSkillsChanged();
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
    m_task = taskId; m_loader.selectTask(taskId); emit missionChanged();
    if (current()) refresh();
}
void MissionController::releaseCreationPreview()
{
    ++m_previewGeneration;
    m_previewRequest = 0;
    const auto token = m_creationPreview.take("preview_token").toString();
    if (!token.isEmpty()) m_creationApi.discardMissionPreview(token, m_creationPreviewBackend);
    m_creationReady = false;
}
void MissionController::invalidateCreationPreview(bool forget)
{
    releaseCreationPreview();
    m_creationError.clear();
    if (forget) m_creationPreview = {};
    emit creationChanged();
}
void MissionController::previewCreation(const QVariantMap &request)
{
    QPointer<MissionController> guard(this);
    const auto generation = m_previewGeneration + 1;
    const auto backend = baseUrl();
    invalidateCreationPreview();
    if (!guard || m_previewGeneration != generation || baseUrl() != backend) return;
    m_previewRequest = m_creationApi.previewMission(QJsonObject::fromVariantMap(request), backend);
    m_previewBackends.insert(m_previewRequest, backend);
    emit creationChanged();
}
void MissionController::createMission(const QVariantMap &request)
{
    if (busy()) return;
    auto body = QJsonObject::fromVariantMap(request);
    const auto token = body.value("preview_token").toString();
    if (body.contains("preview_token") && (!m_creationReady || m_creationPreviewBackend != baseUrl()
            || token != m_creationPreview.value("preview_token").toString())) {
        m_creationError = tr("预览已失效，请重新预览并确认后提交。");
        emit creationChanged();
        return;
    }
    const auto suppliedSelection = body.take("selected_skills");
    QJsonArray selected;
    if (!m_selectedSkillIds.isEmpty()) {
        if (m_recommendationBody.value("description") != body.value("request")
            || m_recommendationBody.value("task_type").toString() != body.value("task_type").toString("auto")
            || m_recommendationRepository != body.value("repository_path").toString()) {
            m_creationError = tr("任务输入已变化，请重新选择技能。");
            emit creationChanged();
            return;
        }
        for (const auto &entry : m_recommendations) {
            const auto skill = entry.toObject();
            if (m_selectedSkillIds.contains(skill.value("skill_id").toString()))
                selected.append(QJsonObject{{"skill_id", skill.value("skill_id")}, {"version", skill.value("version")},
                                            {"content_sha256", skill.value("content_sha256")}});
        }
        body.insert("selected_skills", selected);
    }
    if (!suppliedSelection.isUndefined() && suppliedSelection != selected) {
        m_creationError = tr("提交的技能与当前勾选不一致，请重新选择技能。");
        emit creationChanged();
        return;
    }
    const CreationSubmission submission{token, m_previewGeneration, m_skillContextGeneration, !selected.isEmpty()};
    QPointer<MissionController> guard(this); const auto nextGeneration = m_generation + 1;
    clearSelection();
    if (!guard || m_generation != nextGeneration) return;
    m_creationSubmission = submission;
    track(m_api.createMission(body), RequestKind::Create);
}

bool MissionController::submittedPreviewIsCurrent(const CreationSubmission &submission) const
{
    return !submission.token.isEmpty() && submission.previewGeneration == m_previewGeneration
        && submission.token == m_creationPreview.value("preview_token").toString();
}

void MissionController::creationFailed(quint64 request, int status, const QString &code, const QString &message)
{
    const auto current = continuation();
    const auto submission = m_creationSubmission;
    m_requests.remove(request);
    m_mutation = 0;

    if (submission.previewGeneration != m_previewGeneration) { updateAvailability(); return; }
    const bool sameDraft = submission.skillGeneration == m_skillContextGeneration;
    const bool changed = code == "skill_selection_changed";
    const bool unsupported = status == 404 || status == 405 || (status == 422 && code == "invalid_request");
    const bool refreshChoices = submission.withSkills && (changed || code == "invalid_skill_selection" || unsupported);
    const bool adjustSelection = submission.withSkills && (code == "skill_route_denied" || code == "skill_budget_exceeded");
    // A skill conflict can occur on either side of the backend's preview claim.
    if (submittedPreviewIsCurrent(submission) && (changed || (!refreshChoices && !adjustSelection)))
        releaseCreationPreview();
    if (sameDraft) {
        if (refreshChoices && unsupported)
            m_creationError = tr("后端拒绝了含技能的创建请求。请检查输入，重新选择技能或确认按普通任务创建。");
        else if (adjustSelection)
            m_creationError = MissionProtocol::errorMessage(code, status) + tr(" 请调整勾选后重新创建。");
        else
            m_creationError = code.startsWith("skill_") ? MissionProtocol::errorMessage(code, status) : message;
        const auto previewGeneration = m_previewGeneration;
        setError(MissionProtocol::errorMessage(code, status));
        if (!current()) return;
        if (refreshChoices && submission.skillGeneration == m_skillContextGeneration && previewGeneration == m_previewGeneration)
            refreshRecommendations();
        if (!current()) return;
    }
    emit creationChanged();
    if (current()) updateAvailability();
}

bool MissionController::currentRecommendation(quint64 request) const
{
    return request == m_recommendationRequest && baseUrl() == m_recommendationBackend;
}

void MissionController::updateSkillContext(const QString &description, const QString &taskType,
                                          const QString &repository, const QStringList &projectTags)
{
    QJsonObject body{{"description", description.trimmed()}, {"task_type", taskType}};
    if (!projectTags.isEmpty()) body.insert("project_tags", QJsonArray::fromStringList(projectTags));
    if (m_recommendationBody == body && m_recommendationRepository == repository.trimmed()) return;
    m_recommendationBody = body;
    m_recommendationRepository = repository.trimmed();
    refreshRecommendations();
}

void MissionController::resetSkillContext()
{
    m_recommendationBody = {};
    m_recommendationRepository.clear();
    refreshRecommendations();
}

void MissionController::refreshRecommendations()
{
    // Invalidate immediately, including the gap before the next debounced POST.
    ++m_skillContextGeneration;
    m_recommendationRequest = 0;
    m_recommendationTimer.stop();
    const bool hadRecommendations = !m_recommendations.isEmpty();
    m_recommendations = {};
    m_selectedSkillIds.clear();
    m_recommendationError.clear();
    if (!m_recommendationBody.value("description").toString().isEmpty())
        m_recommendationTimer.start();
    QPointer<MissionController> guard(this);
    if (hadRecommendations) emit skillRecommendationsChanged();
    if (guard) emit recommendationsChanged();
}

QStringList MissionController::selectableSkillStages() const { return MissionProtocol::selectableSkillStages(); }
int MissionController::maxSelectedSkills() const { return MissionProtocol::MaxSelectedSkills; }

void MissionController::setSkillSelected(const QString &skillId, bool selected)
{
    if (m_selectedSkillIds.contains(skillId) == selected) return;
    if (selected) {
        if (m_selectedSkillIds.size() >= maxSelectedSkills()) return;
        const bool offered = std::any_of(m_recommendations.begin(), m_recommendations.end(), [&](const QJsonValue &item) {
            return item.toObject().value("skill_id").toString() == skillId;
        });
        if (!offered) return;
        m_selectedSkillIds.append(skillId);
    } else m_selectedSkillIds.removeAll(skillId);
    emit recommendationsChanged();
}

void MissionController::loadMissionSkills()
{
    if (m_task.isEmpty() || missionSkillsLoading()) return;
    m_missionSkillsError.clear();
    m_missionSkillsRequest = m_api.getMissionSkills(m_task);
    emit missionSkillsChanged();
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
    m_loader.refresh(delay);
}
void MissionController::received(quint64 request, const QString &task, const QJsonObject &body)
{
    if (!m_requests.contains(request)) return; // Cancelled, switched or superseded.
    const auto current = continuation();
    const auto kind = m_requests.take(request);
    if (kind == RequestKind::Create) {
        const bool samePreview = submittedPreviewIsCurrent(m_creationSubmission);
        const bool sameDraft = m_creationSubmission.previewGeneration == m_previewGeneration
            && m_creationSubmission.skillGeneration == m_skillContextGeneration;
        if (samePreview) {
            m_creationPreview.remove("preview_token"); m_creationReady = false;
        }
        if (!sameDraft) {
            m_mutation = 0;
            if (samePreview) emit creationChanged();
            if (current()) refreshMissions();
            if (current()) updateAvailability();
            return;
        }
        m_creationError.clear();
        resetSkillContext();
        if (!current()) return;
        emit creationChanged();
        if (!current()) return;
        m_mutation = 0; m_task = body.value("task_id").toString();
        m_loader.selectTask(m_task);
        emit missionCreated();
        if (!current()) return;
        refreshMissions();
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
}
void MissionController::commitSnapshot(const MissionSnapshot &snapshot)
{
    const auto &mission = snapshot.mission;
    if (mission.value("task_id").toString() != m_task) return;
    const auto current = continuation();
    m_snapshot = mission;
    m_graph.setRecoveryRequired(mission.value("recovery_required").toBool());
    if (!current()) return;
    m_graph.setSnapshot(snapshot.graph);
    if (!current()) return;
    m_artifacts.setSnapshot(snapshot.artifacts);
    if (!current()) return;
    m_evidence.setSnapshot(snapshot.artifacts);
    if (!current()) return;
    m_diff.setSnapshot(snapshot.diff);
    if (!current()) return;
    m_validation.setSnapshot(snapshot.validation);
    if (!current()) return;
    observeMission(mission);
    if (!current()) return;
    m_approval.setSnapshot(snapshot);
    if (!current()) return;
    m_delivery.setMission(mission);
    if (!current()) return;
    if (m_recovery.value("revision").toInteger() != revision()) m_recovery = {};
    m_synchronized = true;
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
    // Discard the pre-mutation read bundle, even if its final GET arrives late.
    m_loader.invalidate(mission.value("current_revision").toInteger()); scheduleRefresh(0);
}
void MissionController::failed(quint64 request, int status, const QString &code)
{
    if (!m_requests.contains(request)) return;
    const auto current = continuation();
    const auto kind = m_requests.take(request);
    setError(MissionProtocol::errorMessage(code, status));
    if (!current()) return;
    if (kind == RequestKind::Artifact) { m_artifactRequest = 0; return; }
    if (kind == RequestKind::Mutation) {
        m_mutation = 0; m_synchronized = false; m_recovery = {}; m_delivery.invalidatePreview();
        if (!current()) return;
        m_loader.invalidate(); scheduleRefresh(0); return;
    }
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
