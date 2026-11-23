#include "SkillDraftClient.h"
#include "JsonHttp.h"
#include "MissionProtocol.h"
#include "Protocol.h"
#include "SseParser.h"
#include <QJsonArray>
#include <QNetworkProxy>
#include <QRegularExpression>
#include <QTimer>
#include <QUuid>
#include <utility>
#include <memory>

namespace {
bool validSkill(const QString &id)
{
    static const QRegularExpression pattern(R"(^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$)");
    return pattern.match(id).hasMatch();
}
bool validDraft(const QJsonObject &draft)
{
    const auto data = draft.value("data").toObject();
    const auto source = draft.value("source_mission_id");
    return MissionProtocol::validId(draft.value("draft_id").toString())
        && (source.isNull() || MissionProtocol::validId(source.toString()))
        && MissionProtocol::validRevision(draft.value("revision"))
        && data.value("generation_status").isString() && data.value("evidence_refs").isArray()
        && data.value("name").isString() && data.value("content").isObject();
}
bool validIdentity(const QJsonObject &skill)
{
    return validSkill(skill.value("skill_id").toString()) && skill.value("enabled").isBool()
        && MissionProtocol::validRevision(skill.value("revision"))
        && MissionProtocol::validRevision(skill.value("current_version"));
}
QString errorMessage(const QString &code, int status)
{
    if (code == "skill_api_unavailable") return QObject::tr("当前后端未提供技能编辑接口。");
    if (code == "skill_revision_conflict") return QObject::tr("修订冲突：请核对服务器上的最新版本，再合并本地编辑。");
    if (code == "skill_draft_incomplete") return QObject::tr("请补全名称、触发条件、适用阶段、正文和适用边界后收录。");
    if (code == "invalid_skill_evidence") return QObject::tr("依据与来源记录不符，请核对任务记录。");
    if (code == "skill_request_conflict") return QObject::tr("请求标识已用于其他内容，请重新找回草稿核对。");
    if (code == "mission_not_terminal") return QObject::tr("请在任务结束后提炼经验。");
    if (code == "skill_draft_not_found" || code == "skill_not_found") return QObject::tr("本机没有找到对应的草稿或技能版本。");
    return MissionProtocol::errorMessage(code, status);
}
}

SkillDraftClient::SkillDraftClient(QObject *parent) : QObject(parent)
{
    m_network.setProxy(QNetworkProxy::NoProxy);
}
SkillDraftClient::~SkillDraftClient() { abortPending(); }
void SkillDraftClient::abortPending()
{
    ++m_generation;
    const auto pending = std::exchange(m_pending, {});
    for (auto *reply : pending) {
        reply->disconnect(this);
        reply->abort();
        reply->deleteLater();
    }
}
void SkillDraftClient::setContext(const QUrl &backend, const QString &task)
{
    if (backend == m_backend && task == m_task) return;
    abortPending();
    m_backend = backend;
    m_task = task;
}
QString SkillDraftClient::newRequestId() const { return QUuid::createUuid().toString(QUuid::WithoutBraces); }
quint64 SkillDraftClient::reject()
{
    const auto serial = ++m_serial;
    const auto generation = m_generation;
    QTimer::singleShot(0, this, [this, serial, generation] {
        if (generation == m_generation)
            emit failed(serial, 0, "invalid_request", tr("技能请求无效，请检查来源、版本和必填内容。"));
    });
    return serial;
}
quint64 SkillDraftClient::listDrafts(int offset)
{
    if (offset < 0 || !MissionProtocol::validId(m_task)) return reject();
    return request("GET", "/skill-drafts?source_mission_id=" + m_task + "&limit=20&offset=" + QString::number(offset),
                   Result::MissionDraftPage);
}
quint64 SkillDraftClient::listAllDrafts(int offset)
{
    if (offset < 0) return reject();
    return request("GET", "/skill-drafts?limit=20&offset=" + QString::number(offset), Result::DraftPage);
}
quint64 SkillDraftClient::listSkills(int offset)
{
    if (offset < 0) return reject();
    return request("GET", "/skills?limit=20&offset=" + QString::number(offset), Result::SkillPage);
}
quint64 SkillDraftClient::createDraft(const QString &note, const QString &mode, const QString &requestId)
{
    if (!MissionProtocol::validId(m_task) || (mode != "model" && mode != "manual") || requestId.isEmpty()) return reject();
    return request("POST", "/missions/" + m_task + "/skill-drafts", Result::MissionDraft,
                   {{"developer_note", note}, {"generation_mode", mode}, {"request_id", requestId}});
}
quint64 SkillDraftClient::getDraft(const QString &id)
{
    if (!MissionProtocol::validId(id)) return reject();
    return request("GET", "/skill-drafts/" + id, Result::Draft, {}, id);
}
quint64 SkillDraftClient::getSourceEvent(const QString &eventId)
{
    const auto sequence = Protocol::sequence(eventId, m_task);
    if (!MissionProtocol::validId(m_task) || sequence < 1 || !Protocol::validBaseUrl(m_backend)) return reject();
    // Replay from the record immediately before this reference. Do not advance
    // the live workbench's cursor or apply historical events to its models.
    QNetworkRequest lookup(MissionProtocol::endpoint(m_backend, '/' + m_task + "/events"));
    JsonHttp::authenticate(lookup);
    lookup.setRawHeader("Accept", "text/event-stream");
    lookup.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    if (sequence > 1) lookup.setRawHeader("Last-Event-ID", (m_task + ':' + QString::number(sequence - 1)).toUtf8());
    lookup.setTransferTimeout(30000);
    const auto serial = ++m_serial;
    auto *reply = m_network.get(lookup);
    m_pending.insert(reply);
    reply->setReadBufferSize(128 * 1024);
    const auto finish = [this, reply, serial](const QString &code, const QJsonObject &event = {}) {
        if (!m_pending.remove(reply)) return;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        reply->disconnect(this);
        if (!reply->isFinished()) reply->abort();
        reply->deleteLater();
        if (code.isEmpty()) emit succeeded(serial, event.toVariantMap());
        else emit failed(serial, status, code, tr("未能定位对应的本机任务事件。"));
    };
    const auto parser = std::make_shared<SseParser>(128 * 1024);
    connect(reply, &QNetworkReply::readyRead, this, [this, eventId, parser, reply, finish] {
        if (reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt() != 200
            || !reply->header(QNetworkRequest::ContentTypeHeader).toString().startsWith("text/event-stream")) {
            finish("invalid_response");
            return;
        }
        const auto frames = parser->feed(reply->readAll());
        for (const auto &frame : frames) {
            if (frame.retry >= 0) continue;
            QJsonObject event;
            if (frame.id == eventId && JsonHttp::parseStrictObject(frame.data, event)
                && event.value("event_id") == eventId && MissionProtocol::validEvent(event, frame.event, m_task))
                finish({}, event);
            else finish("invalid_response");
            return;
        }
        if (!parser->error().isEmpty()) finish("invalid_response");
    });
    connect(reply, &QNetworkReply::finished, this, [finish] { finish("skill_event_unavailable"); });
    QTimer::singleShot(30000, reply, [reply] { if (!reply->isFinished()) reply->abort(); });
    return serial;
}
quint64 SkillDraftClient::saveDraft(const QString &id, qint64 revision, const QVariantMap &changes)
{
    if (!MissionProtocol::validId(id) || revision < 1 || changes.isEmpty()) return reject();
    auto body = QJsonObject::fromVariantMap(changes);
    body.insert("expected_revision", revision);
    return request("PATCH", "/skill-drafts/" + id, Result::Draft, body, id);
}
quint64 SkillDraftClient::regenerate(const QString &id, qint64 revision, const QString &requestId)
{
    if (!MissionProtocol::validId(id) || revision < 1 || requestId.isEmpty()) return reject();
    return request("POST", "/skill-drafts/" + id + "/regenerate", Result::Draft,
                   {{"expected_revision", revision}, {"request_id", requestId}}, id);
}
quint64 SkillDraftClient::publish(const QString &id, qint64 revision, const QString &requestId)
{
    if (!MissionProtocol::validId(id) || revision < 1 || requestId.isEmpty()) return reject();
    return request("POST", "/skill-drafts/" + id + "/publish-local", Result::Version,
                   {{"expected_revision", revision}, {"request_id", requestId}});
}
quint64 SkillDraftClient::getVersion(const QString &id, qint64 version)
{
    if (!validSkill(id) || version < 1) return reject();
    return request("GET", "/skills/" + id + "/versions/" + QString::number(version), Result::Version, {}, id, version);
}
quint64 SkillDraftClient::revise(const QString &id, qint64 version, const QString &requestId)
{
    if (!validSkill(id) || version < 1 || requestId.isEmpty()) return reject();
    return request("POST", "/skills/" + id + "/drafts", Result::RevisionDraft,
                   {{"version", version}, {"request_id", requestId}}, id, version);
}
quint64 SkillDraftClient::disable(const QString &id, qint64 revision)
{
    if (!validSkill(id) || revision < 1) return reject();
    return request("PATCH", "/skills/" + id, Result::Identity, {{"expected_revision", revision}, {"enabled", false}}, id);
}

bool SkillDraftClient::matches(Result result, const QJsonObject &o, const QString &id, qint64 version) const
{
    switch (result) {
    case Result::MissionDraft:
        return validDraft(o) && o.value("source_mission_id") == m_task;
    case Result::Draft:
        return validDraft(o) && o.value("draft_id") == id
            && (o.value("source_mission_id").isNull() || o.value("source_mission_id") == m_task);
    case Result::RevisionDraft:
        return validDraft(o) && o.value("base_skill_id") == id && o.value("base_version").toInteger() == version;
    case Result::Identity:
        return validIdentity(o) && o.value("skill_id") == id && !o.value("enabled").toBool();
    case Result::Version:
        return validSkill(o.value("skill_id").toString()) && MissionProtocol::validRevision(o.value("version"))
            && MissionProtocol::validHash(o.value("content_sha256")) && o.value("name").isString()
            && o.value("content").isObject() && (id.isEmpty() || (o.value("skill_id") == id && o.value("version").toInteger() == version));
    case Result::SkillPage:
    case Result::MissionDraftPage:
    case Result::DraftPage: {
        const auto items = o.value(result == Result::SkillPage ? "skills" : "drafts");
        const auto next = o.value("next_offset");
        if (!items.isArray() || (!next.isNull() && !MissionProtocol::validRevision(next))) return false;
        for (const auto &entry : items.toArray()) {
            const auto row = entry.toObject();
            if (result == Result::SkillPage) {
                if (!validIdentity(row) || !row.value("name").isString()) return false;
            } else if (!validDraft(row) || (result == Result::MissionDraftPage && row.value("source_mission_id") != m_task)) return false;
        }
        return true;
    }
    }
    return false;
}
quint64 SkillDraftClient::request(const QByteArray &method, const QString &route, Result result,
                                 const QJsonObject &body, const QString &recordId, qint64 version)
{
    if (!Protocol::validBaseUrl(m_backend)) return reject();
    const auto serial = ++m_serial;
    const auto url = m_backend.resolved(QUrl("/api/v2" + route));
    auto *reply = JsonHttp::send(m_network, this, method, url, body, 64 * 1024 * 1024,
        [=, this](QNetworkReply *finished, const JsonHttp::Response &response) {
            if (!m_pending.remove(finished)) return;
            const int status = response.status;
            QString code;
            if (response.exceededLimit) code = "response_limit";
            else if (!status) code = "network_error";
            else if (status < 200 || status >= 300) {
                const bool typed = MissionProtocol::validError(response.body, m_task);
                if (typed) code = response.body.value("error").toObject().value("code").toString();
                if (status == 405 || (status == 404 && (!typed || code == "route_not_found"))) code = "skill_api_unavailable";
                else if (!typed) code = "invalid_response";
            } else if (response.networkError != QNetworkReply::NoError) code = "network_error";
            else if (response.invalidJson || !matches(result, response.body, recordId, version)) code = "invalid_response";
            if (code.isEmpty()) emit succeeded(serial, response.body.toVariantMap());
            else emit failed(serial, status, code, errorMessage(code, status));
        }, method == "GET", true);
    m_pending.insert(reply);
    return serial;
}
