#include "JsonHttp.h"
#include <QJsonDocument>
#include <QJsonArray>
#include <QSet>
#include <QStringDecoder>
#include <QTimer>
#include <QtGlobal>
#include <memory>
#include <cstring>

namespace {
// QJsonDocument accepts duplicate object members and replacement Unicode. Check
// those ambiguities before handing the document to consumers of the v2 contract.
class StrictJson {
public:
    explicit StrictJson(const QByteArray &bytes) : m_bytes(bytes) {}
    bool check() { return value(0) && (space(), m_at == m_bytes.size()); }
private:
    void space() { while (m_at < m_bytes.size() && QByteArray(" \t\r\n").contains(m_bytes[m_at])) ++m_at; }
    bool take(char c) { space(); if (m_at >= m_bytes.size() || m_bytes[m_at] != c) return false; ++m_at; return true; }
    bool hex(quint16 &number) {
        if (m_at + 4 > m_bytes.size()) return false;
        bool ok = false; number = m_bytes.mid(m_at, 4).toUShort(&ok, 16); m_at += 4; return ok;
    }
    bool string(QString *decoded = nullptr) {
        space(); const auto start = m_at;
        if (!take('"')) return false;
        while (m_at < m_bytes.size()) {
            const auto c = m_bytes[m_at++];
            if (c == '"') {
                if (decoded) *decoded = QJsonDocument::fromJson('[' + m_bytes.mid(start, m_at - start) + ']').array().first().toString();
                return true;
            }
            if (static_cast<unsigned char>(c) < 32) return false;
            if (c != '\\') continue;
            if (m_at >= m_bytes.size()) return false;
            const auto escape = m_bytes[m_at++];
            if (escape == 'u') {
                quint16 code = 0; if (!hex(code)) return false;
                if (QChar::isHighSurrogate(code)) {
                    if (m_at + 2 > m_bytes.size() || m_bytes.mid(m_at, 2) != "\\u") return false;
                    m_at += 2; if (!hex(code) || !QChar::isLowSurrogate(code)) return false;
                } else if (QChar::isLowSurrogate(code)) return false;
            } else if (!QByteArray("\"\\/bfnrt").contains(escape)) return false;
        }
        return false;
    }
    bool value(int depth) {
        if (depth > 128) return false;
        space(); if (m_at >= m_bytes.size()) return false;
        if (m_bytes[m_at] == '"') return string();
        if (m_bytes[m_at] == '{') {
            ++m_at; if (take('}')) return true; QSet<QString> keys;
            do { QString key; if (!string(&key) || keys.contains(key) || !take(':')) return false;
                keys.insert(key); if (!value(depth + 1)) return false;
            } while (take(','));
            return take('}');
        }
        if (m_bytes[m_at] == '[') {
            ++m_at; if (take(']')) return true;
            do { if (!value(depth + 1)) return false; } while (take(','));
            return take(']');
        }
        const auto start = m_at;
        while (m_at < m_bytes.size() && !QByteArray(" \t\r\n,]}").contains(m_bytes[m_at])) ++m_at;
        return m_at > start; // QJsonDocument validates numbers and literal spelling.
    }
    const QByteArray &m_bytes;
    qsizetype m_at = 0;
};
// A feedback POST must remain a single submission even if the connection closes
// before response headers. Qt can otherwise rewind buffered bodies and resend.
class SingleSubmissionBody final : public QIODevice {
public:
    explicit SingleSubmissionBody(QByteArray bytes) : m_bytes(std::move(bytes)) { open(ReadOnly); }
    bool isSequential() const override { return true; }
    bool reset() override { return false; }
    qint64 bytesAvailable() const override { return m_bytes.size() - m_offset + QIODevice::bytesAvailable(); }
protected:
    qint64 readData(char *data, qint64 maximum) override {
        const auto size = qMin(maximum, qint64(m_bytes.size() - m_offset));
        std::memcpy(data, m_bytes.constData() + m_offset, size_t(size));
        m_offset += size;
        return size;
    }
    qint64 writeData(const char *, qint64) override { return -1; }
private:
    QByteArray m_bytes;
    qint64 m_offset = 0;
};
}

void JsonHttp::authenticate(QNetworkRequest &request)
{
    const QByteArray token = qgetenv("FORGEPILOT_LOCAL_API_TOKEN");
    if (!token.isEmpty()) request.setRawHeader("X-ForgePilot-Token", token);
}

bool JsonHttp::parseStrictObject(const QByteArray &bytes, QJsonObject &object)
{
    QStringDecoder decoder(QStringDecoder::Utf8);
    const QString decoded = decoder(bytes);
    Q_UNUSED(decoded);
    if (decoder.hasError()) return false;
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(bytes, &error);
    if (error.error != QJsonParseError::NoError || !document.isObject() || !StrictJson(bytes).check()) return false;
    object = document.object();
    return true;
}

QNetworkReply *JsonHttp::send(QNetworkAccessManager &network, QObject *owner, const QByteArray &method,
                             const QUrl &url, const QJsonObject &body, qsizetype limit,
                             std::function<void(QNetworkReply *, const Response &)> finished,
                             bool allowReplay, bool strictJson)
{
    QNetworkRequest request(url);
    authenticate(request);
    request.setRawHeader("Accept", "application/json");
    request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    request.setTransferTimeout(30000);
    QNetworkReply *reply;
    if (method == "GET") reply = network.get(request);
    else {
        const auto bytes = QJsonDocument(body).toJson(QJsonDocument::Compact);
        if (allowReplay) reply = network.sendCustomRequest(request, method, bytes);
        else {
            request.setAttribute(QNetworkRequest::DoNotBufferUploadDataAttribute, true);
            request.setHeader(QNetworkRequest::ContentLengthHeader, bytes.size());
            auto *upload = new SingleSubmissionBody(bytes);
            reply = network.sendCustomRequest(request, method, upload);
            upload->setParent(reply);
        }
    }
    reply->setReadBufferSize(128 * 1024);
    struct Buffer { QByteArray bytes; bool exceeded = false; };
    auto buffer = std::make_shared<Buffer>();
    QObject::connect(reply, &QNetworkReply::metaDataChanged, owner, [reply, buffer, limit] {
        bool valid = false;
        const auto length = reply->header(QNetworkRequest::ContentLengthHeader).toLongLong(&valid);
        if (valid && length > limit && !buffer->exceeded) {
            buffer->exceeded = true;
            reply->abort();
        }
    });
    auto consume = [reply, buffer, limit] {
        while (!buffer->exceeded && reply->bytesAvailable()) {
            buffer->bytes.append(reply->read(64 * 1024));
            if (buffer->bytes.size() > limit) { buffer->exceeded = true; reply->abort(); }
        }
    };
    QObject::connect(reply, &QIODevice::readyRead, owner, consume);
    auto *deadline = new QTimer(reply);
    deadline->setSingleShot(true);
    QObject::connect(deadline, &QTimer::timeout, reply, &QNetworkReply::abort);
    deadline->start(60000);
    QObject::connect(reply, &QNetworkReply::finished, owner, [=] {
        deadline->stop();
        consume();
        QJsonParseError parse;
        // An oversized partial document is already rejected; do not allocate a
        // second parsed copy. Streaming checks still cover absent/false lengths.
        const auto document = buffer->exceeded ? QJsonDocument{} : QJsonDocument::fromJson(buffer->bytes, &parse);
        auto object = document.object();
        const bool invalidStrictJson = strictJson && !buffer->exceeded && !parseStrictObject(buffer->bytes, object);
        const Response response{object,
            reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt(), reply->error(),
            buffer->exceeded, buffer->exceeded || parse.error != QJsonParseError::NoError || !document.isObject() || invalidStrictJson};
        reply->deleteLater();
        finished(reply, response);
    });
    return reply;
}
