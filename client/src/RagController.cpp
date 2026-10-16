#include "RagController.h"
#include "JsonHttp.h"
#include "Protocol.h"
#include <QJsonDocument>
#include <QNetworkProxy>
#include <QRegularExpression>
#include <QSet>
#include <QUrlQuery>

RagController::RagController(QObject *parent) : QObject(parent)
{
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_reconnect.setSingleShot(true);
    connect(&m_reconnect, &QTimer::timeout, this, &RagController::openEvents);
}

RagController::~RagController()
{
    ++m_generation;
    if (m_request) m_request->abort();
    stopEvents();
}

void RagController::setError(const QString &message)
{
    if (m_error == message) return;
    m_error = message;
    emit errorChanged();
}

void RagController::clearError() { setError({}); }

void RagController::reset()
{
    ++m_generation;
    if (m_request) { m_request->disconnect(this); m_request->abort(); m_request = nullptr; }
    stopEvents();
    m_repositoryId.clear(); m_operationId.clear(); m_revision = 0;
    m_enabled = m_available = m_running = false; m_status = "disabled";
    m_fileCount = m_chunks = m_dirty = m_failedFiles = m_total = m_processed = 0;
    m_lastIndexed.clear(); setError({}); emit changed();
}

void RagController::setBaseUrl(const QUrl &url)
{
    if (url == m_base || !Protocol::validBaseUrl(url)) return;
    reset(); m_base = url; emit changed();
    if (!m_repository.isEmpty()) refresh();
}

void RagController::setRepositoryPath(const QString &path)
{
    if (path == m_repository) return;
    reset(); m_repository = path; emit changed();
    if (!path.isEmpty()) refresh();
}

void RagController::refresh()
{
    if (m_repository.isEmpty() || busy()) return;
    send("GET", "/api/v1/rag/status");
}

void RagController::buildIndex()
{
    if (m_repository.isEmpty() || busy() || m_running) return;
    send("POST", "/api/v1/rag/index", {{"repository_path", m_repository}});
}

void RagController::cancelIndex()
{
    if (m_repository.isEmpty() || busy() || !m_running) return;
    send("POST", "/api/v1/rag/index/cancel", {{"repository_path", m_repository}});
}

void RagController::deleteIndex()
{
    if (m_repository.isEmpty() || busy() || m_running) return;
    send("DELETE", "/api/v1/rag/index");
}

void RagController::send(const QByteArray &method, const QString &path, const QJsonObject &body)
{
    QUrl url(m_base); url.setPath(path);
    if (method != "POST") {
        QUrlQuery query; query.addQueryItem("repository_path", m_repository); url.setQuery(query);
    }
    clearError();
    const auto generation = m_generation;
    auto *reply = JsonHttp::send(m_network, this, method, url, body, 1024 * 1024,
        [this, generation](QNetworkReply *completed, const JsonHttp::Response &response) {
            if (generation != m_generation || completed != m_request) return;
            m_request = nullptr; emit changed();
            const auto error = response.body.value("error").toObject();
            if (response.exceededLimit || response.invalidJson || response.status < 200
                    || response.status >= 300 || response.networkError != QNetworkReply::NoError) {
                const auto code = error.value("code").toString("network_error");
                const auto message = error.value("message").toString("代码索引请求失败，请刷新后重试。");
                setError(code + "：" + message);
                return;
            }
            if (!applyStatus(response.body)) return;
            if (m_running) openEvents(); else stopEvents();
        });
    m_request = reply; emit changed();
}

bool RagController::applyStatus(const QJsonObject &body)
{
    static const QRegularExpression repositoryId("^[0-9a-f]{64}$");
    static const QRegularExpression operationId("^[0-9a-f]{32}$");
    static const QSet<QString> statuses{"disabled","unavailable","empty","pending","indexing",
        "cancelling","ready","degraded","failed","cancelled"};
    const auto id = body.value("repository_id").toString();
    const auto operation = body.value("operation_id");
    const auto indexedAt = body.value("last_indexed_at");
    const auto status = body.value("status").toString();
    const auto revision = body.value("revision").toInteger(-1);
    const auto integer = [&body](const char *name) {
        const auto value = body.value(QLatin1String(name));
        return value.isDouble() && value.toInteger(-1) >= 0;
    };
    if (!repositoryId.match(id).hasMatch() || !statuses.contains(status)
            || (!operation.isNull() && (!operation.isString()
                || !operationId.match(operation.toString()).hasMatch()))
            || !body.value("enabled").isBool() || !body.value("available").isBool()
            || !body.value("running").isBool() || revision < 0
            || (!indexedAt.isNull() && !indexedAt.isString())
            || !integer("file_count") || !integer("chunks") || !integer("dirty_file_count")
            || !integer("failed_file_count") || !integer("total_files") || !integer("processed")) {
        setError("代码索引状态响应格式无效。");
        return false;
    }
    m_repositoryId = id; m_operationId = operation.toString(); m_status = status;
    m_enabled = body.value("enabled").toBool(); m_available = body.value("available").toBool();
    m_running = body.value("running").toBool(); m_revision = revision;
    m_fileCount = body.value("file_count").toInt(); m_chunks = body.value("chunks").toInt();
    m_dirty = body.value("dirty_file_count").toInt();
    m_failedFiles = body.value("failed_file_count").toInt();
    m_total = body.value("total_files").toInt(); m_processed = body.value("processed").toInt();
    m_lastIndexed = indexedAt.toString();
    emit changed();
    return true;
}

void RagController::stopEvents()
{
    m_reconnect.stop(); m_parser.reset();
    if (!m_stream) return;
    auto *reply = m_stream.data(); m_stream = nullptr;
    reply->disconnect(this); reply->abort(); reply->deleteLater();
}

void RagController::openEvents()
{
    if (!m_running || m_repository.isEmpty() || m_stream) return;
    m_parser.reset();
    QUrl url(m_base); url.setPath("/api/v1/rag/index/events");
    QUrlQuery query; query.addQueryItem("repository_path", m_repository); url.setQuery(query);
    QNetworkRequest request(url); request.setRawHeader("Accept", "text/event-stream");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    auto *reply = m_network.get(request); reply->setReadBufferSize(128 * 1024); m_stream = reply;
    const auto generation = m_generation;
    connect(reply, &QIODevice::readyRead, this, [this, reply, generation] { readEvents(reply, generation); });
    connect(reply, &QNetworkReply::finished, this, [this, reply, generation] {
        if (generation != m_generation || reply != m_stream) return;
        readEvents(reply, generation); m_stream = nullptr; reply->deleteLater();
        if (m_running) m_reconnect.start(1000);
    });
}

void RagController::readEvents(QNetworkReply *reply, quint64 generation)
{
    if (generation != m_generation || reply != m_stream) return;
    if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() != 200) return;
    const auto frames = m_parser.feed(reply->readAll());
    if (!m_parser.error().isEmpty()) { setError("代码索引进度流格式无效。"); stopEvents(); return; }
    for (const auto &frame : frames) {
        if (frame.event != "rag_index_progress") continue;
        QJsonParseError error;
        const auto document = QJsonDocument::fromJson(frame.data, &error);
        const auto body = document.object();
        const auto operation = body.value("operation_id").toString();
        const auto revision = body.value("revision").toInteger(-1);
        const auto expectedId = operation + ':' + QString::number(revision);
        if (error.error != QJsonParseError::NoError || !document.isObject()
                || operation.isEmpty() || operation != m_operationId || frame.id != expectedId) {
            if (m_error.isEmpty()) setError("代码索引进度响应格式无效。");
            stopEvents(); return;
        }
        if (revision <= m_revision) continue;
        if (!applyStatus(body)) { stopEvents(); return; }
        if (!m_running) { stopEvents(); return; }
    }
}
