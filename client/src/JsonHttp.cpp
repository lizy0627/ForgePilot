#include "JsonHttp.h"
#include <QJsonDocument>
#include <QTimer>
#include <memory>
#include <cstring>

namespace {
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

QNetworkReply *JsonHttp::send(QNetworkAccessManager &network, QObject *owner, const QByteArray &method,
                             const QUrl &url, const QJsonObject &body, qsizetype limit,
                             std::function<void(QNetworkReply *, const Response &)> finished,
                             bool allowReplay)
{
    QNetworkRequest request(url);
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
        const Response response{document.object(),
            reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt(), reply->error(),
            buffer->exceeded, buffer->exceeded || parse.error != QJsonParseError::NoError || !document.isObject()};
        reply->deleteLater();
        finished(reply, response);
    });
    return reply;
}
