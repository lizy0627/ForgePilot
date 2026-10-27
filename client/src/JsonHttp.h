#pragma once
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <functional>

namespace JsonHttp {
// Strict wire JSON: object root, unique keys, valid UTF-8 and Unicode scalars.
bool parseStrictObject(const QByteArray &bytes, QJsonObject &object);
struct Response {
    QJsonObject body;
    int status;
    QNetworkReply::NetworkError networkError;
    bool exceededLimit;
    bool invalidJson;
};

// One bounded JSON request. Callers retain request identity, error policy and retry decisions.
QNetworkReply *send(QNetworkAccessManager &network, QObject *owner, const QByteArray &method,
                    const QUrl &url, const QJsonObject &body, qsizetype limit,
                    std::function<void(QNetworkReply *, const Response &)> finished,
                    bool allowReplay = true, bool strictJson = false);
}
