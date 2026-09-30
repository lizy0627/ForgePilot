#pragma once
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QSet>

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
    void abortPending(); // Detach requests; never issues a task cancellation.
signals:
    void responseReceived(quint64 requestId, const QString &operation, const QString &taskId, const QJsonObject &body);
    void requestError(quint64 requestId, const QString &operation, const QString &taskId,
                      int status, const QString &code, const QString &message);
private:
    quint64 send(const QString &operation, const QString &taskId = {}, const QJsonObject &body = {});
    QNetworkAccessManager m_network;
    QUrl m_base{"http://127.0.0.1:8765"};
    QSet<QNetworkReply *> m_pending;
    quint64 m_next = 0;
};
