#pragma once
#include "AgentApiClient.h"
#include "SseClient.h"
#include "ApprovalController.h"
#include "DeliveryController.h"
#include "MissionListModel.h"
#include "GraphModel.h"
#include "ArtifactModel.h"
#include "EvidenceModel.h"
#include "DiffModel.h"
#include "ValidationModel.h"
#include <QTimer>
#include <functional>

// Coordinates backend projections. It owns no execution, model, Git or file tools.
class MissionController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QUrl baseUrl READ baseUrl WRITE setBaseUrl NOTIFY baseUrlChanged)
    Q_PROPERTY(QString taskId READ taskId NOTIFY missionChanged)
    Q_PROPERTY(QString status READ status NOTIFY missionChanged)
    Q_PROPERTY(qint64 revision READ revision NOTIFY missionChanged)
    Q_PROPERTY(QVariantMap snapshot READ snapshot NOTIFY missionChanged)
    Q_PROPERTY(QVariantMap recovery READ recovery NOTIFY missionChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY availabilityChanged)
    Q_PROPERTY(bool synchronized READ synchronized NOTIFY availabilityChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)
    Q_PROPERTY(QString connectionState READ connectionState NOTIFY connectionStateChanged)
    Q_PROPERTY(MissionListModel* missions READ missions CONSTANT)
    Q_PROPERTY(GraphModel* graph READ graph CONSTANT)
    Q_PROPERTY(ArtifactModel* artifacts READ artifacts CONSTANT)
    Q_PROPERTY(EvidenceModel* evidence READ evidence CONSTANT)
    Q_PROPERTY(DiffModel* diff READ diff CONSTANT)
    Q_PROPERTY(ValidationModel* validation READ validation CONSTANT)
    Q_PROPERTY(ApprovalController* approval READ approval CONSTANT)
    Q_PROPERTY(DeliveryController* delivery READ delivery CONSTANT)
public:
    explicit MissionController(QObject *parent = nullptr);
    ~MissionController() override;
    QUrl baseUrl() const { return m_api.baseUrl(); }
    void setBaseUrl(const QUrl &url);
    QString taskId() const { return m_task; }
    QString status() const { return m_snapshot.value("status").toString(); }
    qint64 revision() const { return m_snapshot.value("current_revision").toInteger(); }
    QVariantMap snapshot() const { return m_snapshot.toVariantMap(); }
    QVariantMap recovery() const { return m_recovery.toVariantMap(); }
    bool busy() const { return m_mutation || m_approval.busy() || m_delivery.busy(); }
    bool synchronized() const { return m_synchronized; }
    QString error() const { return m_error; }
    QString connectionState() const { return m_sse.connectionState(); }
    MissionListModel *missions() { return &m_missions; }
    GraphModel *graph() { return &m_graph; }
    ArtifactModel *artifacts() { return &m_artifacts; }
    EvidenceModel *evidence() { return &m_evidence; }
    DiffModel *diff() { return &m_diff; }
    ValidationModel *validation() { return &m_validation; }
    ApprovalController *approval() { return &m_approval; }
    DeliveryController *delivery() { return &m_delivery; }
    Q_INVOKABLE void createMission(const QVariantMap &request);
    Q_INVOKABLE void selectMission(const QString &taskId);
    Q_INVOKABLE void clearSelection();
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void cancel();
    Q_INVOKABLE void retry(const QString &nodeId = {});
    Q_INVOKABLE void prepareRecovery();
    Q_INVOKABLE void confirmRecovery();
    Q_INVOKABLE void loadArtifact(const QString &artifactId);
    Q_INVOKABLE QString localPath(const QUrl &folder) const { return folder.toLocalFile(); }
signals:
    void baseUrlChanged();
    void missionChanged();
    void availabilityChanged();
    void errorChanged();
    void connectionStateChanged();
    void artifactLoaded(const QVariantMap &artifact);
    void eventReceived(const QString &eventId, const QJsonObject &event);
private:
    enum class RequestKind { Start, Graph, Artifacts, Diff, Validation, Fence, Mutation, Create, Artifact };
    void track(quint64 request, RequestKind kind);
    void received(quint64 request, const QString &task, const QJsonObject &body);
    void failed(quint64 request, int status, const QString &code);
    void scheduleRefresh(int delay = 40);
    void beginRefresh();
    void abortRefresh();
    void commitSnapshot(const QJsonObject &mission);
    void mutationSnapshot(const QJsonObject &mission);
    void updateAvailability();
    bool canControl() const;
    void setError(const QString &message, bool streamError = false);
    // A synchronous signal may delete the controller or select another mission.
    std::function<bool()> continuation();
    AgentApiClient m_api;
    SseClient m_sse;
    MissionListModel m_missions;
    GraphModel m_graph;
    ArtifactModel m_artifacts;
    EvidenceModel m_evidence;
    DiffModel m_diff;
    ValidationModel m_validation;
    ApprovalController m_approval;
    DeliveryController m_delivery;
    QTimer m_refreshTimer;
    QHash<quint64, RequestKind> m_requests;
    QJsonObject m_snapshot, m_start, m_graphSnapshot, m_artifactSnapshot, m_diffSnapshot, m_validationSnapshot, m_recovery;
    QString m_task, m_error;
    qint64 m_floor = 0;
    quint64 m_mutation = 0, m_artifactRequest = 0;
    quint64 m_generation = 0;
    int m_parts = 0, m_refreshAttempts = 0;
    bool m_refreshing = false, m_synchronized = false, m_updatingAvailability = false, m_streamSelected = false;
    bool m_streamError = false;
};
