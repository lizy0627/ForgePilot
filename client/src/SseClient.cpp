#include "SseClient.h"
#include "Protocol.h"
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <algorithm>

SseClient::SseClient(QObject *parent) : QObject(parent)
{
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_reconnect.setSingleShot(true);
    m_idle.setSingleShot(true);
    connect(&m_reconnect, &QTimer::timeout, this, &SseClient::open);
    connect(&m_idle, &QTimer::timeout, this, [this] { if (m_reply) m_reply->abort(); });
}
bool SseClient::setBaseUrl(const QUrl &url)
{
    if (!Protocol::validBaseUrl(url)) return false;
    disconnect(); m_base = url; return true;
}
void SseClient::setState(const QString &state)
{
    if (m_state == state) return;
    m_state = state; emit connectionStateChanged();
}
void SseClient::detach()
{
    ++m_generation;
    m_reconnect.stop(); m_idle.stop();
    if (m_reply) {
        auto *reply = m_reply.data();
        m_reply = nullptr;
        QObject::disconnect(reply, nullptr, this, nullptr);
        reply->abort(); reply->deleteLater();
    }
    m_parser.reset();
}
void SseClient::disconnect()
{
    m_active = false; detach(); setState("disconnected");
}
void SseClient::forgetTask()
{
    disconnect(); m_taskId.clear(); m_lastId.clear(); m_sequence=0; emit lastEventIdChanged();
}
void SseClient::connectToTask(const QString &taskId, const QString &lastEventId)
{
    disconnect();
    if (!Protocol::validTaskId(taskId) || Protocol::sequence(lastEventId, taskId) < 0) {
        fail("任务或事件标识无效。"); return;
    }
    m_taskId = taskId; m_lastId = lastEventId;
    m_sequence = Protocol::sequence(lastEventId, taskId);
    emit lastEventIdChanged();
    m_retry = m_backoff = 1000;
    m_active = true; open();
}
void SseClient::fail(const QString &message)
{
    m_active = false; detach(); setState("error"); emit connectionError(message);
}
void SseClient::open()
{
    if (!m_active) return;
    m_parser.reset();
    setState(m_lastId.isEmpty() ? "connecting" : "reconnecting");
    QNetworkRequest request(Protocol::endpoint(m_base, '/' + m_taskId + "/events"));
    request.setRawHeader("Accept", "text/event-stream");
    if (!m_lastId.isEmpty()) request.setRawHeader("Last-Event-ID", m_lastId.toUtf8());
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    auto *reply = m_network.get(request);
    m_reply = reply;
    reply->setReadBufferSize(128 * 1024);
    const auto generation = ++m_generation;
    m_idle.start(45000);
    connect(reply, &QIODevice::readyRead, this, [=, this] { read(reply, generation); });
    connect(reply, &QNetworkReply::finished, this, [=, this] {
        if (generation != m_generation) return;
        read(reply, generation);
        if (generation != m_generation) return;
        m_idle.stop();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        m_reply = nullptr; reply->deleteLater();
        if (status == 204) {
            m_active = false; setState("ended"); emit streamEnded(); return;
        }
        if (status >= 300 && status < 500) {
            fail(QString("事件连接返回 HTTP %1；请刷新任务，已保留事件游标。").arg(status)); return;
        }
        if (!m_active) return;
        setState("reconnecting");
        emit connectionError("事件连接已断开，正在从上次接收的位置重连。");
        m_reconnect.start(m_backoff);
        m_backoff = std::min(m_backoff * 2, 30000);
    });
}
void SseClient::read(QNetworkReply *reply, quint64 generation)
{
    if (generation != m_generation || !m_active) return;
    if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() != 200) return;
    if (!reply->header(QNetworkRequest::ContentTypeHeader).toString().startsWith("text/event-stream", Qt::CaseInsensitive)) {
        fail("事件响应类型应为 text/event-stream。"); return;
    }
    setState("connected");
    while (reply->bytesAvailable() && generation == m_generation) {
        const auto frames = m_parser.feed(reply->read(64 * 1024));
        m_idle.start(45000);
        if (!m_parser.error().isEmpty()) { fail(m_parser.error()); return; }
        for (const auto &frame : frames) {
            if (generation != m_generation) return;
            if (frame.retry >= 0) { m_retry = m_backoff = frame.retry; continue; }
            accept(frame);
        }
    }
}
void SseClient::accept(const SseFrame &frame)
{
    const auto sequence = Protocol::sequence(frame.id, m_taskId);
    if (sequence <= 0) { fail("事件缺少有效的任务内标识。"); return; }
    if (sequence <= m_sequence) return; // Replay/duplicate: no state, rows or signals applied twice.
    if (sequence != m_sequence + 1) { fail("事件序列不连续，已保留游标。"); return; }
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(frame.data, &error);
    const auto payload = document.object();
    if (error.error != QJsonParseError::NoError || !document.isObject()
        || !Protocol::validEvent(payload, frame.event, m_taskId)) {
        fail("事件 JSON 内容或任务标识无效。"); return;
    }
    m_lastId = frame.id; m_sequence = sequence; m_backoff = m_retry;
    emit lastEventIdChanged();
    const auto generation = m_generation;
    emit eventApplied(frame.id, payload);
    if (generation != m_generation) return;
    const auto state = payload.value("data").toObject().value("state").toString();
    // completed/failed state is followed by a summary; consume that final event too.
    if (frame.event == "task_completed" || frame.event == "task_failed"
        || (frame.event == "task_state_changed" && state == "cancelled")) {
        m_active = false; detach(); setState("ended"); emit streamEnded();
    }
}
