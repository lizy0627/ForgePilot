#pragma once
#include "AgentApiClient.h"
#include <QPointer>
#include <QTimer>

// A bundle is published only after all projections pass the final Mission fence.
struct MissionSnapshot {
    QJsonObject mission, graph, artifacts, diff, validation;
};
Q_DECLARE_METATYPE(MissionSnapshot)

// Read-only synchronization. The injected API owns transport/validation; this
// component owns only its GETs, retry timer and the selected task's revision floor.
// It has no QML models, SSE connection or authority to submit a mutation.
class MissionSnapshotLoader : public QObject {
    Q_OBJECT
public:
    explicit MissionSnapshotLoader(AgentApiClient *api, QObject *parent = nullptr);
    ~MissionSnapshotLoader() override;
    void selectTask(const QString &task);
    void refresh(int delay = 0);
    void observeRevision(qint64 revision);
    // Discard even a final GET already in flight. The caller requests a new read.
    void invalidate(qint64 minimumRevision = 0);
signals:
    void snapshotReady(const MissionSnapshot &snapshot);
    void loadFailed(int status, const QString &code);
private:
    enum class Part { Start, Graph, Artifacts, Fence };
    struct Pending { Part part; quint64 generation; };
    void begin();
    void track(quint64 request, Part part);
    void received(quint64 request, const QString &task, const QJsonObject &body);
    void failed(quint64 request, int status, const QString &code);
    QPointer<AgentApiClient> m_api;
    QTimer m_timer;
    QHash<quint64, Pending> m_requests;
    QString m_task;
    QJsonObject m_start;
    MissionSnapshot m_bundle;
    qint64 m_floor = 0;
    quint64 m_generation = 0;
    int m_attempts = 0;
    bool m_loading = false;
};
