#pragma once
#include <QJsonObject>
#include <QUrl>

namespace Protocol {
bool validBaseUrl(const QUrl &url);
bool validTaskId(const QString &id);
qint64 sequence(const QString &id, const QString &taskId);
bool terminal(const QString &state);
bool validState(const QString &state);
bool validEvent(const QJsonObject &payload, const QString &event, const QString &taskId);
QUrl endpoint(const QUrl &base, const QString &path);
}
