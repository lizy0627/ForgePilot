#include "AgentApiClient.h"
#include "Protocol.h"
#include "JsonHttp.h"
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
    for (auto *reply : pending) { reply->disconnect(this); reply->abort(); reply->deleteLater(); }
}
quint64 AgentApiClient::send(const QString &operation, const QString &id, const QJsonObject &body)
{
    const auto serial = ++m_next;
    if (!id.isEmpty() && !Protocol::validTaskId(id)) {
        QTimer::singleShot(0, this, [this, serial, operation, id] {
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
    return serial;
}
