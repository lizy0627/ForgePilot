#pragma once
#include "SseParser.h"
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QPointer>
#include <QTimer>

class SseClient : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString connectionState READ connectionState NOTIFY connectionStateChanged)
    Q_PROPERTY(QString lastEventId READ lastEventId NOTIFY lastEventIdChanged)
    Q_PROPERTY(qint64 missionRevision READ missionRevision NOTIFY missionRevisionChanged)
public:
    explicit SseClient(QObject *parent = nullptr);
    ~SseClient() override { detach(); }
    bool setBaseUrl(const QUrl &url);
    void connectToTask(const QString &taskId, const QString &lastEventId = {});
    void connectToMission(const QString &taskId, const QString &lastEventId = {}, qint64 revision = 0);
    // Called only after a coherent mission snapshot has been committed to the models.
    // Advances an open stream in place, or resumes a stream waiting for recovery.
    bool adoptMissionSnapshot(const QString &taskId, const QString &lastEventId, qint64 revision);
    void disconnect();
    void forgetTask();
    QString connectionState() const { return m_state; }
    QString lastEventId() const { return m_lastId; }
    qint64 missionRevision() const { return m_revision; }
signals:
    void eventApplied(const QString &id, const QJsonObject &payload);
    void connectionError(const QString &message);
    void connectionStateChanged();
    void lastEventIdChanged();
    void missionRevisionChanged();
    void snapshotRequired();
    void streamEnded();
private:
    void open();
    void read(QNetworkReply *reply, quint64 generation);
    void accept(const SseFrame &frame);
    void acceptMission(const SseFrame &frame);
    void requireSnapshot(const QString &message, bool invalidCursor = false);
    void fail(const QString &error);
    void setState(const QString &state);
    void detach();
    QNetworkAccessManager m_network;
    QUrl m_base{"http://127.0.0.1:8765"};
    QPointer<QNetworkReply> m_reply;
    QTimer m_reconnect, m_idle;
    SseParser m_parser;
    QString m_taskId, m_lastId, m_state{"disconnected"};
    qint64 m_sequence = 0;
    qint64 m_revision = 0;
    quint64 m_generation = 0;
    bool m_active = false;
    bool m_mission = false, m_recovering = false, m_invalidCursor = false;
    QByteArray m_errorBody;
    int m_retry = 1000, m_backoff = 1000;
};
