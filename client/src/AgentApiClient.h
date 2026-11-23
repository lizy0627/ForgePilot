#pragma once
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QSet>
#include <QHash>

class AgentApiClient : public QObject {
    Q_OBJECT
public:
    explicit AgentApiClient(QObject *parent = nullptr);
    ~AgentApiClient() override { abortPending(); }
    QUrl baseUrl() const { return m_base; }
    bool setBaseUrl(const QUrl &url);
    quint64 createTask(const QString &repositoryPath, const QString &prompt);
    quint64 approveTask(const QString &taskId, int planVersion);
    quint64 feedbackTask(const QString &taskId, int planVersion, const QString &feedback);
    quint64 cancelTask(const QString &taskId, bool reject = false, int planVersion = 0);
    quint64 getTask(const QString &taskId);
    quint64 listTasks(const QString &before = {}, const QString &conversationId = {});
    quint64 getTrace(const QString &taskId, qint64 after = 0);
    quint64 getTraceDetail(const QString &taskId, qint64 sequence);
    quint64 createMission(const QJsonObject &body);
    quint64 recommendSkills(const QJsonObject &body);
    quint64 previewMission(const QJsonObject &body);
    quint64 discardMissionPreview(const QString &token);
    quint64 listMissions(const QString &before = {}, const QString &repositoryPath = {}, const QString &status = {});
    quint64 getMission(const QString &taskId);
    quint64 getMissionGraph(const QString &taskId);
    quint64 getMissionSkills(const QString &taskId);
    quint64 getMissionScopeExpansion(const QString &taskId);
    quint64 getMissionArtifacts(const QString &taskId);
    quint64 getMissionArtifact(const QString &taskId, const QString &artifactId);
    quint64 getMissionDiff(const QString &taskId);
    quint64 getMissionValidation(const QString &taskId);
    quint64 approveMission(const QString &taskId, const QJsonObject &body);
    quint64 cancelMission(const QString &taskId, qint64 revision);
    quint64 retryMission(const QString &taskId, qint64 revision, const QString &nodeId = {});
    quint64 resumeMission(const QString &taskId, qint64 revision, const QString &token = {});
    quint64 deliverMission(const QString &taskId, const QJsonObject &body);
    void abortRequest(quint64 requestId); // Silent local detach, never a server-side cancel.
    void abortPending(); // Detach requests; never issues a task cancellation.
signals:
    void responseReceived(quint64 requestId, const QString &operation, const QString &taskId, const QJsonObject &body);
    void requestError(quint64 requestId, const QString &operation, const QString &taskId,
                      int status, const QString &code, const QString &message);
private:
    quint64 send(const QString &operation, const QString &taskId = {}, const QJsonObject &body = {});
    quint64 sendMission(const QString &operation, const QString &taskId = {}, const QJsonObject &body = {});
    quint64 rejectRequest(const QString &operation, const QString &taskId, const QString &code);
    QNetworkAccessManager m_network;
    QUrl m_base{"http://127.0.0.1:8765"};
    QSet<QNetworkReply *> m_pending;
    QHash<quint64, QNetworkReply *> m_requests;
    QSet<quint64> m_rejections;
    quint64 m_next = 0;
};
