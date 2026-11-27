#include "AgentApiClient.h"
#include "JsonHttp.h"
#include "workbench/MissionProtocol.h"
#include "workbench/SkillDraftClient.h"
#include <QFile>
#include <QJsonDocument>
#include <QtTest>
#include <QUrlQuery>
#include "HttpFixture.h"

namespace {
QJsonObject example(const QString &name) {
    QFile file(QFINDTESTDATA("../../protocol/examples.json"));
    if (!file.open(QIODevice::ReadOnly)) qFatal("Missing canonical protocol examples");
    return QJsonDocument::fromJson(file.readAll()).object().value(name).toObject();
}
QJsonObject mission() { return example("mission_query_response"); }
QJsonObject artifact() {
    const QJsonObject item{{"evidence_id", "EV-1"}, {"claim", "Worker is still active"}, {"path", QJsonValue::Null},
        {"start_line", QJsonValue::Null}, {"end_line", QJsonValue::Null}, {"file_hash", QJsonValue::Null},
        {"symbol", QJsonValue::Null}, {"source", "local_repository"}, {"confidence", 0.8}, {"reason", "Trace"}};
    return {{"artifact_id", B}, {"task_id", A}, {"artifact_type", "EvidenceBundle"}, {"schema_version", "1.0"},
        {"producer", "analyst.v1"}, {"producer_node", "analysis"}, {"created_at", "2026-10-27T08:00:00Z"},
        {"parent_artifact_ids", QJsonArray{}}, {"content_sha256", QString(64, 'a')},
        {"content", QJsonObject{{"summary", "Evidence"}, {"items", QJsonArray{item}}}}};
}
QJsonObject futureArtifact() {
    auto record = artifact();
    record["artifact_type"] = "FutureAnalysis";
    record["content"] = QJsonObject{{"observations", QJsonArray{"example"}}};
    return record;
}
QJsonObject graph() {
    const QJsonObject node{{"node_id", "analysis"}, {"node_type", "agent"}, {"status", "running"}, {"attempt", 1}, {"result", QJsonValue::Null}};
    const QJsonObject definition{{"node_id", "analysis"}, {"node_type", "agent"}, {"handler", "analysis"},
        {"dependencies", QJsonArray{}}, {"agent_id", "analyst.v1"}, {"agent_definition_hash", QJsonValue::Null}, {"settings", QJsonObject{}}};
    return {{"task_id", A}, {"graph_id", "analysis.v1"}, {"graph_version", 1}, {"definition_hash", QString(64, 'a')},
        {"status", "running"}, {"current_revision", 8}, {"nodes", QJsonArray{node}},
        {"definition", QJsonObject{{"graph_id", "analysis.v1"}, {"version", 1}, {"nodes", QJsonArray{definition}}, {"edges", QJsonArray{}}}}};
}
QJsonObject approval() {
    return {{"expected_revision", 8}, {"approval_type", "execution"}, {"task_spec_hash", QString(64, 'a')},
        {"plan_hash", QString(64, 'b')}, {"scope_hash", QString(64, 'c')}, {"decision", "approved"}};
}
void raw(const Request &r, const QByteArray &bytes, int status = 200) {
    if (!r.socket) return;
    r.socket->write("HTTP/1.1 " + QByteArray::number(status) + " Result\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
        + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
    r.socket->disconnectFromHost();
}
}

class MissionApiTests : public QObject {
    Q_OBJECT
private slots:
    void skillDraftEditingContract() {
        Source source;
        SkillDraftClient api;
        api.setContext(source.url(), A);
        QSignalSpy ok(&api, &SkillDraftClient::succeeded), errors(&api, &SkillDraftClient::failed);
        const auto draft = example("skill_draft_create_response");
        const auto id = draft.value("draft_id").toString();
        const auto skill = example("skill_version_detail_response").value("skill_id").toString();
        const QByteArray draftPath = "/api/v2/skill-drafts/" + id.toUtf8();
        const QByteArray skillPath = "/api/v2/skills/" + skill.toUtf8();
        const auto save = example("skill_draft_save_request");
        auto changes = save.toVariantMap();
        changes.remove("expected_revision");
        auto check = [&](quint64 serial, const QByteArray &method, const QByteArray &path,
                         const QJsonObject &body, const QString &response, int status = 200) {
            QTRY_COMPARE_WITH_TIMEOUT(source.requests.size(), 1, 3000);
            const auto call = source.requests.takeFirst();
            QCOMPARE(call.path, path);
            QCOMPARE(call.method, method);
            QCOMPARE(call.body, body);
            const auto reply = example(response);
            Source::json(call, reply, status);
            QTRY_COMPARE_WITH_TIMEOUT(ok.size(), 1, 3000);
            const auto args = ok.takeFirst();
            QCOMPARE(args.at(0).toULongLong(), serial);
            QCOMPARE(args.at(1).toMap(), reply.toVariantMap());
        };
        const auto key = api.newRequestId();
        QVERIFY(key != api.newRequestId());
        check(api.listDrafts(), "GET", "/api/v2/skill-drafts?source_mission_id=" + A.toUtf8() + "&limit=20&offset=0",
              {}, "skill_draft_page_response");
        check(api.listAllDrafts(20), "GET", "/api/v2/skill-drafts?limit=20&offset=20", {}, "skill_draft_page_response");
        check(api.createDraft("手记", "manual", key), "POST", "/api/v2/missions/" + A.toUtf8() + "/skill-drafts",
              {{"developer_note", "手记"}, {"generation_mode", "manual"}, {"request_id", key}}, "skill_draft_create_response", 201);
        check(api.getDraft(id), "GET", draftPath, {}, "skill_draft_detail_response");
        check(api.saveDraft(id, 1, changes), "PATCH", draftPath, save, "skill_draft_save_response");
        check(api.regenerate(id, 2, key), "POST", draftPath + "/regenerate", {{"expected_revision", 2}, {"request_id", key}},
              "skill_draft_create_response", 202);
        check(api.publish(id, 2, key), "POST", draftPath + "/publish-local",
              {{"expected_revision", 2}, {"request_id", key}}, "skill_publish_local_response");
        check(api.listSkills(), "GET", "/api/v2/skills?limit=20&offset=0", {}, "skill_page_response");
        check(api.getVersion(skill, 1), "GET", skillPath + "/versions/1", {}, "skill_version_detail_response");
        check(api.revise(skill, 1, key), "POST", skillPath + "/drafts",
              {{"version", 1}, {"request_id", key}}, "skill_revision_draft_response", 201);
        check(api.disable(skill, 1), "PATCH", skillPath, example("skill_disable_request"), "skill_disable_response");
        QVERIFY(errors.isEmpty());

        api.saveDraft(id, 1, changes);
        QTRY_COMPARE(source.requests.size(), 1);
        Source::json(source.requests.takeFirst(), example("skill_save_conflict_error"), 409);
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.takeFirst().at(2).toString(), "skill_revision_conflict");
        api.listDrafts();
        QTRY_COMPARE(source.requests.size(), 1);
        Source::json(source.requests.takeFirst(), {{"detail", "Not Found"}}, 404);
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.takeFirst().at(2).toString(), "skill_api_unavailable");
        api.listDrafts();
        QTRY_COMPARE(source.requests.size(), 1);
        Source::json(source.requests.takeFirst(), {{"error", QJsonObject{{"code", "route_not_found"}, {"message", "Not Found"},
            {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}}, 404);
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.takeFirst().at(2).toString(), "skill_api_unavailable");
        QVERIFY(ok.isEmpty());
    }
    void skillDraftIsolation() {
        Source first, second;
        SkillDraftClient api;
        api.setContext(first.url(), A);
        QSignalSpy ok(&api, &SkillDraftClient::succeeded), errors(&api, &SkillDraftClient::failed);
        const auto page = example("skill_draft_page_response");
        const auto emptyPage = QJsonObject{{"drafts", QJsonArray{}}, {"next_offset", QJsonValue::Null}};
        const auto windowOne = api.listDrafts();
        QTRY_COMPARE(first.requests.size(), 1);
        const auto windowTwo = api.listDrafts();
        QVERIFY(windowTwo != windowOne);
        QTRY_COMPARE(first.requests.size(), 2);
        Source::json(first.requests.last(), emptyPage);
        QTRY_COMPARE(ok.size(), 1);
        QCOMPARE(ok.last()[0].toULongLong(), windowTwo);
        Source::json(first.requests.first(), page);
        QTRY_COMPARE(ok.size(), 2);
        QCOMPARE(ok.last()[0].toULongLong(), windowOne);
        first.requests.clear();
        ok.clear();

        api.listDrafts();
        QTRY_COMPARE(first.requests.size(), 1);
        api.setContext(first.url(), B);
        Source::json(first.requests.takeFirst(), page);
        api.listDrafts();
        QTRY_COMPARE(first.requests.size(), 1);
        api.setContext(second.url(), A);
        Source::json(first.requests.takeFirst(), emptyPage);
        const auto current = api.listDrafts();
        QTRY_COMPARE(second.requests.size(), 1);
        Source::json(second.requests.takeFirst(), page);
        QTRY_COMPARE(ok.size(), 1);
        QCOMPARE(ok.first()[0].toULongLong(), current);
        QVERIFY(errors.isEmpty());

        api.setContext(second.url(), B);
        api.listDrafts();
        QTRY_COMPARE(second.requests.size(), 1);
        Source::json(second.requests.takeFirst(), page); // A's records in B's list.
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.first()[2].toString(), "invalid_response");
        QCOMPARE(ok.size(), 1);
    }
    void skillDraftHistoricalEvent() {
        Source source;
        SkillDraftClient api;
        api.setContext(source.url(), A);
        QSignalSpy ok(&api, &SkillDraftClient::succeeded), errors(&api, &SkillDraftClient::failed);
        const auto id = A + ":9";
        const auto serial = api.getSourceEvent(id);
        QTRY_COMPARE(source.requests.size(), 1);
        const auto call = source.requests.takeFirst();
        QCOMPARE(call.path, "/api/v2/missions/" + A.toUtf8() + "/events");
        QVERIFY(call.headers.toLower().contains("last-event-id: " + A.toUtf8() + ":8"));
        QJsonObject event{{"event_id", id}, {"task_id", A}, {"task_revision", 11}, {"event_type", "task.completed"},
            {"timestamp", "2026-10-27T08:00:00Z"}, {"actor", QJsonValue::Null}, {"node_id", QJsonValue::Null},
            {"data", QJsonObject{}}};
        Source::stream(call, "retry: 1000\r\n\r\nid: " + id.toUtf8() + "\r\nevent: task.completed\r\ndata: "
            + QJsonDocument(event).toJson(QJsonDocument::Compact) + "\r\n\r\n", false);
        QTRY_COMPARE(ok.size(), 1);
        QCOMPARE(ok.first().at(0).toULongLong(), serial);
        QCOMPARE(ok.first().at(1).toMap(), event.toVariantMap());
        api.getSourceEvent(B + ":9");
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.first().at(2).toString(), "invalid_request");
        QVERIFY(source.requests.isEmpty());
    }
    void skillRecommendationContract() {
        const QString op = "mission.recommendations";
        const auto request = example("skill_recommendations_request");
        QVERIFY(MissionProtocol::validRequest(op, request));
        QVERIFY(MissionProtocol::validRequest(op, example("skill_recommendations_unmatched_request")));
        auto invalid = request;
        invalid["repository_path"] = "D:/fixture";
        QVERIFY(!MissionProtocol::validRequest(op, invalid));
        invalid = request; invalid["task_type"] = "analysis";
        QVERIFY(!MissionProtocol::validRequest(op, invalid));
        for (const QString &value : {QString(" "), QString(8193, 'x'), QString(QChar::Null)}) {
            invalid = request; invalid["description"] = value;
            QVERIFY(!MissionProtocol::validRequest(op, invalid));
        }
        invalid = request; invalid["project_tags"] = QJsonArray{QString(257, 'x')};
        QVERIFY(!MissionProtocol::validRequest(op, invalid));
        const auto response = example("skill_recommendations_response");
        QVERIFY(MissionProtocol::validResponse(op, {}, response));
        QVERIFY(MissionProtocol::validResponse(op, {}, example("skill_recommendations_empty_response")));
        const auto candidate = response.value("recommendations").toArray().first().toObject();
        for (const auto &key : {"skill_id", "version", "content_sha256", "name", "summary", "reasons"}) {
            auto broken = candidate;
            broken.remove(key);
            QVERIFY2(!MissionProtocol::validResponse(op, {}, {{"recommendations", QJsonArray{broken}}}), key);
        }
        QVERIFY(!MissionProtocol::validResponse(op, {}, {{"recommendations", QJsonArray{candidate, candidate}}}));
        auto broken = candidate;
        broken["reasons"] = QJsonArray{};
        QVERIFY(!MissionProtocol::validResponse(op, {}, {{"recommendations", QJsonArray{broken}}}));
        broken = candidate; broken["version"] = "1";
        QVERIFY(!MissionProtocol::validResponse(op, {}, {{"recommendations", QJsonArray{broken}}}));
        broken = candidate; broken["content_sha256"] = QString(64, 'g');
        QVERIFY(!MissionProtocol::validResponse(op, {}, {{"recommendations", QJsonArray{broken}}}));
    }
    void skillRecommendationAndCreatePostBodies() {
        const auto request = example("skill_recommendations_request");
        const auto response = example("skill_recommendations_response");
        const auto withSkills = example("mission_create_with_skills_request");
        Source source;
        source.handle = [&](const Request &r) {
            if (r.path == "/api/v2/skills/recommendations") Source::json(r, response);
            else if (r.body.contains("selected_skills")) Source::json(r, example("mission_skill_selection_changed_error"), 409);
            else Source::json(r, mission(), 202);
        };
        AgentApiClient api;
        api.setBaseUrl(source.url());
        QSignalSpy received(&api, &AgentApiClient::responseReceived), errors(&api, &AgentApiClient::requestError);
        const auto serial = api.recommendSkills(request);
        QTRY_COMPARE(received.size(), 1);
        QCOMPARE(received.first()[0].toULongLong(), serial);
        QCOMPARE(received.first()[1].toString(), "mission.recommendations");
        QCOMPARE(received.first()[3].toJsonObject(), response);
        QCOMPARE(source.requests.first().method, "POST");
        QCOMPARE(source.requests.first().path, "/api/v2/skills/recommendations");
        QCOMPARE(source.requests.first().body, request);
        api.createMission(withSkills);
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.first()[3].toInt(), 409);
        QCOMPARE(errors.first()[4].toString(), "skill_selection_changed");
        QCOMPARE(source.requests.last().method, "POST");
        QCOMPARE(source.requests.last().path, "/api/v2/missions");
        QCOMPARE(source.requests.last().body, withSkills);
        auto empty = withSkills;
        empty["selected_skills"] = QJsonArray{};
        api.createMission(empty);
        QTRY_COMPARE(received.size(), 2);
        empty.remove("selected_skills");
        QCOMPARE(source.requests.last().body, empty);
        QCOMPARE(source.requests.size(), 3);
    }
    void skillStagesAndSelectionLimitMatchExportedOpenApi() {
        QFile file(QFINDTESTDATA("../../protocol/openapi.json"));
        QVERIFY(file.open(QIODevice::ReadOnly));
        const auto schemas = QJsonDocument::fromJson(file.readAll()).object()["components"].toObject()["schemas"].toObject();
        const auto properties = [&](const QString &name) { return schemas.value(name).toObject()["properties"].toObject(); };
        const auto stages = QJsonArray::fromStringList(MissionProtocol::selectableSkillStages());
        QVERIFY(!stages.isEmpty());
        for (const auto &name : {"SkillDraftContent", "SkillContent"}) {
            const auto field = properties(name).value("stages").toObject();
            QCOMPARE(field.value("items").toObject().value("enum").toArray(), stages);
            QCOMPARE(field.value("maxItems").toInt(-1), stages.size());
        }
        QCOMPARE(properties("SkillInjection").value("node_id").toObject().value("enum").toArray(), stages);
        QCOMPARE(properties("CreateMission").value("selected_skills").toObject().value("maxItems").toInt(-1),
                 MissionProtocol::MaxSelectedSkills);
    }
    void reviewedSkillSelectionAndInjectionHistory() {
        const auto request = example("mission_create_with_skills_request");
        const auto history = example("mission_skills_response");
        const auto selected = request.value("selected_skills").toArray();
        QVERIFY(MissionProtocol::validRequest("mission.create", request));
        auto body = request;
        auto three = history.value("selected_skills").toArray();
        auto third = selected.first().toObject();
        third["skill_id"] = "third";
        three.append(third);
        body["selected_skills"] = three;
        QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        body["selected_skills"] = QJsonArray{selected.first(), selected.first()};
        QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        auto extra = selected.first().toObject();
        extra["body"] = "private";
        body["selected_skills"] = QJsonArray{extra};
        QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        QVERIFY(MissionProtocol::validResponse("mission.skills", A, history));
        auto invalid = history;
        invalid["selected_skills"] = QJsonArray{};
        QVERIFY(!MissionProtocol::validResponse("mission.skills", A, invalid));

        Source source;
        source.handle = [history](const Request &r) { Source::json(r, history); };
        AgentApiClient api;
        api.setBaseUrl(source.url());
        QSignalSpy responses(&api, &AgentApiClient::responseReceived);
        api.getMissionSkills(A);
        QTRY_COMPARE(responses.size(), 1);
        QCOMPARE(source.requests.first().path, "/api/v2/missions/" + A.toUtf8() + "/skills");
        QCOMPARE(source.requests.first().method, "GET");
    }
    void graphActionWireValues() {
        auto validNode = [](const QJsonObject &node) {
            auto body = graph();
            auto definition = body.value("definition").toObject();
            auto declared = definition.value("nodes").toArray().first().toObject();
            declared["node_id"] = node.value("node_id");
            declared["node_type"] = node.value("node_type");
            definition["nodes"] = QJsonArray{declared};
            body["definition"] = definition;
            body["nodes"] = QJsonArray{node};
            body["status"] = node.value("status");
            return MissionProtocol::validGraph(body);
        };
        for (const auto name : {"mission_execution_pending_node", "mission_scope_pending_node",
                                "mission_missing_current_evidence_node", "mission_retryable_node", "mission_interrupted_node"})
            QVERIFY2(validNode(example(name)), name);

        auto node = example("mission_scope_pending_node");
        node.remove("pending_action");
        QVERIFY(validNode(node));
        node["pending_action"] = "retry";
        QVERIFY(!validNode(node));
        node = example("mission_retryable_node");
        node.remove("retry_actions");
        QVERIFY(validNode(node));
        for (const QJsonValue value : {QJsonValue(QJsonValue::Null), QJsonValue("retry"),
                QJsonValue(QJsonArray{"approve"}), QJsonValue(QJsonArray{"retry", "retry"})}) {
            node["retry_actions"] = value;
            QVERIFY(!validNode(node));
        }
    }
    void scopeExpansionPreviewAndDecision() {
        const auto preview = example("mission_scope_expansion_preview");
        const auto decision = example("mission_scope_approval_request");
        const QString op = "mission.scope-expansion";
        QVERIFY(MissionProtocol::validResponse(op, A, preview));
        QVERIFY(!MissionProtocol::validResponse(op, B, preview));
        for (const auto &key : {"attempt", "revision", "partial_patch", "partial_change_set_artifact_id",
                                "task_spec_hash", "scope_hash", "plan_hash", "checkpoint_id", "files"}) {
            auto incomplete = preview;
            incomplete.remove(key);
            QVERIFY2(!MissionProtocol::validResponse(op, A, incomplete), key);
        }
        auto malformed = preview;
        auto file = preview.value("files").toArray().first().toObject();
        file.remove("reason");
        malformed["files"] = QJsonArray{file};
        QVERIFY(!MissionProtocol::validResponse(op, A, malformed));

        Source source;
        source.handle = [&](const Request &r) {
            if (r.method == "GET") Source::json(r, preview);
            else Source::json(r, mission(), 202);
        };
        AgentApiClient api;
        api.setBaseUrl(source.url());
        QSignalSpy responses(&api, &AgentApiClient::responseReceived);
        api.getMissionScopeExpansion(A);
        QTRY_COMPARE(responses.size(), 1);
        QCOMPARE(source.requests.first().path, "/api/v2/missions/" + A.toUtf8() + "/scope-expansion");
        QCOMPARE(source.requests.first().method, "GET");
        api.approveMission(A, decision);
        QTRY_COMPARE(responses.size(), 2);
        QCOMPARE(source.requests.last().path, "/api/v2/missions/" + A.toUtf8() + "/approvals");
        QCOMPARE(source.requests.last().method, "POST");
        QCOMPARE(source.requests.last().body, decision);
    }
    void missionToolSelectionIsValidatedAndSent() {
        const QJsonObject request{{"repository_path", "D:/fixture"}, {"request", "Use selected tools"},
            {"mcp_server_ids", QJsonArray{B}}, {"use_rag", true}};
        QVERIFY(MissionProtocol::validRequest("mission.create", request));
        auto body = request; body["mcp_server_ids"] = QJsonArray{B, B}; QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        body = request; body["mcp_server_ids"] = QJsonArray{"invalid"}; QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        body = request; body["mcp_server_ids"] = B; QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        body = request; body["use_rag"] = "true"; QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        body = request; body["mcp_server_ids"] = QJsonArray{A, B, "10000000-0000-4000-a000-000000000003", "10000000-0000-4000-a000-000000000004", "10000000-0000-4000-a000-000000000005"};
        QVERIFY(!MissionProtocol::validRequest("mission.create", body));
        Source source; source.handle = [](const Request &r) { Source::json(r, mission(), 202); };
        AgentApiClient api; api.setBaseUrl(source.url()); QSignalSpy errors(&api, &AgentApiClient::requestError), responses(&api, &AgentApiClient::responseReceived);
        api.createMission(body); QTRY_COMPARE(errors.size(), 1); QCOMPARE(source.requests.size(), 0);
        api.createMission(request); QTRY_COMPARE(responses.size(), 1); QCOMPARE(source.requests.first().body, request);
        const QJsonObject state{{"enabled", true}, {"used", false}, {"fallback_reasons", QJsonArray{}}};
        auto rag = state; rag["requested"] = true;
        const QJsonObject context{{"rag", rag}, {"clangd", state}, {"mcp", QJsonObject{{"used", false},
            {"fallback_reasons", QJsonArray{}}, {"server_ids", QJsonArray{B}},
            {"servers", QJsonArray{QJsonObject{{"server_id", B}, {"name", "Fixture"}, {"tools", QJsonArray{"lookup"}}}}}}}};
        auto response = mission(); response["tool_context"] = context; QVERIFY(MissionProtocol::validMission(response));
        auto malformed = context; rag["used"] = "true"; malformed["rag"] = rag;
        response["tool_context"] = malformed; QVERIFY(!MissionProtocol::validMission(response));
    }
    void creationPreviewShowsConcretePresetFailureAndSupportsDisposal() {
        const QJsonObject preview{{"preview_token", QString(64, 'c')}, {"base_commit", QString(40, 'a')},
            {"source_head", QString(40, 'a')}, {"baseline_paths", QJsonArray{}}, {"changed_files", QJsonArray{"main.cpp"}},
            {"patch", ""}, {"build", QJsonObject{{"source", "default"}}}};
        QVERIFY(MissionProtocol::validResponse("mission.preview", {}, preview));
        auto bad=preview; bad["changed_files"]=QJsonArray{1}; QVERIFY(!MissionProtocol::validResponse("mission.preview", {}, bad));
        Source source; source.handle=[&](const Request &request) {
            if(request.method == "DELETE") Source::json(request, {{"discarded", true}});
            else if(request.body.value("configure_preset") == "unsupported")
                Source::json(request, {{"error", QJsonObject{{"code", "preset_fields"}, {"message", "configure preset toolchainFile is unsupported"},
                    {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}}, 422);
            else Source::json(request, preview);
        };
        AgentApiClient api; api.setBaseUrl(source.url());
        QSignalSpy responses(&api,&AgentApiClient::responseReceived), errors(&api,&AgentApiClient::requestError);
        api.previewMission({{"repository_path", "D:/fixture"}, {"baseline_paths", QJsonArray{}}}); QTRY_COMPARE(responses.size(), 1);
        QCOMPARE(source.requests.first().method, QByteArray("POST")); QCOMPARE(source.requests.first().path, QByteArray("/api/v2/missions/preview"));
        api.discardMissionPreview(QString(64,'c')); QTRY_COMPARE(responses.size(), 2);
        QCOMPARE(source.requests.last().method, QByteArray("DELETE"));
        QCOMPARE(source.requests.last().path, QByteArray("/api/v2/missions/previews/")+QByteArray(64,'c'));
        api.previewMission({{"repository_path", "D:/fixture"}, {"configure_preset", "unsupported"}}); QTRY_COMPARE(errors.size(), 1);
        QVERIFY(errors.first()[5].toString().contains("toolchainFile"));
    }
    void missionHistoryUsesCollectionGetAndValidatesSummaryPages() {
        auto summary = mission();
        for (const auto &key : {"review", "last_event_id", "graph_id", "graph_version", "base_ref", "toolchain_profile"}) summary.remove(key);
        const QJsonObject page{{"items", QJsonArray{summary}}, {"total", 2}, {"next_cursor", "page-two"}};
        QVERIFY(MissionProtocol::validMissionPage(page));
        auto bad = page; bad["items"] = QJsonArray{summary, summary}; QVERIFY(!MissionProtocol::validMissionPage(bad));
        bad = page; bad["total"] = "2"; QVERIFY(!MissionProtocol::validMissionPage(bad));
        auto malformed = summary; malformed["recovery_required"] = "true";
        bad = page; bad["items"] = QJsonArray{malformed}; QVERIFY(!MissionProtocol::validMissionPage(bad));
        Source source; source.handle = [&](const Request &r) { Source::json(r, page); };
        AgentApiClient api; api.setBaseUrl(source.url()); QSignalSpy responses(&api, &AgentApiClient::responseReceived);
        const QString repository = "D:/项目 with spaces/C++ 100%/%2B";
        const QString cursor = "page+two%20";
        api.listMissions(cursor, repository, "interrupted");
        QTRY_COMPARE(responses.size(), 1); QCOMPARE(source.requests.size(), 1);
        const auto &request = source.requests.first(); QCOMPARE(request.method, "GET"); QVERIFY(request.body.isEmpty());
        // The HTTP server treats a raw '+' in query values as a space.
        QVERIFY(!request.path.contains('+'));
        const auto url = QUrl::fromEncoded(request.path); QCOMPARE(url.path(), "/api/v2/missions");
        const QUrlQuery query(url); QCOMPARE(query.queryItemValue("limit"), "20");
        QCOMPARE(query.queryItemValue("before", QUrl::FullyDecoded), cursor); QCOMPARE(query.queryItemValue("status"), "interrupted");
        QCOMPARE(query.queryItemValue("repository_path", QUrl::FullyDecoded), repository);
    }
    void strictRawJson() {
        QJsonObject o;
        QVERIFY(JsonHttp::parseStrictObject(R"({"text":"汉字","n":8,"items":[{"ok":true}]})", o));
        const QList<QByteArray> invalid{R"({"revision":1,"revision":2})", "{\"nested\":{\"x\":1,\"\\u0078\":2}}",
            "{\"text\":\"\\ud800\"}", "{\"text\":\"\\udc00\"}", QByteArray("{\"text\":\"") + char(0xff) + "\"}",
            "[]", "null", "{", R"({"n":NaN})"};
        for (const auto &bytes : invalid) QVERIFY2(!JsonHttp::parseStrictObject(bytes, o), bytes.constData());
        QVERIFY(JsonHttp::parseStrictObject("{\"text\":\"\\ud83d\\ude00\"}", o));
    }
    void strictSchemas() {
        const auto good = mission();
        QVERIFY(MissionProtocol::validMission(good));
        for (const auto &invalid : {QJsonValue(true), QJsonValue("8"), QJsonValue(1.5), QJsonValue(0), QJsonValue(-1)}) {
            auto body = good; body["current_revision"] = invalid;
            QVERIFY(!MissionProtocol::validMission(body));
        }
        auto body = good; body.remove("review"); QVERIFY(!MissionProtocol::validMission(body));
        body = good; body["task_id"] = B; QVERIFY(!MissionProtocol::validMission(body));
        body = good; auto review = body["review"].toObject(); review["plan_hash"] = 42; body["review"] = review;
        QVERIFY(!MissionProtocol::validMission(body));
        QVERIFY(MissionProtocol::validGraph(graph()));
        auto g = graph(); auto nodes = g["nodes"].toArray(); nodes.append(nodes[0]); g["nodes"] = nodes;
        QVERIFY(!MissionProtocol::validGraph(g));
        QVERIFY(MissionProtocol::validArtifact(artifact()));
        auto a = artifact(); auto content = a["content"].toObject(); auto items = content["items"].toArray();
        auto item = items[0].toObject(); item["confidence"] = "0.8"; items[0] = item; content["items"] = items; a["content"] = content;
        QVERIFY(!MissionProtocol::validArtifact(a));
    }
    void unknownArtifactsRequireValidListEnvelopes() {
        const auto future = futureArtifact();
        const auto list = [](const QJsonArray &items) {
            return QJsonObject{{"task_id", A}, {"items", items}};
        };
        QVERIFY(MissionProtocol::validArtifacts(list({future})));
        for (const auto &field : future.keys()) {
            auto incomplete = future;
            incomplete.remove(field);
            QVERIFY2(!MissionProtocol::validArtifacts(list({incomplete})), qPrintable(field));
        }
        auto foreign = future;
        foreign["task_id"] = B;
        QVERIFY(!MissionProtocol::validArtifacts(list({foreign})));
        QVERIFY(!MissionProtocol::validArtifacts(list({artifact(), future}))); // Same artifact ID.

        QVERIFY(!MissionProtocol::validResponse("mission.artifact", A, future));
        QVERIFY(!MissionProtocol::validDiff({{"task_id", A}, {"patch", ""}, {"change_sets", QJsonArray{future}}}));
        QVERIFY(!MissionProtocol::validValidation({{"task_id", A}, {"items", QJsonArray{future}}, {"latest", future}}));
    }
    void malformedKnownReportStillFailsArtifactListRequest() {
        auto report = artifact();
        report["artifact_id"] = "33333333-3333-4333-8333-333333333333";
        report["artifact_type"] = "ValidationReport";
        report["content"] = QJsonObject{{"status", "passed"}};
        Source source;
        source.handle = [&](const Request &r) {
            Source::json(r, {{"task_id", A}, {"items", QJsonArray{futureArtifact(), report}}});
        };
        AgentApiClient api;
        QVERIFY(api.setBaseUrl(source.url()));
        QSignalSpy responses(&api, &AgentApiClient::responseReceived), errors(&api, &AgentApiClient::requestError);
        api.getMissionArtifacts(A);
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors.first()[4].toString(), "invalid_response");
        QVERIFY(responses.isEmpty());
    }
    void allV2Routes() {
        Source source;
        source.handle = [](const Request &r) {
            if (r.path.endsWith("/graph")) Source::json(r, graph());
            else if (r.path.endsWith("/artifacts")) Source::json(r, {{"task_id", A}, {"items", QJsonArray{artifact()}}});
            else if (r.path.endsWith(B.toUtf8())) Source::json(r, artifact());
            else if (r.path.endsWith("/diff")) Source::json(r, {{"task_id", A}, {"patch", ""}, {"change_sets", QJsonArray{}}});
            else if (r.path.endsWith("/validation")) Source::json(r, {{"task_id", A}, {"items", QJsonArray{}}, {"latest", QJsonValue::Null}});
            else if (r.path.endsWith("/resume")) Source::json(r, {{"mission", mission()}, {"recovery", QJsonValue::Null}}, 202);
            else if (r.path.endsWith("/delivery")) Source::json(r, {{"mission", mission()}, {"delivery", QJsonObject{{"task_id", A}, {"token", QString(64, 'd')}}}});
            else Source::json(r, mission(), r.method == "POST" && !r.path.endsWith("/cancel") ? 202 : 200);
        };
        AgentApiClient api; QVERIFY(api.setBaseUrl(source.url()));
        QSignalSpy responses(&api, &AgentApiClient::responseReceived), errors(&api, &AgentApiClient::requestError);
        api.createMission({{"repository_path", "D:/Projects/Example"}, {"request", "Fix worker"}});
        api.getMission(A); api.getMissionGraph(A); api.getMissionArtifacts(A); api.getMissionArtifact(A, B);
        api.getMissionDiff(A); api.getMissionValidation(A); api.approveMission(A, approval());
        api.cancelMission(A, 8); api.retryMission(A, 8, "analysis"); api.resumeMission(A, 8, QString(64, 'd'));
        api.deliverMission(A, {{"expected_revision", 8}, {"mode", "preview"}});
        QTRY_COMPARE(responses.size(), 12); QCOMPARE(errors.size(), 0); QCOMPARE(source.requests.size(), 12);
        QSet<QByteArray> paths;
        for (const auto &r : source.requests) {
            QVERIFY(r.path.startsWith("/api/v2/missions")); paths.insert(r.path);
            if (r.path.endsWith("/approvals")) QCOMPARE(r.body, approval());
            if (r.path.endsWith("/retry")) QCOMPARE(r.body.value("node_id").toString(), "analysis");
            if (r.path.endsWith("/resume")) QCOMPARE(r.body.value("recovery_token").toString(), QString(64, 'd'));
        }
        QCOMPARE(paths.size(), 12);
    }
    void recoveryAndDeliveryBindings() {
        const QJsonObject preview{{"task_id", A}, {"token", QString(64, 'd')}};
        QJsonObject d{{"mission", mission()}, {"delivery", preview}};
        QVERIFY(MissionProtocol::validResponse("mission.delivery", A, d));
        auto bad = preview; bad["task_id"] = B; d["delivery"] = bad;
        QVERIFY(!MissionProtocol::validResponse("mission.delivery", A, d));
        bad = preview; bad["validated"] = "true"; d["delivery"] = bad;
        QVERIFY(!MissionProtocol::validResponse("mission.delivery", A, d));
        d["delivery"] = QJsonObject{}; QVERIFY(!MissionProtocol::validResponse("mission.delivery", A, d));
        auto recovered = QJsonObject{{"mode", "squash"}, {"status", "active"}, {"outcome", "restored"},
            {"target_branch", "main"}, {"target_commit", QString(40, 'a')}};
        d["delivery"] = recovered; QVERIFY(MissionProtocol::validResponse("mission.delivery", A, d));
        recovered["outcome"] = "completed";
        d["delivery"] = recovered; QVERIFY(!MissionProtocol::validResponse("mission.delivery", A, d));
        auto manual = preview;
        manual["outcome"] = "manual_inspection";
        d["delivery"] = manual; QVERIFY(!MissionProtocol::validResponse("mission.delivery", A, d));
        const QJsonObject plan{{"task_id", A}, {"revision", 8}, {"token", QString(64, 'd')}, {"blocked_reason", QJsonValue::Null}};
        QJsonObject r{{"mission", mission()}, {"recovery", plan}};
        QVERIFY(MissionProtocol::validResponse("mission.resume", A, r));
        bad = plan; bad["revision"] = 7; r["recovery"] = bad;
        QVERIFY(!MissionProtocol::validResponse("mission.resume", A, r));
        bad = plan; bad["blocked_reason"] = false; r["recovery"] = bad;
        QVERIFY(!MissionProtocol::validResponse("mission.resume", A, r));
        bad = plan; bad["token"] = ""; r["recovery"] = bad;
        QVERIFY(!MissionProtocol::validResponse("mission.resume", A, r));
        bad["blocked_reason"] = "terminal_graph"; r["recovery"] = bad;
        QVERIFY(MissionProtocol::validResponse("mission.resume", A, r));
    }
    void stableErrorsAndNoMutationReplay() {
        Source source;
        source.handle = [](const Request &r) {
            Source::json(r, {{"error", QJsonObject{{"code", "mission_approval_conflict"}, {"message", "Unstable backend detail"},
                {"details", QJsonArray{}}, {"task_id", A}}}}, 409);
        };
        AgentApiClient api; api.setBaseUrl(source.url()); QSignalSpy errors(&api, &AgentApiClient::requestError);
        api.approveMission(A, approval()); QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors[0][4].toString(), "mission_approval_conflict");
        QCOMPARE(errors[0][5].toString(), MissionProtocol::errorMessage("mission_approval_conflict", 409));
        QVERIFY(errors[0][5].toString().contains("旧审批不会自动重试"));
        QTest::qWait(50); QCOMPARE(source.requests.size(), 1);
        QVERIFY(MissionProtocol::staleError("mission_revision_conflict"));
        QVERIFY(!MissionProtocol::staleError("permission_denied"));
        QVERIFY(MissionProtocol::errorMessage("permission_denied", 403).contains("权限"));
        QVERIFY(MissionProtocol::errorMessage("network_error").contains("刷新任务状态"));
        source.handle = [](const Request &r) { if (r.socket) r.socket->abort(); };
        api.approveMission(A, approval()); QTRY_COMPARE(errors.size(), 2);
        QTest::qWait(50); QCOMPARE(source.requests.size(), 2);
    }
    void malformedAndForeignResponses() {
        Source source; int mode = 0;
        source.handle = [&mode](const Request &r) {
            if (mode == 0) raw(r, "{\"task_id\":\"wrong\",\"task_id\":\"duplicate\"}");
            else if (mode == 1) { auto m = mission(); m["task_id"] = B; Source::json(r, m); }
            else raw(r, "{\"error\":{\"code\":7}}", 409);
        };
        AgentApiClient api; api.setBaseUrl(source.url()); QSignalSpy errors(&api, &AgentApiClient::requestError);
        for (mode = 0; mode < 3; ++mode) {
            api.getMission(A); QTRY_COMPARE(errors.size(), mode + 1);
            QCOMPARE(errors.last()[4].toString(), "invalid_response");
        }
    }
    void previousGraphRequiresNewApproval_data() {
        QTest::addColumn<QString>("code"); QTest::addColumn<QString>("reason");
        QTest::newRow("previous-graph") << QString("graph_definition") << QString("任务冻结流程已变化");
        QTest::newRow("missing-source-build") << QString("mission_frozen_config_missing") << QString("缺少冻结的源码基线或构建配置");
        QTest::newRow("changed-build-selection") << QString("build_selection_changed") << QString("预设文件、构建配置或工具身份已变化");
    }
    void previousGraphRequiresNewApproval() {
        QFETCH(QString, code); QFETCH(QString, reason);
        Source source;
        source.handle = [&code](const Request &r) {
            Source::json(r, {{"error", QJsonObject{{"code", code},
                {"message", "Recreate the mission and approve its execution plan"},
                {"details", QJsonArray{}}, {"task_id", A}}}}, 409);
        };
        AgentApiClient api; api.setBaseUrl(source.url());
        QSignalSpy errors(&api, &AgentApiClient::requestError);
        api.resumeMission(A, 8);
        QTRY_COMPARE(errors.size(), 1);
        QCOMPARE(errors[0][4].toString(), code);
        QVERIFY(errors[0][5].toString().contains(reason));
        QVERIFY(errors[0][5].toString().contains("重新创建任务并审批执行方案"));
        QVERIFY(errors[0][5].toString().contains("历史记录仍可查看"));
        QTest::qWait(50); QCOMPARE(source.requests.size(), 1);
    }
    void invalidRequestIsLocalAndCancellable() {
        Source source; AgentApiClient api; api.setBaseUrl(source.url()); QSignalSpy errors(&api, &AgentApiClient::requestError);
        auto invalid = approval(); invalid.remove("scope_hash");
        api.approveMission(A, invalid); QTRY_COMPARE(errors.size(), 1); QCOMPARE(errors[0][4].toString(), "invalid_request");
        const auto rejected = api.getMission("invalid"); api.abortRequest(rejected);
        QTest::qWait(20); QCOMPARE(errors.size(), 1); QCOMPARE(source.requests.size(), 0);
    }
    void cancellationAndDestruction() {
        Source source; AgentApiClient api; api.setBaseUrl(source.url());
        QSignalSpy responses(&api, &AgentApiClient::responseReceived), errors(&api, &AgentApiClient::requestError);
        const auto serial = api.getMission(A); QTRY_COMPARE(source.requests.size(), 1);
        api.abortRequest(serial); Source::json(source.requests[0], mission()); QTest::qWait(20);
        QCOMPARE(responses.size(), 0); QCOMPARE(errors.size(), 0);
        api.getMission(A); QTRY_COMPARE(source.requests.size(), 2); api.setBaseUrl(source.url());
        Source::json(source.requests[1], mission()); QTest::qWait(20); QCOMPARE(responses.size(), 0);
        auto *owned = new AgentApiClient; owned->setBaseUrl(source.url()); owned->getMission(A);
        QTRY_COMPARE(source.requests.size(), 3); delete owned; Source::json(source.requests[2], mission());
        QTest::qWait(20); QCOMPARE(errors.size(), 0);
    }
};
QTEST_GUILESS_MAIN(MissionApiTests)
#include "tst_mission_api.moc"
