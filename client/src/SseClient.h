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
public:
    explicit SseClient(QObject *parent = nullptr);
    ~SseClient() override { detach(); }
    bool setBaseUrl(const QUrl &url);
    void connectToTask(const QString &taskId, const QString &lastEventId = {});
    void disconnect();
    void forgetTask();
    QString connectionState() const { return m_state; }
    QString lastEventId() const { return m_lastId; }
signals:
    void eventApplied(const QString &id, const QJsonObject &payload);
    void connectionError(const QString &message);
    void connectionStateChanged();
    void lastEventIdChanged();
    void streamEnded();
private:
    void open();
    void read(QNetworkReply *reply, quint64 generation);
    void accept(const SseFrame &frame);
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
    quint64 m_generation = 0;
    bool m_active = false;
    int m_retry = 1000, m_backoff = 1000;
};
