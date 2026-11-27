#pragma once
#include "AgentApiClient.h"
#include "SseClient.h"
#include "ApprovalController.h"
#include "DeliveryController.h"
#include "MissionSnapshotLoader.h"
#include "MissionListModel.h"
#include "SkillDraftClient.h"
#include "GraphModel.h"
#include "ArtifactModel.h"
#include "EvidenceModel.h"
#include "DiffModel.h"
#include "ValidationModel.h"
#include <QJsonArray>
#include <QTimer>
#include <functional>

// Presents Mission state and operations to QML; read coordination is delegated.
class MissionController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QUrl baseUrl READ baseUrl WRITE setBaseUrl NOTIFY baseUrlChanged)
    Q_PROPERTY(QString taskId READ taskId NOTIFY missionChanged)
    Q_PROPERTY(QString status READ status NOTIFY missionChanged)
    Q_PROPERTY(qint64 revision READ revision NOTIFY missionChanged)
    Q_PROPERTY(QVariantMap snapshot READ snapshot NOTIFY missionChanged)
    Q_PROPERTY(QVariantMap recovery READ recovery NOTIFY missionChanged)
    Q_PROPERTY(QVariantMap creationPreview READ creationPreview NOTIFY creationChanged)
    Q_PROPERTY(bool previewLoading READ previewLoading NOTIFY creationChanged)
    Q_PROPERTY(bool creationReady READ creationReady NOTIFY creationChanged)
    Q_PROPERTY(QString creationError READ creationError NOTIFY creationChanged)
    Q_PROPERTY(QVariantList skillRecommendations READ skillRecommendations NOTIFY skillRecommendationsChanged)
    Q_PROPERTY(QStringList selectedSkillIds READ selectedSkillIds NOTIFY recommendationsChanged)
    Q_PROPERTY(QStringList selectableSkillStages READ selectableSkillStages CONSTANT)
    Q_PROPERTY(int maxSelectedSkills READ maxSelectedSkills CONSTANT)
    Q_PROPERTY(QString recommendationError READ recommendationError NOTIFY recommendationsChanged)
    Q_PROPERTY(bool recommendationsLoading READ recommendationsLoading NOTIFY recommendationsChanged)
    Q_PROPERTY(QVariantMap missionSkills READ missionSkills NOTIFY missionSkillsChanged)
    Q_PROPERTY(bool missionSkillsLoading READ missionSkillsLoading NOTIFY missionSkillsChanged)
    Q_PROPERTY(QString missionSkillsError READ missionSkillsError NOTIFY missionSkillsChanged)
    Q_PROPERTY(SkillDraftClient* skillDrafts READ skillDrafts CONSTANT)
    Q_PROPERTY(bool busy READ busy NOTIFY availabilityChanged)
    Q_PROPERTY(bool synchronized READ synchronized NOTIFY availabilityChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)
    Q_PROPERTY(QString connectionState READ connectionState NOTIFY connectionStateChanged)
    Q_PROPERTY(MissionListModel* missions READ missions CONSTANT)
    Q_PROPERTY(bool historyLoading READ historyLoading NOTIFY historyChanged)
    Q_PROPERTY(bool historyLoaded READ historyLoaded NOTIFY historyChanged)
    Q_PROPERTY(bool historyHasMore READ historyHasMore NOTIFY historyChanged)
    Q_PROPERTY(int historyTotal READ historyTotal NOTIFY historyChanged)
    Q_PROPERTY(QString historyError READ historyError NOTIFY historyChanged)
    Q_PROPERTY(QString repositoryFilter READ repositoryFilter WRITE setRepositoryFilter NOTIFY historyChanged)
    Q_PROPERTY(QString statusFilter READ statusFilter WRITE setStatusFilter NOTIFY historyChanged)
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
    QVariantMap creationPreview() const { return m_creationPreview.toVariantMap(); }
    bool previewLoading() const { return m_previewRequest != 0; }
    bool creationReady() const { return m_creationReady; }
    QString creationError() const { return m_creationError; }
    QVariantList skillRecommendations() const { return m_recommendations.toVariantList(); }
    QStringList selectedSkillIds() const { return m_selectedSkillIds; }
    QStringList selectableSkillStages() const;
    int maxSelectedSkills() const;
    QString recommendationError() const { return m_recommendationError; }
    bool recommendationsLoading() const { return m_recommendationTimer.isActive() || m_recommendationRequest != 0; }
    QVariantMap missionSkills() const { return m_missionSkills.toVariantMap(); }
    bool missionSkillsLoading() const { return m_missionSkillsRequest != 0; }
    QString missionSkillsError() const { return m_missionSkillsError; }
    SkillDraftClient *skillDrafts() { return &m_skillDrafts; }
    bool busy() const { return m_mutation || m_approval.busy() || m_delivery.busy(); }
    bool synchronized() const { return m_synchronized; }
    QString error() const { return m_error; }
    QString connectionState() const { return m_sse.connectionState(); }
    MissionListModel *missions() { return &m_missions; }
    bool historyLoading() const { return m_historyRequest != 0; }
    bool historyLoaded() const { return m_historyLoaded; }
    bool historyHasMore() const { return !m_historyCursor.isEmpty(); }
    int historyTotal() const { return m_historyTotal; }
    QString historyError() const { return m_historyError; }
    QString repositoryFilter() const { return m_repositoryFilter; }
    QString statusFilter() const { return m_statusFilter; }
    void setRepositoryFilter(const QString &path);
    void setStatusFilter(const QString &status);
    GraphModel *graph() { return &m_graph; }
    ArtifactModel *artifacts() { return &m_artifacts; }
    EvidenceModel *evidence() { return &m_evidence; }
    DiffModel *diff() { return &m_diff; }
    ValidationModel *validation() { return &m_validation; }
    ApprovalController *approval() { return &m_approval; }
    DeliveryController *delivery() { return &m_delivery; }
    Q_INVOKABLE void createMission(const QVariantMap &request);
    Q_INVOKABLE void updateSkillContext(const QString &description, const QString &taskType,
                                        const QString &repository, const QStringList &projectTags);
    Q_INVOKABLE void resetSkillContext();
    Q_INVOKABLE void setSkillSelected(const QString &skillId, bool selected);
    Q_INVOKABLE void loadMissionSkills();
    Q_INVOKABLE void previewCreation(const QVariantMap &request);
    Q_INVOKABLE void invalidateCreationPreview(bool forget = false);
    Q_INVOKABLE void refreshMissions();
    Q_INVOKABLE void loadMoreMissions();
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
    void historyChanged();
    void creationChanged();
    void recommendationsChanged();
    void skillRecommendationsChanged();
    void missionSkillsChanged();
    void missionCreated();
    void missionChanged();
    void availabilityChanged();
    void errorChanged();
    void connectionStateChanged();
    void artifactLoaded(const QVariantMap &artifact);
    void eventReceived(const QString &eventId, const QJsonObject &event);
private:
    enum class RequestKind { Mutation, Create, Artifact };
    struct CreationSubmission {
        QString token;
        quint64 previewGeneration;
        quint64 skillGeneration;
        bool withSkills;
    };
    bool submittedPreviewIsCurrent(const CreationSubmission &submission) const;
    void releaseCreationPreview();
    void creationFailed(quint64 request, int status, const QString &code, const QString &message);
    void track(quint64 request, RequestKind kind);
    void received(quint64 request, const QString &task, const QJsonObject &body);
    void failed(quint64 request, int status, const QString &code);
    void scheduleRefresh(int delay = 40);
    void commitSnapshot(const MissionSnapshot &snapshot);
    void mutationSnapshot(const QJsonObject &mission);
    void updateAvailability();
    bool canControl() const;
    void setError(const QString &message, bool streamError = false);
    void requestHistory(bool append);
    void observeMission(const QJsonObject &mission);
    void refreshRecommendations();
    // A synchronous signal may delete the controller or select another mission.
    std::function<bool()> continuation();
    AgentApiClient m_api;
    AgentApiClient m_historyApi;
    AgentApiClient m_creationApi;
    SkillDraftClient m_skillDrafts;
    MissionSnapshotLoader m_loader;
    SseClient m_sse;
    MissionListModel m_missions;
    GraphModel m_graph;
    ArtifactModel m_artifacts;
    EvidenceModel m_evidence;
    DiffModel m_diff;
    ValidationModel m_validation;
    ApprovalController m_approval;
    DeliveryController m_delivery;
    QHash<quint64, RequestKind> m_requests;
    QJsonObject m_snapshot, m_recovery, m_creationPreview;
    QString m_creationError;
    quint64 m_previewRequest = 0;
    quint64 m_previewGeneration = 0;
    CreationSubmission m_creationSubmission{};
    bool m_creationReady = false;
    QTimer m_recommendationTimer;
    QString m_recommendationRepository, m_recommendationError;
    QJsonObject m_recommendationBody;
    quint64 m_recommendationRequest = 0;
    quint64 m_skillContextGeneration = 0;
    QJsonArray m_recommendations;
    QStringList m_selectedSkillIds;
    quint64 m_missionSkillsRequest = 0;
    QJsonObject m_missionSkills;
    QString m_missionSkillsError;
    QString m_task, m_error;
    quint64 m_mutation = 0, m_artifactRequest = 0;
    quint64 m_generation = 0;
    quint64 m_historyRequest = 0, m_historyGeneration = 0;
    QString m_historyCursor, m_historyError, m_repositoryFilter, m_statusFilter;
    int m_historyTotal = 0;
    bool m_historyAppend = false, m_historyLoaded = false;
    bool m_synchronized = false, m_updatingAvailability = false, m_streamSelected = false;
    bool m_streamError = false;
};
