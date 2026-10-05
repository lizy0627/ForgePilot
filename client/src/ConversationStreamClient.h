#pragma once
#include "SseParser.h"
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QPointer>
#include <QTimer>

// Conversation answer SSE has request revisions and terminal semantics that are
// intentionally independent from Task SseClient.
class ConversationStreamClient : public QObject {
    Q_OBJECT
public:
    explicit ConversationStreamClient(QObject *parent=nullptr);
    ~ConversationStreamClient() override { disconnectStream(); }
    bool setBaseUrl(const QUrl &url);
    quint64 connectToRequest(const QString &conversationId,const QString &requestId,
                             const QString &lastEventId={});
    void disconnectStream();
    void requestRecovery();
    QString connectionState() const { return m_state; }
    QUrl baseUrl() const { return m_base; }
    QString lastEventId() const { return m_lastId; }
    QString conversationId() const { return m_conversationId; }
    QString requestId() const { return m_requestId; }
    qint64 revision() const { return m_revision; }
    int reconnectCount() const { return m_reconnectCount; }
    quint64 sessionGeneration() const { return m_sessionGeneration; }
signals:
    void eventReceived(const QString &type,const QJsonObject &payload,quint64 sessionGeneration);
    void connectionStateChanged(const QString &state,quint64 sessionGeneration);
    void transientError(const QString &errorCode,quint64 sessionGeneration);
    void unsupported(const QString &errorCode,quint64 sessionGeneration);
    void fatalError(const QString &errorCode,quint64 sessionGeneration);
    void streamEnded(quint64 sessionGeneration);
private:
    void open();
    void read(QNetworkReply *reply,quint64 connectionGeneration);
    void accept(const SseFrame &frame);
    void reconnect(const QString &errorCode);
    void fail(const QString &errorCode);
    void markUnsupported();
    void setState(const QString &state);
    void detach();
    QNetworkAccessManager m_network;
    QUrl m_base{"http://127.0.0.1:8765"};
    QPointer<QNetworkReply> m_reply;
    QTimer m_reconnect,m_idle;
    SseParser m_parser{128*1024};
    QString m_conversationId,m_requestId,m_lastId,m_state{"disconnected"};
    qint64 m_revision=0,m_responseBytes=0;
    quint64 m_connectionGeneration=0,m_sessionGeneration=0;
    bool m_active=false;
    int m_retry=1000,m_backoff=1000,m_reconnectCount=0;
};
