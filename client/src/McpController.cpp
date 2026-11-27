#include "McpController.h"
#include "BackendConnection.h"
#include "Protocol.h"
#include <QDir>
#include <QFileInfo>
#include <QHostAddress>
#include <QJsonArray>
#include <QRegularExpression>
#include <QUuid>

namespace {
QJsonArray stdioModes(const QJsonObject &draft)
{
    return draft.value("modes").toArray().contains("task") ? QJsonArray{"task"} : QJsonArray{};
}
QJsonArray credentialScope(const QJsonObject &config)
{
    if(config.value("transport")!="streamable_http") return {"stdio"};
    const auto c=config.value("connection").toObject();
    const auto endpoint=c.value("endpoint").toString(); const QUrl url(endpoint);
    // QUrl normalizes escapes; preserve the literal path just as urllib.urlsplit does.
    const auto start=endpoint.indexOf('/',endpoint.indexOf("://")+3);
    const auto path=start<0?QString("/"):endpoint.mid(start);
    return {"streamable_http",url.scheme().toLower(),url.host().toLower(),
        url.port(url.scheme().toLower()=="https"?443:80),path,
        c.value("auth_type"),c.value("header_name").toString().toLower()};
}
QString errorCode(const JsonHttp::Response &r)
{ return r.invalidJson?QString{}:r.body.value("error").toObject().value("code").toString(); }
const QMap<QString,QString> &errorMessages()
{
    static const QMap<QString,QString> messages{
        {"mcp_auth","鉴权失败，请检查密钥及服务权限。"},
        {"mcp_secret_required","缺少凭证，请填写密钥或移除对应鉴权/环境变量。"},
        {"mcp_secret_unavailable","已保存凭证无法解密，请重新填写所有凭证。"},
        {"mcp_config_conflict","配置已变更，请重新读取此服务后再编辑。"},
        {"unknown_mcp_server","服务已不存在，请刷新列表。"},
        {"mcp_disabled","后端已禁用 MCP；仅可查看、停用和删除。"},
        {"mcp_not_mcp","地址不是可用的 MCP 端点，请检查完整 /mcp 地址。"},
        {"mcp_capacity","服务繁忙或请求过多，请稍后重试。"},
        {"mcp_timeout","连接检查超时，请检查服务后重试。"},
        {"mcp_catalog_changed","工具定义已变化，请重新连接并选择工具。"},
        {"mcp_preset_mismatch","预设不匹配：缺少可用的 Tavily 搜索/提取工具。"},
        {"mcp_limit","超过配置、工具或响应限额，请减少服务、工具选择或参数。"},
        {"mcp_executable_missing","程序不存在，请重新选择已安装的 .exe 文件。"},
        {"mcp_config","配置字段无效，请检查地址、路径、参数和鉴权。"},
        {"invalid_request","配置字段或格式无效，请检查表单。"},
        {"mcp_selection","请至少选择一个兼容的读取/查询工具。"},
        {"mcp_protocol","服务返回了无效 MCP 协议，请检查端点。"},
        {"mcp_transport_unsupported","服务使用了不支持的传输方式。"},
        {"mcp_version","服务 MCP 协议版本不兼容。"},
        {"mcp_redirect","地址发生跳转，请填写服务提供方给出的最终 MCP 地址。"},
        {"mcp_disconnected","服务连接已断开，请检查服务是否运行，再重试。"},
        {"mcp_unavailable","服务暂不可用，请检查远程服务或本地程序后重试。"},
        {"mcp_capability","该地址未提供可用的 MCP 工具能力，请检查端点。"},
        {"mcp_storage","配置未能保存，原配置已保留。请检查本地存储后重试。"},
        {"mcp_catalog","服务返回的工具列表无效，请检查服务版本。"},
        {"mcp_request","服务拒绝了本次请求，请检查服务配置及可用状态。"},
        {"server_stopping","后端正在停止，请启动后端后重试。"},
        {"mcp_transient","服务暂时不可用，请稍后重试。"},
        {"mcp_session_expired","服务会话已失效，请重新连接。"}
    };
    return messages;
}
}

McpController::McpController(BackendConnection &connection) : m_connection(connection)
{
    connect(&connection, &BackendConnection::baseUrlChanged, this, [this] {
        ++m_missionGeneration;
        const auto replies = m_missionReplies; m_missionReplies.clear();
        for (auto *reply : replies) reply->abort();
        m_missionChoices.clear(); m_missionPending = 0; m_missionLoading = false; m_missionError.clear(); emit missionChoicesChanged();
        const bool reopen = m_open;
        const bool pendingSave = m_pending.has_value();
        const bool pendingAction = m_busy;
        m_pending.reset(); m_resolving=false; m_state="editing";
        close();
        if (pendingSave || pendingAction) {
            m_notice = pendingSave ? "旧后端的上次保存结果待确认，请切回原地址核对服务配置。"
                                   : "旧后端的操作结果待确认，请切回原地址核对服务状态。";
            emit changed();
        }
        if (reopen) open();
    });
}
bool McpController::ok(const Response &r)
{ return r.status>=200 && r.status<300 && r.networkError==QNetworkReply::NoError && !r.exceededLimit && !r.invalidJson; }
QString McpController::failure(const Response &r)
{
    // Never display raw transport/server text: even a wrong backend may echo a body.
    if (r.exceededLimit) return "响应超过大小限制，请减少工具或参数。";
    const auto code = errorCode(r);
    const auto &messages = errorMessages();
    // Only stable, allowlisted codes are safe to expose; never echo unknown fields.
    if (messages.contains(code)) return messages.value(code)+"（"+code+"）";
    if (r.status==409) return "配置冲突，请重新读取此服务。";
    if (r.status==429) return "服务繁忙，请稍后重试。";
    if (r.status==404) return "服务或 MCP 管理接口不存在，请检查后端版本。";
    return "请求未完成，请检查后端与 MCP 服务连接后重试。";
}
void McpController::request(bool editor, const QByteArray &method, const QString &path,
                            const QJsonObject &body, std::function<void(const Response &)> callback)
{
    const auto generation=m_generation, editGeneration=m_editorGeneration;
    const auto url = m_connection.baseUrl().resolved(QUrl("/api/v1/mcp" + path));
    auto *reply=JsonHttp::send(m_network,this,method,url,body,512*1024,
        [=,this](QNetworkReply *reply,const Response &response) {
            m_pageReplies.remove(reply); m_editorReplies.remove(reply);
            if(generation!=m_generation || (editor && editGeneration!=m_editorGeneration)) return;
            callback(response);
        });
    (editor?m_editorReplies:m_pageReplies).insert(reply);
}
void McpController::abort(bool editor)
{
    auto &replies=editor?m_editorReplies:m_pageReplies;
    const auto copy=replies; replies.clear();
    for(auto *reply:copy) reply->abort();
}
void McpController::missionRequest(const QString &path, std::function<void(const Response &)> callback)
{
    const auto generation = m_missionGeneration;
    const auto url = m_connection.baseUrl().resolved(QUrl("/api/v1/mcp" + path));
    auto *reply = JsonHttp::send(m_network, this, "GET", url, {}, 512 * 1024,
        [=, this](QNetworkReply *reply, const Response &response) {
            m_missionReplies.remove(reply);
            if (generation == m_missionGeneration) callback(response);
        });
    m_missionReplies.insert(reply);
}
void McpController::refreshMissionChoices()
{
    ++m_missionGeneration;
    const auto replies = m_missionReplies; m_missionReplies.clear();
    for (auto *reply : replies) reply->abort();
    m_missionChoices.clear(); m_missionError.clear(); m_missionLoading = true;
    emit missionChoicesChanged();
    missionRequest("/servers", [this](const Response &response) {
        if (!ok(response) || !response.body.value("items").isArray() || !response.body.value("disabled").isBool()) {
            m_missionLoading = false; m_missionError = failure(response); emit missionChoicesChanged(); return;
        }
        if (response.body.value("disabled").toBool()) {
            m_missionLoading = false; m_missionError = "后端已禁用 MCP；本次任务仍可使用文件检索。";
            emit missionChoicesChanged(); return;
        }
        QJsonArray servers;
        for (const auto &value : response.body.value("items").toArray()) {
            const auto server = value.toObject();
            if (Protocol::validTaskId(server.value("server_id").toString()) && server.value("enabled").toBool()
                && server.value("selected_tool_count").toInt() > 0) servers.append(server);
        }
        m_missionPending = servers.size();
        if (!m_missionPending) { m_missionLoading = false; emit missionChoicesChanged(); return; }
        for (const auto &value : servers) {
            const auto server = value.toObject();
            missionRequest("/servers/" + server.value("server_id").toString(), [this, server](const Response &detail) {
                if (!ok(detail)) m_missionError = failure(detail);
                else {
                    const auto body = detail.body;
                    const auto selected = body.value("selected_tools").toArray();
                    QSet<QString> compatible;
                    for (const auto &tool : body.value("tools").toArray()) {
                        const auto entry = tool.toObject();
                        const auto annotations = entry.value("annotations").toObject();
                        if (entry.value("compatible") == QJsonValue(true) && annotations.value("readOnlyHint") == QJsonValue(true)
                            && (!annotations.contains("destructiveHint") || annotations.value("destructiveHint") == QJsonValue(false)))
                            compatible.insert(entry.value("name").toString());
                    }
                    QStringList names;
                    for (const auto &tool : selected) if (tool.isString() && compatible.contains(tool.toString())) names.append(tool.toString());
                    if (body.value("server_id") == server.value("server_id") && body.value("revision") == server.value("revision")
                        && body.value("enabled").toBool() && body.value("effect") == "read"
                        && body.value("modes").toArray().contains("task") && !names.isEmpty() && names.size() == selected.size())
                        m_missionChoices.append(QVariantMap{{"server_id", body.value("server_id").toString()},
                            {"name", body.value("name").toString()}, {"tools", names}});
                }
                if (!--m_missionPending) m_missionLoading = false;
                emit missionChoicesChanged();
            });
        }
    });
}
void McpController::open() { if(m_open) return; m_open=true; refresh(); }
void McpController::close()
{
    if(!canCloseEditor()) return;
    ++m_generation; m_open=false; closeEditor(); abort(false);
    m_loading=false; m_busy=false; m_disabled=true; m_statusKnown=false;
    m_items.clear(); m_preset={}; m_error.clear(); m_notice.clear(); emit changed();
}
void McpController::refresh()
{
    if(!m_open || m_loading || m_busy) return;
    m_loading=true; m_error.clear(); emit changed();
    request(false,"GET","/servers",{},[this](const Response &r) {
        const bool valid=ok(r) && r.body.value("items").isArray() && r.body.value("disabled").isBool();
        if(!valid) {
            m_loading=false; m_disabled=true; m_statusKnown=false;
            m_error=ok(r)?"MCP 状态响应格式无效，请检查本机后端版本后刷新。":failure(r);
            emit changed(); return;
        }
        m_items=r.body.value("items").toArray().toVariantList(); m_disabled=r.body.value("disabled").toBool(true);
        m_statusKnown=true;
        request(false,"GET","/presets",{},[this](const Response &r) {
            m_loading=false;
            if(ok(r)) { const auto items=r.body.value("items").toArray(); m_preset=items.isEmpty()?QJsonObject{}:items.first().toObject(); }
            else m_error=failure(r);
            emit changed(); emit editorChanged();
        });
    });
}
void McpController::closeEditor()
{
    if(!canCloseEditor()) return;
    ++m_editorGeneration; m_editorOpen=false; abort(true); m_editorLoading=false;
    m_secrets={}; m_draft={}; m_original={}; m_tools={}; m_id.clear(); m_hash.clear(); m_revision=0;
    m_state="editing"; m_editorError.clear(); m_editorNotice.clear(); m_uncertain=false; m_recoveryConflict=false; m_hasDiscovered=false;
    emit clearSensitiveInputs(); emit editorChanged();
}
void McpController::edit(const QString &id)
{
    if(!m_open || !m_statusKnown || m_loading || m_busy || (!id.isEmpty() && !Protocol::validTaskId(id))) return;
    if(!canCloseEditor() || (id.isEmpty() && (m_disabled || m_pending))) return;
    closeEditor(); m_editorOpen=true;
    m_id=id.isEmpty()?QUuid::createUuid().toString(QUuid::WithoutBraces):id;
    m_draft={{"name","联网搜索"},{"transport","streamable_http"},{"connection",m_preset.value("connection")},
        {"modes",QJsonArray{"chat","task"}},{"effect","read"},{"selected_tools",QJsonArray{}},{"enabled",true}};
    if(id.isEmpty()) { if(m_preset.isEmpty()) setKind(1); emit editorChanged(); return; }
    m_editorLoading=true; emit editorChanged();
    request(true,"GET","/servers/"+m_id,{},[this](const Response &r) {
        m_editorLoading=false;
        if(ok(r)) loadDetail(r.body); else { m_editorError=failure(r); m_state="load_failed"; }
        emit editorChanged();
    });
}
void McpController::loadDetail(const QJsonObject &body)
{
    m_original=body; m_revision=body.value("revision").toInteger();
    m_draft={};
    for(const auto &key:{"name","transport","connection","modes","selected_tools","effect","enabled"}) m_draft[key]=body.value(key);
    if(m_draft.value("transport")=="stdio") {
        m_draft["modes"] = stdioModes(m_draft);
        auto c=m_draft.value("connection").toObject();
        if(!c.contains("public_env_names")) c["public_env_names"]=QJsonArray{};
        m_draft["connection"]=c;
    }
    m_tools=body.value("tools").toArray(); m_hash=body.value("catalog_hash").toString(); m_secrets={};
    m_state=m_hash.isEmpty()?"editing":"discovered"; m_hasDiscovered=!m_hash.isEmpty(); m_recoveryConflict=false;
    m_editorNotice="已加载保存时的工具，保存时会重新验证。"; emit clearSensitiveInputs();
}
int McpController::kind() const
{
    if(m_draft.value("transport")=="stdio") return 2;
    return m_draft.value("connection").toObject().value("preset_id")=="tavily"?0:1;
}
void McpController::invalidate()
{ m_tools={}; m_hash.clear(); m_state="editing"; m_editorError.clear(); m_editorNotice.clear(); }
void McpController::setKind(int value)
{
    if(!m_editorOpen || editorBusy() || m_disabled || m_uncertain || m_pending || m_recoveryConflict || m_state=="load_failed" || value<0 || value>2) return;
    if(value==0 && m_preset.isEmpty()) { m_editorError="搜索预设未加载，请刷新列表。"; emit editorChanged(); return; }
    m_draft["transport"]=value==2?"stdio":"streamable_http";
    if(value==2) m_draft["modes"] = stdioModes(m_draft);
    m_draft["connection"]=value==0?m_preset.value("connection").toObject():value==1?
        QJsonObject{{"endpoint",""},{"auth_type","none"},{"header_name",QJsonValue::Null},{"preset_id",QJsonValue::Null}}:
        QJsonObject{{"command",""},{"args",QJsonArray{}},{"cwd",QJsonValue::Null},{"env_names",QJsonArray{}},{"public_env_names",QJsonArray{}}};
    m_draft["selected_tools"]=QJsonArray{}; m_secrets={}; m_hasDiscovered=false; invalidate(); emit clearSensitiveInputs(); emit editorChanged();
}
void McpController::update(const QString &field,const QVariant &value)
{
    if(!m_editorOpen || editorBusy() || m_disabled || m_uncertain || m_pending || m_recoveryConflict || m_state=="load_failed") return;
    const auto json=QJsonValue::fromVariant(value);
    if(field=="name" || field=="modes") m_draft[field]=json;
    else {
        auto connection=m_draft.value("connection").toObject();
        const auto oldScope=credentialScope(m_draft);
        const auto oldNames=connection.value("env_names").toArray();
        const QStringList allowed=kind()==2?QStringList{"command","args","cwd","env_names","public_env_names"}:QStringList{"endpoint","auth_type","header_name"};
        if(kind()==0 || !allowed.contains(field) || connection.value(field)==json) return;
        connection[field]=json;
        if(field=="auth_type") {
            connection["header_name"]=QJsonValue::Null; m_secrets.remove("api_key"); emit clearSensitiveInputs();
        }
        if(field=="env_names") {
            const auto names=json.toArray();
            for(const auto &key:m_secrets.keys()) if(key.startsWith("env:") && !names.contains(key.mid(4))) m_secrets.remove(key);
            QJsonArray publicNames;
            for(const auto &name:oldNames) if(names.contains(name))
                for(const auto &declared:connection.value("public_env_names").toArray())
                    if(name.toString().compare(declared.toString(),Qt::CaseInsensitive)==0) { publicNames.append(name); break; }
            connection["public_env_names"]=publicNames;
        }
        m_draft["connection"]=connection; invalidate();
        if(oldScope!=credentialScope(m_draft)) { m_secrets.remove("api_key"); emit clearSensitiveInputs(); }
    }
    emit editorChanged();
}
bool McpController::hasSecret(const QString &key) const
{
    if(!m_original.value("has_secret").toBool()) return false;
    const auto connection=m_original.value("connection").toObject();
    if(key=="api_key") return m_original.value("transport")=="streamable_http" && connection.value("auth_type")!="none"
        && credentialScope(m_draft)==credentialScope(m_original);
    return m_draft.value("transport")=="stdio" && m_original.value("transport")=="stdio"
        && key.startsWith("env:") && connection.value("env_names").toArray().contains(key.mid(4));
}
QVariantMap McpController::credentialState() const
{
    QVariantMap result;
    if(hasSecret("api_key")) result["api_key"]="saved";
    for(const auto &name:m_original.value("connection").toObject().value("env_names").toArray())
        if(hasSecret("env:"+name.toString())) result["env:"+name.toString()]="saved";
    for(auto it=m_secrets.begin();it!=m_secrets.end();++it) result[it.key()]=it.value().isNull()?"cleared":"replacement";
    return result;
}
void McpController::setSecret(const QString &key,const QString &value)
{
    if(!m_editorOpen || editorBusy() || m_disabled || m_uncertain || m_pending || m_recoveryConflict || m_state=="load_failed" || value.size()>4096) return;
    if(value.isEmpty()) m_secrets.remove(key); else m_secrets[key]=value;
    invalidate(); emit editorChanged();
}
void McpController::clearSecret(const QString &key)
{
    if(!m_editorOpen || editorBusy() || m_disabled || m_uncertain || m_pending || m_recoveryConflict || m_state=="load_failed") return;
    if(key=="api_key" && kind()!=0) {
        auto connection=m_draft.value("connection").toObject(); connection["auth_type"]="none"; connection["header_name"]=QJsonValue::Null;
        m_draft["connection"]=connection;
    }
    if(hasSecret(key)) m_secrets[key]=QJsonValue::Null; else m_secrets.remove(key);
    invalidate(); emit clearSensitiveInputs(); emit editorChanged();
}
QString McpController::defaultDirectory(const QString &command) const
{ return command.isEmpty()?QString{}:QDir::toNativeSeparators(QFileInfo(command).absolutePath()); }
QString McpController::validation() const
{
    const auto name=m_draft.value("name").toString();
    if(name.trimmed().isEmpty() || name.size()>40) return "请填写 1–40 字的名称。";
    if(m_draft.value("modes").toArray().isEmpty()) return kind()==2 ? "请选择工作范围。" : "请至少选择问答或工作范围。";
    const auto c=m_draft.value("connection").toObject();
    QStringList required;
    if(kind()==2) {
        const QFileInfo file(c.value("command").toString());
        if(!file.isAbsolute() || file.suffix().toLower()!="exe" || !file.isFile()) return "请选择已安装的绝对 .exe 程序路径。";
        const auto cwd=c.value("cwd").toString();
        if(!cwd.isEmpty() && (!QFileInfo(cwd).isAbsolute() || !QFileInfo(cwd).isDir())) return "请选择存在的绝对工作目录。";
        if(c.value("args").toArray().size()>32) return "参数最多 32 项。";
        QSet<QString> names;
        for(const auto &entry:c.value("env_names").toArray()) {
            const auto name=entry.toString();
            if(!QRegularExpression("^[A-Za-z_][A-Za-z0-9_]{0,127}$").match(name).hasMatch() || names.contains(name.toUpper())) return "环境变量名称格式无效或重复。";
            names.insert(name.toUpper()); required.append("env:"+name);
        }
        if(names.size()>16) return "环境变量最多 16 项。";
    } else {
        const auto endpoint=c.value("endpoint").toString(); const QUrl url(endpoint,QUrl::StrictMode);
        const auto host=url.host().toLower();
        const bool loopback=host=="localhost" || host=="::1" || QHostAddress(host).isLoopback();
        if(endpoint.size()>2048 || !url.isValid() || host.isEmpty() || (url.scheme()!="https" && !(url.scheme()=="http" && loopback)) ||
           !url.userInfo().isEmpty() || url.hasQuery() || url.hasFragment()) return "请输入 HTTPS MCP 完整地址（本机可用 HTTP），不含凭证、查询参数或片段。";
        if(c.value("auth_type")=="api_key") {
            const auto header=c.value("header_name").toString(); const auto lower=header.toLower();
            if(!QRegularExpression("^[!#$%&'*+.^_`|~0-9A-Za-z-]+$").match(header).hasMatch() ||
               QStringList{"host","content-type","content-length","accept","accept-encoding","authorization","connection","transfer-encoding","cookie","origin","last-event-id","te","trailer","upgrade"}.contains(lower) ||
               lower.startsWith("mcp-") || lower.startsWith("proxy-") || lower.startsWith("sec-"))
                return "请填写有效的 API Key 请求头名称，不能覆盖协议或鉴权头。";
        }
        if(c.value("auth_type")!="none") required.append("api_key");
    }
    for(const auto &key:required) {
        if(m_secrets.contains(key) && m_secrets.value(key).isNull()) return "凭证已清除，请重新填写或移除对应鉴权/环境变量。";
        if(!m_secrets.contains(key) && !hasSecret(key)) {
            if(key=="api_key" && m_original.value("has_secret").toBool() && credentialScope(m_draft)!=credentialScope(m_original))
                return "连接地址或鉴权已改变，请重新填写密钥。";
            return "请填写所需密钥或环境变量值。";
        }
        const auto secret=m_secrets.value(key).toString();
        if(!secret.isEmpty() && QRegularExpression("^[*•●]+$").match(secret).hasMatch()) return "请输入真实凭证，不能提交掩码。";
    }
    return {};
}
QVariantList McpController::tools() const
{
    QVariantList result; const auto selected=m_draft.value("selected_tools").toArray();
    for(const auto &tool:m_tools) { auto entry=tool.toObject(); entry["selected"]=selected.contains(entry.value("name")); result.append(entry.toVariantMap()); }
    return result;
}
bool McpController::canSave() const
{ return m_editorOpen && !editorBusy() && !m_disabled && !m_uncertain && !m_pending && !m_recoveryConflict && m_state=="discovered"
    && (!m_draft.value("enabled").toBool() || !m_draft.value("selected_tools").toArray().isEmpty()) && validation().isEmpty(); }
void McpController::selectTool(const QString &name,bool selected)
{
    if(m_state!="discovered" || editorBusy() || m_disabled || m_uncertain || m_pending || m_recoveryConflict) return;
    bool allowed=false;
    for(const auto &entry:m_tools) if(entry.toObject().value("name")==name && entry.toObject().value("compatible").toBool()) allowed=true;
    if(!allowed) return;
    auto names=m_draft.value("selected_tools").toArray();
    if(selected && !names.contains(name)) names.append(name);
    if(!selected) for(int i=names.size()-1;i>=0;--i) if(names[i]==name) names.removeAt(i);
    m_draft["selected_tools"]=names; emit editorChanged();
}
void McpController::failEditor(const Response &r)
{
    const auto code=errorCode(r);
    const bool saving=m_state=="saving";
    if(code=="mcp_catalog_changed") invalidate();
    else m_state=saving && !m_hash.isEmpty()?"discovered":"editing";
    if(code=="mcp_config_conflict") { m_recoveryConflict=true; m_state="conflict"; }
    m_editorError=failure(r); emit editorChanged();
}
void McpController::probe()
{
    if(!m_editorOpen || editorBusy() || m_disabled || m_uncertain || m_pending || m_recoveryConflict || m_state=="load_failed") return;
    m_editorError=validation(); if(!m_editorError.isEmpty()) { emit editorChanged(); return; }
    auto body=m_draft; body["server_id"]=m_id; body["base_revision"]=m_revision; body["secret_updates"]=m_secrets;
    m_state="probing"; m_tools={}; m_hash.clear(); m_editorNotice.clear(); emit editorChanged();
    request(true,"POST","/probe",body,[this](const Response &r) {
        if(!ok(r)) { failEditor(r); return; }
        m_tools=r.body.value("tools").toArray(); m_hash=r.body.value("catalog_hash").toString(); m_draft["connection"]=r.body.value("connection");
        const auto prior=m_draft.value("selected_tools").toArray();
        const auto defaults=r.body.value("default_selected_tools").toArray(); QJsonArray selected;
        for(const auto &entry:m_tools) { const auto tool=entry.toObject();
            if(tool.value("compatible").toBool() && (m_revision>0 || m_hasDiscovered?prior:defaults).contains(tool.value("name"))) selected.append(tool.value("name")); }
        m_draft["selected_tools"]=selected; m_state="discovered"; m_hasDiscovered=true;
        m_editorNotice=QString("连接成功，发现 %1 个工具。仅验证握手与工具发现，尚未验证业务调用。").arg(m_tools.size()); emit editorChanged();
    });
}
void McpController::save()
{
    if(!canSave()) return;
    auto body=m_draft; body["base_revision"]=m_revision; body["catalog_hash"]=m_hash; body["secret_updates"]=m_secrets;
    m_pending=PendingSave{m_connection.baseUrl(),m_id,m_revision};
    m_state="saving"; m_editorError.clear(); emit editorChanged();
    request(true,"PUT","/servers/"+m_id,body,[this](const Response &r) {
        if(ok(r) && r.body.value("server_id")==m_id && r.body.value("revision").toInteger()>0) {
            m_pending.reset(); m_state="saved"; m_secrets={}; emit clearSensitiveInputs(); emit editorChanged();
            m_notice=r.body.value("enabled").toBool()?"配置已保存并启用。":"配置已保存，保持停用。"; emit saved(); refresh(); return;
        }
        // A transport/response failure can occur after commit. Never create a new ID.
        if(!r.exceededLimit && !r.invalidJson && r.status>=400 && errorMessages().contains(errorCode(r))) {
            m_pending.reset(); failEditor(r);
        } else { m_state="editing"; m_uncertain=true; resolveSave(); }
    });
}
void McpController::resolveSave()
{
    if(editorBusy() || m_resolving || !m_pending || m_pending->backend!=m_connection.baseUrl()) return;
    const auto pending=*m_pending;
    m_resolving=true; m_editorError.clear(); emit editorChanged();
    request(false,"GET","/servers/"+pending.serverId,{},[this,pending](const Response &r) {
        m_resolving=false;
        if(ok(r) && r.body.value("server_id")==pending.serverId && r.body.value("revision").toInteger()>0) {
            m_pending.reset(); m_uncertain=false;
            if(m_editorOpen && m_id==pending.serverId) loadDetail(r.body);
            m_notice=m_editorNotice="已读取同一服务的当前保存版本，请核对配置及密钥状态。";
            refresh();
        } else if(r.status==404 && errorCode(r)=="unknown_mcp_server") {
            m_pending.reset(); m_uncertain=false;
            if(m_editorOpen && m_id==pending.serverId) {
                invalidate();
                if(pending.baseRevision>0) { m_state="load_failed"; m_editorError="原有服务已删除，请关闭编辑并刷新列表。"; }
                else m_editorNotice="此 ID 尚无保存记录，可以修正后重新连接；重试仍使用原 ID。";
            }
            m_notice="已确认当前没有该服务配置。"; refresh();
        } else {
            m_notice="上次保存结果待确认。";
            m_editorError="无法确认保存状态，请检查连接后点击“确认保存状态”。";
        }
        emit changed(); emit editorChanged();
    });
}
QString McpController::toolReason(const QString &reason) const
{
    if(reason.startsWith("mcp_read_only")) return "工具声明会修改外部数据，暂不支持（mcp_read_only）";
    if(reason.startsWith("mcp_schema_unsupported")) return "工具参数结构暂不支持（mcp_schema_unsupported）";
    return "工具定义暂不兼容，请检查服务说明。";
}
void McpController::mutate(const QByteArray &method,const QString &path,const QJsonObject &body,const QString &notice)
{
    if(!m_open || !m_statusKnown || m_busy || m_loading) return;
    m_busy=true; m_error.clear(); m_notice.clear(); emit changed();
    request(false,method,path,body,[=,this](const Response &r) {
        m_busy=false;
        if(ok(r)) m_notice=notice; else m_error=failure(r);
        const auto error=m_error; refresh(); m_error=error; emit changed();
    });
}
void McpController::test(const QString &id,qint64 revision)
{ if(!m_disabled && Protocol::validTaskId(id)) mutate("POST","/servers/"+id+"/test",{{"revision",revision}},"最近连接检查成功；仅验证握手与工具发现。"); }
void McpController::setEnabled(const QString &id,qint64 revision,bool enabled)
{ if((!enabled || !m_disabled) && Protocol::validTaskId(id)) mutate("PATCH","/servers/"+id,{{"revision",revision},{"enabled",enabled}},enabled?"已启用，对新请求生效。":"已停用，对新请求生效。"); }
void McpController::remove(const QString &id,qint64 revision)
{ if(Protocol::validTaskId(id)) mutate("DELETE","/servers/"+id+"?revision="+QString::number(revision),{},"配置及凭证已删除，历史记录保留。"); }
