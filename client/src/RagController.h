#pragma once

#include "SseParser.h"
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QPointer>
#include <QTimer>

class RagController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QUrl baseUrl READ baseUrl WRITE setBaseUrl NOTIFY changed)
    Q_PROPERTY(QString repositoryPath READ repositoryPath WRITE setRepositoryPath NOTIFY changed)
    Q_PROPERTY(bool useForTasks READ useForTasks WRITE setUseForTasks NOTIFY useForTasksChanged)
    Q_PROPERTY(bool enabled READ enabled NOTIFY changed)
    Q_PROPERTY(bool available READ available NOTIFY changed)
    Q_PROPERTY(bool reachable READ reachable NOTIFY changed)
    Q_PROPERTY(QString status READ status NOTIFY changed)
    Q_PROPERTY(QString statusErrorCode READ statusErrorCode NOTIFY changed)
    Q_PROPERTY(int fileCount READ fileCount NOTIFY changed)
    Q_PROPERTY(int chunkCount READ chunkCount NOTIFY changed)
    Q_PROPERTY(int dirtyCount READ dirtyCount NOTIFY changed)
    Q_PROPERTY(int failedCount READ failedCount NOTIFY changed)
    Q_PROPERTY(int totalFiles READ totalFiles NOTIFY changed)
    Q_PROPERTY(int processed READ processed NOTIFY changed)
    Q_PROPERTY(QString lastIndexedAt READ lastIndexedAt NOTIFY changed)
    Q_PROPERTY(bool running READ running NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(double progress READ progress NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)
public:
    explicit RagController(QObject *parent = nullptr);
    ~RagController() override;
    QUrl baseUrl() const { return m_base; }
    QString repositoryPath() const { return m_repository; }
    bool useForTasks() const { return m_useForTasks; }
    bool enabled() const { return m_enabled; }
    bool available() const { return m_available; }
    bool reachable() const { return m_reachable; }
    QString status() const { return m_status; }
    QString statusErrorCode() const { return m_statusErrorCode; }
    int fileCount() const { return m_fileCount; }
    int chunkCount() const { return m_chunks; }
    int dirtyCount() const { return m_dirty; }
    int failedCount() const { return m_failedFiles; }
    int totalFiles() const { return m_total; }
    int processed() const { return m_processed; }
    QString lastIndexedAt() const { return m_lastIndexed; }
    bool running() const { return m_running; }
    bool busy() const { return !m_request.isNull(); }
    double progress() const { return m_total > 0 ? double(m_processed) / m_total : 0.0; }
    QString error() const { return m_error; }
    void setBaseUrl(const QUrl &url);
    void setRepositoryPath(const QString &path);
    void setUseForTasks(bool enabled);
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void buildIndex();
    Q_INVOKABLE void cancelIndex();
    Q_INVOKABLE void deleteIndex();
    Q_INVOKABLE void clearError();
signals:
    void changed();
    void errorChanged();
    void useForTasksChanged();
private:
    void send(const QByteArray &method, const QString &path, const QJsonObject &body = {});
    bool applyStatus(const QJsonObject &body);
    void openEvents();
    void readEvents(QNetworkReply *reply, quint64 generation);
    void stopEvents();
    void startPolling();
    void scheduleRecovery();
    void reset();
    void setError(const QString &message);
    QNetworkAccessManager m_network;
    QUrl m_base{"http://127.0.0.1:8765"};
    QString m_repository, m_repositoryId, m_operationId, m_status{"disabled"};
    QString m_lastIndexed, m_error, m_statusErrorCode;
    bool m_useForTasks = true;
    bool m_enabled = false, m_available = false, m_reachable = false, m_running = false;
    int m_fileCount = 0, m_chunks = 0, m_dirty = 0, m_failedFiles = 0;
    int m_total = 0, m_processed = 0;
    qint64 m_revision = 0;
    quint64 m_generation = 0;
    QPointer<QNetworkReply> m_request, m_stream;
    SseParser m_parser;
    QTimer m_recover, m_poll;
};
