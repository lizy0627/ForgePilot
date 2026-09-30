#pragma once
// Shared loopback HTTP fixture: responses can be held to order client races.
#include <QJsonArray>
#include <QJsonDocument>
#include <QPointer>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <functional>
#include <memory>

const QString A = "11111111-1111-4111-8111-111111111111";
const QString B = "22222222-2222-4222-8222-222222222222";

struct Request { QPointer<QTcpSocket> socket; QByteArray method, path, headers; QJsonObject body; };
class Source : public QTcpServer {
public:
    QList<Request> requests;
    std::function<void(const Request &)> handle;
    Source() {
        connect(this, &QTcpServer::newConnection, this, [this] {
            while (hasPendingConnections()) {
                auto *socket = nextPendingConnection();
                connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
                connect(socket, &QTcpSocket::readyRead, this, [=, this] {
                    auto bytes = socket->property("input").toByteArray() + socket->readAll();
                    socket->setProperty("input", bytes);
                    if (socket->property("handled").toBool()) return;
                    const auto boundary = bytes.indexOf("\r\n\r\n");
                    if (boundary < 0) return;
                    int size = 0;
                    for (const auto &line : bytes.left(boundary).split('\n'))
                        if (line.toLower().startsWith("content-length:")) size = line.mid(15).trimmed().toInt();
                    if (bytes.size() < boundary + 4 + size) return;
                    socket->setProperty("handled", true);
                    const auto start = bytes.left(bytes.indexOf('\r')).split(' ');
                    Request r{socket,start[0],start[1],bytes.left(boundary),QJsonDocument::fromJson(bytes.mid(boundary+4,size)).object()};
                    requests.append(r);
                    if (handle) handle(r);
                });
            }
        });
        if (!listen(QHostAddress::LocalHost)) qFatal("Cannot listen");
    }
    QUrl url() const { return QUrl(QString("http://127.0.0.1:%1").arg(serverPort())); }
    static void json(const Request &r, const QJsonObject &body, int status=200) {
        const auto data = QJsonDocument(body).toJson(QJsonDocument::Compact);
        if (!r.socket) return;
        r.socket->write("HTTP/1.1 " + QByteArray::number(status) + " Result\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + QByteArray::number(data.size()) + "\r\n\r\n" + data);
        r.socket->disconnectFromHost();
    }
    static void stream(const Request &r, const QByteArray &bytes, bool close=true, bool fragment=false) {
        if (!r.socket) return;
        r.socket->write("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nConnection: close\r\n\r\n");
        if (!fragment) { r.socket->write(bytes); if(close) r.socket->disconnectFromHost(); return; }
        auto *timer = new QTimer(r.socket);
        auto position = std::make_shared<qsizetype>(0);
        QObject::connect(timer, &QTimer::timeout, r.socket, [=] {
            if (*position == bytes.size()) {
                timer->stop(); timer->deleteLater(); if(close) r.socket->disconnectFromHost(); return;
            }
            r.socket->write(bytes.mid((*position)++,1));
        });
        timer->start(1);
    }
};
QJsonObject payload(const QString &type, QJsonObject data={}, const QString &id=A)
{ return {{"event",type},{"task_id",id},{"timestamp","2026-09-18T00:00:00+00:00"},{"data",data}}; }
QByteArray sseEvent(int sequence, const QString &type, QJsonObject data={}, const QString &id=A)
{
    return "id: " + id.toUtf8() + ':' + QByteArray::number(sequence) + "\r\nevent: " + type.toUtf8()
        + "\r\ndata: " + QJsonDocument(payload(type,data,id)).toJson(QJsonDocument::Compact) + "\r\n\r\n";
}
QJsonObject snapshot(const QString &id=A, const QString &state="waiting_approval", int sequence=3, int version=1)
{
    return {{"task_id",id},{"state",state},{"repository_path","C:/fixture"},{"plan_version",version},
        {"plan",QJsonObject{{"summary","中文方案"}}},{"last_event_id",id+':'+QString::number(sequence)},
        {"updated_at","2026-09-18T00:00:00+00:00"}};
}

