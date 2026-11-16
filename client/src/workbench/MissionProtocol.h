#pragma once
#include <QJsonObject>
#include <QUrl>

// V2 transport validation is independent of the legacy Task protocol. Unknown
// additive response fields are tolerated; required and consumed fields are typed.
namespace MissionProtocol {
bool validId(const QString &id);
bool validHash(const QJsonValue &value);
bool validRevision(const QJsonValue &value);
bool validState(const QString &state);
bool terminal(const QString &state);
bool validMission(const QJsonObject &body);
bool validMissionPage(const QJsonObject &body);
bool validGraph(const QJsonObject &body);
bool validArtifact(const QJsonObject &body);
bool validArtifacts(const QJsonObject &body);
bool validDiff(const QJsonObject &body);
bool validValidation(const QJsonObject &body);
bool validEvent(const QJsonObject &body, const QString &event, const QString &taskId);
bool validResponse(const QString &operation, const QString &taskId, const QJsonObject &body);
bool validRequest(const QString &operation, const QJsonObject &body);
bool validError(const QJsonObject &body, const QString &taskId);
bool staleError(const QString &code);
QString errorMessage(const QString &code, int status = 0);
QUrl endpoint(const QUrl &base, const QString &path = {});
}
