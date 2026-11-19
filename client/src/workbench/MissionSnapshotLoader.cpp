#include "MissionSnapshotLoader.h"
#include <QJsonArray>
#include <algorithm>

namespace {
bool hasPausedWriter(const QJsonObject &graph)
{
    if (graph.value("status") != "awaiting_human") return false;
    for (const auto &entry : graph.value("nodes").toArray()) {
        const auto node = entry.toObject();
        const auto id = node.value("node_id").toString();
        if (node.value("status") == "awaiting_human" && (id == "implementer" || id.startsWith("repair_")))
            return true;
    }
    return false;
}

void applyArtifacts(MissionSnapshot &snapshot, const QJsonObject &artifacts)
{
    snapshot.artifacts = artifacts;
    QJsonArray changes, reports;
    for (const auto &item : artifacts.value("items").toArray()) {
        const auto type = item.toObject().value("artifact_type");
        if (type == "ChangeSet") changes.append(item);
        else if (type == "ValidationReport") reports.append(item);
    }

    // Match the Mission diff endpoint: the last ChangeSet holds the cumulative patch.
    QString patch;
    if (!changes.isEmpty()) {
        const auto content = changes.last().toObject().value("content").toObject();
        for (const auto &file : content.value("files").toArray())
            patch += file.toObject().value("unified_diff").toString();
    }
    const auto task = artifacts.value("task_id");
    snapshot.diff = {{"task_id", task}, {"patch", patch}, {"change_sets", changes}};
    snapshot.validation = {{"task_id", task}, {"items", reports},
        {"latest", reports.isEmpty() ? QJsonValue::Null : reports.last()}};
}
}

MissionSnapshotLoader::MissionSnapshotLoader(AgentApiClient *api, QObject *parent)
    : QObject(parent), m_api(api)
{
    Q_ASSERT(api);
    m_timer.setSingleShot(true);
    connect(&m_timer, &QTimer::timeout, this, &MissionSnapshotLoader::begin);
    connect(api, &AgentApiClient::responseReceived, this,
        [this](quint64 id, const QString &, const QString &task, const QJsonObject &body) { received(id, task, body); });
    connect(api, &AgentApiClient::requestError, this,
        [this](quint64 id, const QString &, const QString &, int status, const QString &code, const QString &) { failed(id, status, code); });
}
MissionSnapshotLoader::~MissionSnapshotLoader() { invalidate(); }

void MissionSnapshotLoader::selectTask(const QString &task)
{
    invalidate();
    m_task = task; m_floor = 0; m_attempts = 0;
}
void MissionSnapshotLoader::observeRevision(qint64 revision)
{ m_floor = std::max(m_floor, revision); }

void MissionSnapshotLoader::invalidate(qint64 minimumRevision)
{
    ++m_generation;
    m_timer.stop();
    // Detach our reads only; approval/delivery and other API consumers own theirs.
    const auto requests = m_requests.keys();
    m_requests.clear();
    if (m_api) for (const auto request : requests) m_api->abortRequest(request);
    m_loading = false; m_start = {}; m_bundle = {};
    observeRevision(minimumRevision);
}
void MissionSnapshotLoader::refresh(int delay)
{
    if (!m_api || m_task.isEmpty() || m_loading || m_timer.isActive()) return;
    m_timer.start(delay);
}
void MissionSnapshotLoader::track(quint64 request, Part part)
{ m_requests.insert(request, {part, m_generation}); }

void MissionSnapshotLoader::begin()
{
    if (!m_api || m_task.isEmpty() || m_loading) return;
    m_loading = true; m_start = {}; m_bundle = {};
    track(m_api->getMission(m_task), Part::Start);
}
void MissionSnapshotLoader::received(quint64 request, const QString &task, const QJsonObject &body)
{
    auto it = m_requests.find(request);
    if (it == m_requests.end()) return;
    const auto pending = it.value(); m_requests.erase(it);
    if (pending.generation != m_generation || task != m_task) return;
    const auto part = pending.part;
    if (part == Part::Start) {
        m_start = body;
        track(m_api->getMissionGraph(m_task), Part::Graph);
        track(m_api->getMissionArtifacts(m_task), Part::Artifacts);
        return;
    }
    if (part == Part::Fence) {
        const auto revision = body.value("current_revision").toInteger();
        if (revision < m_floor || revision != m_start.value("current_revision").toInteger()
            || revision != m_bundle.graph.value("current_revision").toInteger()
            || (!m_bundle.scopeExpansion.isEmpty() && revision != m_bundle.scopeExpansion.value("revision").toInteger())
            || body.value("last_event_id") != m_start.value("last_event_id")) {
            invalidate();
            refresh(std::min(100 * ++m_attempts, 2000));
            return;
        }
        m_bundle.mission = body; m_floor = revision;
        m_loading = false; m_attempts = 0;
        // A synchronous receiver can delete us or switch tasks. Publish a value
        // independent of our storage, and never continue after the signal.
        const auto snapshot = m_bundle;
        emit snapshotReady(snapshot);
        return;
    }
    if (part == Part::Graph) {
        m_bundle.graph = body;
        if (hasPausedWriter(body)) track(m_api->getMissionScopeExpansion(m_task), Part::ScopeExpansion);
    }
    if (part == Part::ScopeExpansion) m_bundle.scopeExpansion = body;
    if (part == Part::Artifacts) applyArtifacts(m_bundle, body);
    if (m_requests.isEmpty()) track(m_api->getMission(m_task), Part::Fence);
}
void MissionSnapshotLoader::failed(quint64 request, int status, const QString &code)
{
    const auto it = m_requests.constFind(request);
    if (it == m_requests.cend() || it->generation != m_generation) return;
    if (it->part == Part::ScopeExpansion && status == 404 && code == "scope_expansion_not_found") {
        // Older writers could pause with only a decision summary.
        received(request, m_task, {});
        return;
    }
    invalidate();
    if (status == 0 || status == 429 || status >= 500) refresh(std::min(250 * ++m_attempts, 5000));
    emit loadFailed(status, code);
}
