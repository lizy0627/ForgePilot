#include "AgentApiClient.h"
#include "Protocol.h"
#include "JsonHttp.h"
#include "workbench/MissionProtocol.h"
#include <QJsonArray>
#include <QNetworkProxy>
#include <QTimer>
#include <QUrlQuery>

AgentApiClient::AgentApiClient(QObject *parent) : QObject(parent)
{
    m_network.setProxy(QNetworkProxy::NoProxy);
}
bool AgentApiClient::setBaseUrl(const QUrl &url)
{
    if (!Protocol::validBaseUrl(url)) return false;
    abortPending();
    m_base = url;
    return true;
}
quint64 AgentApiClient::createTask(const QString &repositoryPath, const QString &prompt)
{ return send("create", {}, {{"repository_path", repositoryPath}, {"prompt", prompt}}); }
quint64 AgentApiClient::approveTask(const QString &id, int version)
{ return send("approve", id, {{"plan_version", version}}); }
quint64 AgentApiClient::feedbackTask(const QString &id, int version, const QString &feedback)
{ return send("feedback", id, {{"plan_version", version}, {"feedback", feedback}}); }
quint64 AgentApiClient::cancelTask(const QString &id, bool reject, int version)
{ return send("cancel", id, reject ? QJsonObject{{"reason", "reject"}, {"plan_version", version}} : QJsonObject{}); }
quint64 AgentApiClient::getTask(const QString &id) { return send("get", id); }
quint64 AgentApiClient::listTasks(const QString &before, const QString &conversationId) { return send("list",{},{{"before",before},{"conversation_id",conversationId}}); }
quint64 AgentApiClient::getTrace(const QString &id,qint64 after) { return send("trace",id,{{"after",after}}); }
quint64 AgentApiClient::getTraceDetail(const QString &id,qint64 sequence) { return send("trace_detail",id,{{"sequence",sequence}}); }
void AgentApiClient::abortPending()
{
    const auto pending = m_pending;
    m_pending.clear();
    m_requests.clear();
    m_rejections.clear();
    for (auto *reply : pending) { reply->disconnect(this); reply->abort(); reply->deleteLater(); }
}
void AgentApiClient::abortRequest(quint64 serial)
{
    m_rejections.remove(serial);
    auto *reply = m_requests.take(serial);
    if (!reply) return;
    m_pending.remove(reply);
    reply->disconnect(this);
    reply->abort();
    reply->deleteLater();
}
quint64 AgentApiClient::send(const QString &operation, const QString &id, const QJsonObject &body)
{
    const auto serial = ++m_next;
    if (!id.isEmpty() && !Protocol::validTaskId(id)) {
        m_rejections.insert(serial);
        QTimer::singleShot(0, this, [this, serial, operation, id] {
            if (!m_rejections.remove(serial)) return;
            emit requestError(serial, operation, id, 0, "invalid_task_id", "任务标识无效。");
        });
        return serial;
    }
    QString path = id.isEmpty() ? QString{} : '/' + id;
    if (operation == "approve" || operation == "cancel" || operation == "feedback") path += '/' + operation;
    if(operation=="trace" || operation=="trace_detail") path += "/trace";
    if(operation=="trace_detail") path += '/' + QString::number(body.value("sequence").toInteger());
    auto url=Protocol::endpoint(m_base,path);
    QUrlQuery query;
    if(operation=="list") {
        query.addQueryItem("limit","20");
        if(!body.value("conversation_id").toString().isEmpty()) query.addQueryItem("conversation_id",body.value("conversation_id").toString());
        if(!body.value("before").toString().isEmpty()) query.addQueryItem("before",body.value("before").toString());
    }
    if(operation=="trace") { query.addQueryItem("limit","50"); query.addQueryItem("after",QString::number(body.value("after").toInteger())); }
    url.setQuery(query);
    const bool readOnly = operation == "get" || operation == "list" || operation == "trace" || operation == "trace_detail";
    auto *reply = JsonHttp::send(m_network, this, readOnly ? "GET" : "POST", url, body, 64 * 1024 * 1024,
        [=, this](QNetworkReply *completed, const JsonHttp::Response &response) {
        if (!m_pending.remove(completed)) return;
        m_requests.remove(serial);
        const auto &object = response.body;
        const int status = response.status;
        QString code, message;
        if (response.exceededLimit) { code = "response_limit"; message = "响应超过 64 MiB 上限。"; }
        else if (status < 200 || status >= 300) {
            const auto error = object.value("error").toObject();
            code = error.value("code").toString(status ? "http_error" : "network_error");
            message = error.value("message").toString(readOnly
                ? "读取失败，请检查后端地址及服务是否已启动，然后刷新。"
                : "请求失败；已发送的操作可能已经执行，请先刷新任务状态再决定是否重试。");
        } else if (response.networkError != QNetworkReply::NoError) {
            code = "network_error"; message = readOnly ? "读取响应不完整，请刷新重试。"
                : "响应不完整；已发送的操作可能已经执行，请先刷新任务状态再决定是否重试。";
        } else if (response.invalidJson) {
            code = "invalid_response"; message = "后端响应应为 JSON 对象。";
        } else if (operation == "feedback" && status != 202) {
            code = "invalid_response"; message = "未收到反馈受理的 202 确认，请核对完整执行记录。";
        }
        if (!code.isEmpty()) {
            emit requestError(serial, operation, id, status, code, message);
        } else {
            emit responseReceived(serial, operation, id, object);
        }
    }, operation != "feedback");
    m_pending.insert(reply);
    m_requests.insert(serial, reply);
    return serial;
}

quint64 AgentApiClient::createMission(const QJsonObject &body) {
    auto request = body;
    if (request.value("selected_skills") == QJsonValue(QJsonArray{})) request.remove("selected_skills");
    return sendMission("mission.create", {}, request);
}
quint64 AgentApiClient::recommendSkills(const QJsonObject &body, const QUrl &backend)
{ return sendMission("mission.recommendations", {}, body, backend); }
quint64 AgentApiClient::previewMission(const QJsonObject &body, const QUrl &backend)
{ return sendMission("mission.preview", {}, body, backend); }
quint64 AgentApiClient::discardMissionPreview(const QString &token, const QUrl &backend)
{ return sendMission("mission.discard_preview", {}, {{"preview_token", token}}, backend); }
quint64 AgentApiClient::listMissions(const QString &before, const QString &repositoryPath, const QString &status) {
    return sendMission("mission.list", {}, {{"before", before}, {"repository_path", repositoryPath}, {"status", status}});
}
quint64 AgentApiClient::getMission(const QString &id) { return sendMission("mission.get", id); }
quint64 AgentApiClient::getMissionGraph(const QString &id) { return sendMission("mission.graph", id); }
quint64 AgentApiClient::getMissionSkills(const QString &id) { return sendMission("mission.skills", id); }
quint64 AgentApiClient::getMissionScopeExpansion(const QString &id) { return sendMission("mission.scope-expansion", id); }
quint64 AgentApiClient::getMissionArtifacts(const QString &id) { return sendMission("mission.artifacts", id); }
quint64 AgentApiClient::getMissionArtifact(const QString &id, const QString &artifactId) { return sendMission("mission.artifact", id, {{"artifact_id", artifactId}}); }
quint64 AgentApiClient::getMissionDiff(const QString &id) { return sendMission("mission.diff", id); }
quint64 AgentApiClient::getMissionValidation(const QString &id) { return sendMission("mission.validation", id); }
quint64 AgentApiClient::approveMission(const QString &id, const QJsonObject &body) { return sendMission("mission.approve", id, body); }
quint64 AgentApiClient::cancelMission(const QString &id, qint64 revision) { return sendMission("mission.cancel", id, {{"expected_revision", revision}}); }
quint64 AgentApiClient::retryMission(const QString &id, qint64 revision, const QString &nodeId) {
    QJsonObject body{{"expected_revision", revision}};
    if (!nodeId.isEmpty()) body.insert("node_id", nodeId);
    return sendMission("mission.retry", id, body);
}
quint64 AgentApiClient::resumeMission(const QString &id, qint64 revision, const QString &token) {
    QJsonObject body{{"expected_revision", revision}};
    if (!token.isEmpty()) body.insert("recovery_token", token);
    return sendMission("mission.resume", id, body);
}
quint64 AgentApiClient::deliverMission(const QString &id, const QJsonObject &body) { return sendMission("mission.delivery", id, body); }
quint64 AgentApiClient::rejectRequest(const QString &op, const QString &id, const QString &code) {
    const auto serial = ++m_next;
    m_rejections.insert(serial);
    QTimer::singleShot(0, this, [this, serial, op, id, code] {
        if (m_rejections.remove(serial)) emit requestError(serial, op, id, 0, code, MissionProtocol::errorMessage(code));
    });
    return serial;
}
quint64 AgentApiClient::sendMission(const QString &op, const QString &id, const QJsonObject &body, const QUrl &backend) {
    if (!backend.isEmpty() && !Protocol::validBaseUrl(backend)) return rejectRequest(op, id, "invalid_request");
    const bool needsTaskId = !QSet<QString>{"mission.create", "mission.recommendations", "mission.preview",
        "mission.discard_preview", "mission.list"}.contains(op);
    if (needsTaskId && !MissionProtocol::validId(id)) return rejectRequest(op, id, "invalid_task_id");
    if (!MissionProtocol::validRequest(op, body)) return rejectRequest(op, id, "invalid_request");
    const auto serial = ++m_next;
    QString path = id.isEmpty() ? QString{} : '/' + id;
    const auto action = op.mid(8);
    const bool readOnly = QSet<QString>{"get", "list", "graph", "skills", "scope-expansion", "artifacts", "artifact", "diff", "validation"}.contains(action);
    if (action == "discard_preview") path += "/previews/" + body.value("preview_token").toString();
    else if (action == "approve") path += "/approvals";
    else if (action == "artifact") path += "/artifacts/" + body.value("artifact_id").toString();
    else if (action != "create" && action != "get" && action != "list") path += '/' + action;
    const auto &base = backend.isEmpty() ? m_base : backend;
    auto url = action == "recommendations" ? base.resolved(QUrl("/api/v2/skills/recommendations"))
                                           : MissionProtocol::endpoint(base, path);
    if (action == "list") {
        QStringList query{"limit=20"};
        for (const auto &key : {"before", "repository_path", "status"}) {
            const auto value = body.value(key).toString();
            if (!value.isEmpty())
                query.append(QString::fromLatin1(key) + '=' + QString::fromLatin1(QUrl::toPercentEncoding(value)));
        }
        url.setQuery(query.join('&'), QUrl::StrictMode);
    }
    const QByteArray method = action == "discard_preview" ? "DELETE" : readOnly ? "GET" : "POST";
    auto *reply = JsonHttp::send(m_network, this, method, url,
        readOnly || action == "discard_preview" ? QJsonObject{} : body, 64 * 1024 * 1024,
        [=, this](QNetworkReply *completed, const JsonHttp::Response &response) {
            if (!m_pending.remove(completed)) return;
            m_requests.remove(serial);
            QString code;
            const auto status = response.status;
            if (response.exceededLimit) code = "response_limit";
            else if (!status) code = "network_error";
            else if (status < 200 || status >= 300) {
                code = !response.invalidJson && MissionProtocol::validError(response.body, id)
                    ? response.body.value("error").toObject().value("code").toString() : "invalid_response";
            } else if (response.networkError != QNetworkReply::NoError) code = "network_error";
            else if (response.invalidJson || !MissionProtocol::validResponse(op, id, response.body)
                || ((action == "recommendations" || action == "skills") && status != 200)
                || ((action == "create" || action == "approve" || action == "retry" || action == "resume") && status != 202)
                || (action == "artifact" && response.body.value("artifact_id") != body.value("artifact_id"))) code = "invalid_response";
            if (!code.isEmpty()) {
                const bool creationDetail = (action == "preview" || action == "create")
                    && MissionProtocol::validError(response.body, id);
                emit requestError(serial, op, id, status, code, creationDetail
                    ? response.body.value("error").toObject().value("message").toString()
                    : MissionProtocol::errorMessage(code, status));
            }
            else emit responseReceived(serial, op, id, response.body);
        }, readOnly, true); // Mutations are never replayed by Qt after a dropped connection.
    m_pending.insert(reply);
    m_requests.insert(serial, reply);
    return serial;
}
