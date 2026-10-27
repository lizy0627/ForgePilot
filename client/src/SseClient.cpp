#include "SseClient.h"
#include "Protocol.h"
#include "JsonHttp.h"
#include "workbench/MissionProtocol.h"
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <algorithm>
#include <limits>

SseClient::SseClient(QObject *parent) : QObject(parent)
{
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_reconnect.setSingleShot(true);
    m_idle.setSingleShot(true);
    connect(&m_reconnect, &QTimer::timeout, this, [this] {
        if (m_active && m_mission && m_recovering) emit snapshotRequired();
        else open();
    });
    connect(&m_idle, &QTimer::timeout, this, [this] { if (m_reply) m_reply->abort(); });
}
bool SseClient::setBaseUrl(const QUrl &url)
{
    if (!Protocol::validBaseUrl(url)) return false;
    QPointer<SseClient> guard(this);
    disconnect(); if (!guard) return false;
    m_base = url; return true;
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
    m_parser.reset(); m_errorBody.clear();
}
void SseClient::disconnect()
{
    m_active = false; m_recovering = false; detach(); setState("disconnected");
}
void SseClient::forgetTask()
{
    QPointer<SseClient> guard(this);
    disconnect(); if (!guard) return;
    m_taskId.clear(); m_lastId.clear(); m_sequence=0; m_revision=0;
    m_mission=false; m_invalidCursor=false; emit lastEventIdChanged();
    if (guard) emit missionRevisionChanged();
}
void SseClient::connectToTask(const QString &taskId, const QString &lastEventId)
{
    QPointer<SseClient> guard(this);
    disconnect();
    if (!guard) return;
    m_mission = false; m_revision = 0; m_invalidCursor = false;
    m_parser = SseParser{};
    if (!Protocol::validTaskId(taskId) || Protocol::sequence(lastEventId, taskId) < 0) {
        fail("任务或事件标识无效。"); return;
    }
    m_taskId = taskId; m_lastId = lastEventId;
    m_sequence = Protocol::sequence(lastEventId, taskId);
    const auto generation = m_generation;
    emit lastEventIdChanged();
    if (!guard || generation != m_generation) return;
    m_retry = m_backoff = 1000;
    m_active = true; open();
}
void SseClient::connectToMission(const QString &taskId, const QString &lastEventId, qint64 revision)
{
    QPointer<SseClient> guard(this);
    disconnect();
    if (!guard) return;
    m_mission = true; m_invalidCursor = false;
    // Backend v2 envelopes are bounded to 64 KiB data plus small envelope fields.
    m_parser = SseParser{128 * 1024};
    if (!MissionProtocol::validId(taskId) || Protocol::sequence(lastEventId, taskId) < 0 || revision < 0
        || revision >= std::numeric_limits<qint64>::max()) {
        fail("任务、事件标识或版本无效。"); return;
    }
    m_taskId = taskId; m_lastId = lastEventId;
    m_sequence = Protocol::sequence(lastEventId, taskId); m_revision = revision;
    const auto generation = m_generation;
    emit lastEventIdChanged();
    if (!guard || generation != m_generation) return;
    emit missionRevisionChanged();
    if (!guard || generation != m_generation) return;
    m_retry = m_backoff = 1000; m_active = true; open();
}
bool SseClient::adoptMissionSnapshot(const QString &taskId, const QString &lastEventId, qint64 revision)
{
    const auto sequence = Protocol::sequence(lastEventId, taskId);
    if (!m_mission || taskId != m_taskId || sequence < 0 || revision < m_revision
        || revision >= std::numeric_limits<qint64>::max()
        || (sequence < m_sequence && !m_invalidCursor)) return false;
    const bool idChanged = lastEventId != m_lastId, revisionChanged = revision != m_revision;
    m_lastId = lastEventId; m_sequence = sequence; m_revision = revision; m_invalidCursor = false;
    QPointer<SseClient> guard(this);
    const auto generation = m_generation;
    if (idChanged) emit lastEventIdChanged();
    if (!guard || generation != m_generation) return true;
    if (revisionChanged) emit missionRevisionChanged();
    if (!guard || generation != m_generation) return true;
    if (m_active && m_recovering) {
        m_recovering = false; m_reconnect.stop(); open();
    }
    return true;
}
void SseClient::requireSnapshot(const QString &message, bool invalidCursor)
{
    if (!m_active) return;
    detach(); m_recovering = true; m_invalidCursor = m_invalidCursor || invalidCursor;
    const auto generation = m_generation;
    QPointer<SseClient> guard(this);
    setState("recovering");
    if (!guard || generation != m_generation) return;
    emit connectionError(message);
    if (!guard || generation != m_generation) return;
    m_reconnect.start(m_backoff); m_backoff = std::min(m_backoff * 2, 30000);
}
void SseClient::fail(const QString &message)
{
    m_active = false; detach();
    QPointer<SseClient> guard(this); const auto generation = m_generation;
    setState("error");
    if (guard && generation == m_generation) emit connectionError(message);
}
void SseClient::open()
{
    if (!m_active || m_recovering) return;
    m_parser.reset(); m_errorBody.clear();
    QPointer<SseClient> guard(this); const auto previousGeneration = m_generation;
    setState(m_lastId.isEmpty() ? "connecting" : "reconnecting");
    if (!guard || previousGeneration != m_generation) return;
    auto url = Protocol::endpoint(m_base, '/' + m_taskId + "/events");
    if (m_mission) url.setPath("/api/v2/missions/" + m_taskId + "/events");
    QNetworkRequest request(url);
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
        QPointer<SseClient> guard(this);
        read(reply, generation);
        if (!guard || generation != m_generation) return;
        m_idle.stop();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        m_reply = nullptr; reply->deleteLater();
        if (status == 204) {
            m_active = false; setState("ended");
            if (guard && generation == m_generation) emit streamEnded();
            return;
        }
        if (m_mission) {
            QJsonObject error;
            JsonHttp::parseStrictObject(m_errorBody, error);
            const auto code = error.value("error").toObject().value("code").toString();
            if (code == "invalid_event_id" || code == "event_cursor_ahead") {
                requireSnapshot("事件游标已失效，正在刷新任务快照。", true); return;
            }
            if (status >= 300 && status < 500 && status != 429) {
                fail(MissionProtocol::errorMessage(code, status)); return;
            }
            requireSnapshot("事件连接已断开，正在刷新任务快照后重连。"); return;
        }
        if (status >= 300 && status < 500) {
            fail(QString("事件连接返回 HTTP %1；请刷新任务，已保留事件游标。").arg(status)); return;
        }
        if (!m_active) return;
        setState("reconnecting");
        if (!guard || generation != m_generation) return;
        emit connectionError("事件连接已断开，正在从上次接收的位置重连。");
        if (!guard || generation != m_generation) return;
        m_reconnect.start(m_backoff);
        m_backoff = std::min(m_backoff * 2, 30000);
    });
}
void SseClient::read(QNetworkReply *reply, quint64 generation)
{
    QPointer<SseClient> guard(this);
    if (generation != m_generation || !m_active) return;
    if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() != 200) {
        if (m_mission) {
            m_errorBody += reply->read(64 * 1024 + 1 - m_errorBody.size());
            if (m_errorBody.size() > 64 * 1024) fail("事件错误响应超过安全上限。");
        }
        return;
    }
    if (!reply->header(QNetworkRequest::ContentTypeHeader).toString().startsWith("text/event-stream", Qt::CaseInsensitive)) {
        fail("事件响应类型应为 text/event-stream。"); return;
    }
    setState("connected");
    if (!guard || generation != m_generation) return;
    while (reply->bytesAvailable() && generation == m_generation) {
        const auto frames = m_parser.feed(reply->read(64 * 1024));
        m_idle.start(45000);
        if (!m_parser.error().isEmpty()) { fail(m_parser.error()); return; }
        for (const auto &frame : frames) {
            if (generation != m_generation) return;
            if (frame.retry >= 0) { m_retry = m_backoff = frame.retry; continue; }
            accept(frame);
            if (!guard) return;
        }
    }
}
void SseClient::accept(const SseFrame &frame)
{
    if (m_mission) { acceptMission(frame); return; }
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
    QPointer<SseClient> guard(this);
    const auto generation = m_generation;
    emit lastEventIdChanged();
    if (!guard || generation != m_generation) return;
    emit eventApplied(frame.id, payload);
    if (!guard || generation != m_generation) return;
    const auto state = payload.value("data").toObject().value("state").toString();
    // completed/failed state is followed by a summary; consume that final event too.
    if (frame.event == "task_completed" || frame.event == "task_failed"
        || (frame.event == "task_state_changed" && state == "cancelled")) {
        m_active = false; detach(); const auto endedGeneration = m_generation;
        setState("ended");
        if (guard && endedGeneration == m_generation) emit streamEnded();
    }
}
void SseClient::acceptMission(const SseFrame &frame)
{
    const auto sequence = Protocol::sequence(frame.id, m_taskId);
    if (sequence <= 0) { fail("事件缺少有效的任务内标识。"); return; }
    if (sequence <= m_sequence) return; // An immutable event ID is applied at most once.
    QJsonObject payload;
    if (!JsonHttp::parseStrictObject(frame.data, payload)
        || !MissionProtocol::validEvent(payload, frame.event, m_taskId)
        || payload.value("event_id").toString() != frame.id) {
        fail("事件 JSON 内容、版本或任务标识无效。"); return;
    }
    const auto revision = payload.value("task_revision").toInteger();
    if (sequence != m_sequence + 1 || revision > m_revision + 1) {
        requireSnapshot("事件序列或版本不连续，正在刷新任务快照。"); return;
    }
    // A late revision already covered by a snapshot must never change model state.
    // Consume its contiguous cursor so the following valid event can be processed.
    const bool stale = revision <= m_revision;
    m_lastId = frame.id; m_sequence = sequence; m_backoff = m_retry;
    if (!stale) m_revision = revision;
    QPointer<SseClient> guard(this);
    const auto generation = m_generation;
    emit lastEventIdChanged();
    if (!guard || generation != m_generation || stale) return;
    emit missionRevisionChanged();
    if (!guard || generation != m_generation) return;
    emit eventApplied(frame.id, payload);
    // EOF triggers snapshot reconciliation, including terminal status and artifacts.
    // Do not stop on task.completed: durable events can follow it in the same log.
}
