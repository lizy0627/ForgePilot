#include "MissionSnapshotLoader.h"
#include <algorithm>

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
    m_loading = false; m_parts = 0; m_start = {}; m_bundle = {};
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
    m_loading = true; m_parts = 0; m_start = {}; m_bundle = {};
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
        track(m_api->getMissionDiff(m_task), Part::Diff);
        track(m_api->getMissionValidation(m_task), Part::Validation);
        return;
    }
    if (part == Part::Fence) {
        const auto revision = body.value("current_revision").toInteger();
        if (revision < m_floor || revision != m_start.value("current_revision").toInteger()
            || revision != m_bundle.graph.value("current_revision").toInteger()
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
    if (part == Part::Graph) m_bundle.graph = body;
    if (part == Part::Artifacts) m_bundle.artifacts = body;
    if (part == Part::Diff) m_bundle.diff = body;
    if (part == Part::Validation) m_bundle.validation = body;
    if (++m_parts == 4) track(m_api->getMission(m_task), Part::Fence);
}
void MissionSnapshotLoader::failed(quint64 request, int status, const QString &code)
{
    const auto it = m_requests.constFind(request);
    if (it == m_requests.cend() || it->generation != m_generation) return;
    invalidate();
    if (status == 0 || status == 429 || status >= 500) refresh(std::min(250 * ++m_attempts, 5000));
    emit loadFailed(status, code);
}
