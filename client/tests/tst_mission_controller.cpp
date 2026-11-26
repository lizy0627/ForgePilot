#include <QtTest>
#include <QFile>
#include <QAbstractItemModelTester>
#include <QUrlQuery>
#include "HttpFixture.h"
#include "workbench/MissionController.h"
#include "workbench/MissionProtocol.h"
#define EXPECT_EQ(actual, expected) do { QTest::qCompare(actual, expected, #actual, #expected, __FILE__, __LINE__); } while (false)

namespace {
QJsonObject protocolExample(const char *name)
{
    QFile file(QString(FORGEPILOT_SOURCE_DIR) + "/protocol/examples.json");
    if (!file.open(QIODevice::ReadOnly)) qFatal("Cannot open canonical protocol examples");
    return QJsonDocument::fromJson(file.readAll()).object().value(name).toObject();
}
QJsonObject mission(const QString &task = A, qint64 revision = 8, const QString &status = "awaiting_human")
{
    auto body = protocolExample("mission_query_response");
    body.insert("task_id", task); body.insert("current_revision", revision); body.insert("status", status);
    body.insert("last_event_id", task + ':' + QString::number(revision));
    auto review = body.value("review").toObject(); review.insert("task_id", task); review.insert("revision", revision);
    body.insert("review", review); return body;
}
QJsonObject graph(const QJsonObject &m, const QString &nodeId = "execution_approval",
                  QJsonValue action = QJsonValue::Undefined)
{
    const QString type = nodeId == "execution_approval" ? "human_gate" : "agent";
    QJsonObject node{{"node_id", nodeId}, {"node_type", type},
        {"status", m.value("status")}, {"attempt", 1}, {"result", QJsonValue::Null}};
    node.insert("pending_action", action);
    QJsonObject definition{{"node_id", nodeId}, {"node_type", type},
        {"handler", nodeId}, {"dependencies", QJsonArray{}}, {"agent_id", QJsonValue::Null},
        {"agent_definition_hash", QJsonValue::Null}, {"settings", QJsonObject{}}};
    return {{"task_id", m.value("task_id")}, {"graph_id", m.value("graph_id")}, {"graph_version", 1},
        {"definition_hash", QString(64, 'a')}, {"status", m.value("status")}, {"current_revision", m.value("current_revision")},
        {"nodes", QJsonArray{node}}, {"definition", QJsonObject{{"graph_id", m.value("graph_id")}, {"version", 1},
        {"nodes", QJsonArray{definition}}, {"edges", QJsonArray{}}}}};
}
QJsonObject conflict(const QString &code = "mission_revision_conflict")
{ return {{"error", QJsonObject{{"code", code}, {"message", "Changed"}, {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}}; }
QByteArray missionFrame(qint64 rev, const QString &id = A)
{
    const auto cursor = id + ':' + QString::number(rev);
    QJsonObject body{{"event_id", cursor}, {"task_id", id}, {"task_revision", rev},
        {"timestamp", "2026-10-27T08:00:01Z"}, {"event_type", "node.completed"}, {"node_id", "execution_approval"},
        {"actor", "user"}, {"data", QJsonObject{}}};
    return "id: " + cursor.toUtf8() + "\nevent: node.completed\ndata: " + QJsonDocument(body).toJson(QJsonDocument::Compact) + "\n\n";
}
struct Fixture {
    Source source;
    QJsonObject current = mission();
    std::function<bool(const Request &)> intercept;
    QList<Request> streams;
    int reads = 0;
    Fixture() {
        source.handle = [this](const Request &r) {
            if (intercept && intercept(r)) return;
            const auto task = current.value("task_id");
            if (r.path.startsWith("/api/v2/missions?")) Source::json(r, {{"items", QJsonArray{current}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
            else if (r.path.endsWith("/events")) { streams.append(r); Source::stream(r, ": ready\n\n", false); }
            else if (r.path.endsWith("/graph")) Source::json(r, graph(current));
            else if (r.path.endsWith("/artifacts")) Source::json(r, {{"task_id", task}, {"items", QJsonArray{}}});
            else if (r.path.endsWith("/diff")) Source::json(r, {{"task_id", task}, {"patch", ""}, {"change_sets", QJsonArray{}}});
            else if (r.path.endsWith("/validation")) Source::json(r, {{"task_id", task}, {"items", QJsonArray{}}, {"latest", QJsonValue::Null}});
            else if (r.method == "GET") { ++reads; Source::json(r, current); }
            else Source::json(r, current, 202);
        };
    }
    int posts(const QByteArray &suffix) const {
        int count = 0; for (const auto &r : source.requests) if (r.method == "POST" && r.path.endsWith(suffix)) ++count; return count;
    }
};
}

class MissionControllerTests : public QObject {
    Q_OBJECT
private slots:
    void initTestCase() { QVERIFY(MissionProtocol::validMission(mission())); QVERIFY(MissionProtocol::validGraph(graph(mission()))); }
    void skillRecommendationsDebounceAndIgnoreSupersededReplies()
    {
        Fixture f;
        QList<Request> held;
        f.intercept = [&](const Request &r) {
            if (r.path != "/api/v2/skills/recommendations") return false;
            held.append(r);
            return true;
        };
        MissionController c;
        c.setBaseUrl(f.source.url());
        for (const auto &text : {"a", "ab", "abc"})
            c.updateSkillContext(text, "auto", "D:/repo", {});
        QTRY_COMPARE(held.size(), 1);
        QCOMPARE(held[0].body, (QJsonObject{{"description", "abc"}, {"task_type", "auto"}}));
        c.updateSkillContext("new", "bug_fix", "D:/repo", {"Qt"});
        QTRY_COMPARE(held.size(), 2);
        QCOMPARE(held[1].body, (QJsonObject{{"description", "new"}, {"task_type", "bug_fix"}, {"project_tags", QJsonArray{"Qt"}}}));
        const auto latest = protocolExample("skill_recommendations_response");
        auto stale = latest.value("recommendations").toArray().first().toObject();
        stale.insert("name", "stale result");
        QSignalSpy changes(&c, &MissionController::skillRecommendationsChanged);
        Source::json(held[1], latest);
        Source::json(held[0], {{"recommendations", QJsonArray{stale}}});
        QTRY_VERIFY(!c.recommendationsLoading());
        QTest::qWait(80);
        QCOMPARE(changes.size(), 1);
        QCOMPARE(c.skillRecommendations(), latest.value("recommendations").toArray().toVariantList());
        QVERIFY(c.selectedSkillIds().isEmpty());
        const auto id = stale.value("skill_id").toString();
        c.setSkillSelected(id, true);
        QCOMPARE(c.selectedSkillIds(), QStringList{id});
        QCOMPARE(changes.size(), 1);
        c.updateSkillContext("new", "bug_fix", "D:/other", {"Qt"});
        QVERIFY(c.selectedSkillIds().isEmpty());
        QVERIFY(c.skillRecommendations().isEmpty());
        QTRY_COMPARE(held.size(), 3);
        QCOMPARE(held[2].body, held[1].body); // Repository identity stays local.

        Fixture other;
        c.setBaseUrl(other.source.url());
        Source::json(held[2], latest);
        QTRY_VERIFY(c.historyLoaded());
        QVERIFY(c.skillRecommendations().isEmpty());
        QVERIFY(!c.recommendationsLoading());
        QCOMPARE(other.posts("/skills/recommendations"), 0);
    }
    void checkedSkillsUseExactIdentitiesAndRejectionRequiresExplicitResubmission_data()
    {
        QTest::addColumn<QString>("code");
        QTest::addColumn<int>("status");
        QTest::addColumn<bool>("refreshChoices");
        QTest::newRow("superseded-version") << QString("skill_selection_changed") << 409 << true;
        QTest::newRow("older-create-schema") << QString("invalid_request") << 422 << true;
        QTest::newRow("local-route-required") << QString("skill_route_denied") << 403 << false;
        QTest::newRow("input-budget") << QString("skill_budget_exceeded") << 422 << false;
    }
    void checkedSkillsUseExactIdentitiesAndRejectionRequiresExplicitResubmission()
    {
        QFETCH(QString, code);
        QFETCH(int, status);
        QFETCH(bool, refreshChoices);
        Fixture f;
        const auto exact = protocolExample("mission_skills_response").value("selected_skills").toArray();
        auto first = protocolExample("skill_recommendations_response").value("recommendations").toArray().first().toObject();
        auto second = first;
        const auto secondIdentity = exact[1].toObject();
        for (auto it = secondIdentity.begin(); it != secondIdentity.end(); ++it)
            second.insert(it.key(), it.value());
        auto third = first;
        third.insert("skill_id", "third");
        const QJsonObject offered{{"recommendations", QJsonArray{first, second, third}}};
        const QString token(64, 'a');
        const QJsonObject preview{{"preview_token", token}, {"base_commit", QString(40,'a')},
            {"source_head", QString(40,'a')}, {"baseline_paths", QJsonArray{}}, {"changed_files", QJsonArray{}},
            {"patch", ""}, {"build", QJsonObject{{"source", "preset"}}}};
        QList<QJsonObject> creations;
        f.intercept = [&](const Request &r) {
            if (r.path == "/api/v2/missions/preview") Source::json(r, preview);
            else if (r.path == "/api/v2/skills/recommendations") Source::json(r, offered);
            else if (r.method == "POST" && r.path == "/api/v2/missions") {
                creations.append(r.body);
                if (creations.size() == 1) Source::json(r, conflict(code), status);
                else Source::json(r, f.current, 202);
            } else return false;
            return true;
        };
        MissionController c;
        c.setBaseUrl(f.source.url());
        c.previewCreation({{"repository_path", "D:/repo"}});
        c.updateSkillContext("worker", "bug_fix", "D:/repo", {});
        QTRY_VERIFY(c.creationReady());
        QTRY_COMPARE(c.skillRecommendations().size(), 3);
        c.setSkillSelected(first.value("skill_id").toString(), true);
        c.setSkillSelected(second.value("skill_id").toString(), true);
        c.setSkillSelected("third", true);
        QCOMPARE(c.selectedSkillIds().size(), 2);
        QCOMPARE(c.creationPreview(), preview.toVariantMap());
        QVERIFY(c.creationReady());
        QCOMPARE(f.posts("/missions/preview"), 1);
        const QVariantMap draft{{"request", "worker"}, {"repository_path", "D:/repo"},
            {"task_type", "bug_fix"}, {"preview_token", token}};
        c.createMission(draft);
        QTRY_VERIFY(!c.creationError().isEmpty());
        QCOMPARE(creations.size(), 1);
        auto expected = QJsonObject::fromVariantMap(draft);
        expected.insert("selected_skills", exact);
        QCOMPARE(creations[0], expected);
        if (refreshChoices) {
            QVERIFY(c.selectedSkillIds().isEmpty());
            QTRY_COMPARE(f.posts("/skills/recommendations"), 2);
        } else {
            QCOMPARE(c.selectedSkillIds().size(), 2);
            QCOMPARE(f.posts("/skills/recommendations"), 1);
            c.setSkillSelected(second.value("skill_id").toString(), false);
        }
        QTRY_VERIFY(!c.recommendationsLoading());
        QCOMPARE(creations.size(), 1);
        QVERIFY(c.creationReady());
        QCOMPARE(c.creationPreview(), preview.toVariantMap());
        c.createMission(draft);
        QTRY_COMPARE(creations.size(), 2);
        auto retry = QJsonObject::fromVariantMap(draft);
        if (!refreshChoices) retry.insert("selected_skills", QJsonArray{exact.first()});
        QCOMPARE(creations[1], retry);
        QCOMPARE(f.posts("/missions/preview"), 1);
    }
    void unavailableRecommendationRouteAllowsPlainCreation()
    {
        Fixture f;
        f.intercept = [&](const Request &r) {
            if (r.path != "/api/v2/skills/recommendations") return false;
            Source::json(r, {{"detail", "Not Found"}}, 404);
            return true;
        };
        MissionController c;
        c.setBaseUrl(f.source.url());
        c.updateSkillContext("worker", "auto", "D:/repo", {});
        QTRY_VERIFY(!c.recommendationError().isEmpty());
        QVERIFY(c.creationError().isEmpty());
        QVERIFY(c.error().isEmpty());
        const QVariantMap draft{{"request", "worker"}, {"repository_path", "D:/repo"}};
        c.createMission(draft);
        QTRY_COMPARE(c.taskId(), A);
        QCOMPARE(f.posts("/missions"), 1);
        for (const auto &r : f.source.requests)
            if (r.method == "POST" && r.path == "/api/v2/missions") QCOMPARE(r.body, QJsonObject::fromVariantMap(draft));
    }
    void frozenSkillUsageLoadsOnlyOnDemandAndFollowsTaskIdentity()
    {
        Fixture f;
        QList<Request> held;
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/skills")) return false;
            held.append(r);
            return true;
        };
        MissionController c;
        c.setBaseUrl(f.source.url());
        c.selectMission(A);
        QTRY_VERIFY(c.synchronized());
        QCOMPARE(held.size(), 0);
        c.loadMissionSkills();
        c.loadMissionSkills();
        QTRY_COMPARE(held.size(), 1);
        f.current = mission(B, 12, "succeeded");
        c.selectMission(B);
        QVERIFY(c.missionSkills().isEmpty());
        c.loadMissionSkills();
        QTRY_COMPARE(held.size(), 2);
        auto latest = protocolExample("mission_skills_response");
        latest.insert("task_id", B);
        Source::json(held[1], latest);
        Source::json(held[0], protocolExample("mission_skills_empty_response"));
        QTRY_COMPARE(c.missionSkills(), latest.toVariantMap());
        c.refresh();
        QTRY_VERIFY(c.synchronized());
        QCOMPARE(held.size(), 2);
        c.loadMissionSkills();
        QTRY_COMPARE(held.size(), 3);
        Source::json(held[2], conflict("skill_snapshot_invalid"), 409);
        QTRY_VERIFY(!c.missionSkillsError().isEmpty());
        QVERIFY(c.error().isEmpty());
    }
    void creationPreviewInvalidationDisposesLateResultsAndRequiresReconfirmation_data()
    {
        QTest::addColumn<QString>("conflictCode");
        QTest::newRow("source-changed") << QString("baseline_changed");
        QTest::newRow("build-selection-changed") << QString("build_selection_changed");
    }
    void creationPreviewInvalidationDisposesLateResultsAndRequiresReconfirmation()
    {
        QFETCH(QString, conflictCode);
        Fixture f; QList<Request> previews; QStringList disposed;
        f.intercept=[&](const Request &request) {
            if(request.path == "/api/v2/missions/preview") { previews.append(request); return true; }
            if(request.path.startsWith("/api/v2/missions/previews/")) {
                disposed.append(QString::fromUtf8(request.path.mid(request.path.lastIndexOf('/')+1)));
                Source::json(request, {{"discarded", true}}); return true;
            }
            if(request.method == "POST" && request.path == "/api/v2/missions") {
                Source::json(request, conflict(conflictCode), 409); return true;
            }
            return false;
        };
        auto preview=[](char token) { return QJsonObject{{"preview_token", QString(64, token)}, {"base_commit", QString(40,'a')},
            {"source_head", QString(40,'a')}, {"baseline_paths", QJsonArray{"main.cpp"}}, {"changed_files", QJsonArray{"main.cpp"}},
            {"patch", "selected patch"}, {"build", QJsonObject{{"source", "preset"}}}}; };
        MissionController c; c.setBaseUrl(f.source.url());
        c.selectMission(A); QTRY_VERIFY(c.synchronized());
        c.previewCreation({{"repository_path", "D:/fixture"}}); QTRY_COMPARE(previews.size(), 1);
        c.invalidateCreationPreview(true); c.previewCreation({{"repository_path", "D:/other"}}); QTRY_COMPARE(previews.size(), 2);
        Source::json(previews[0], preview('b')); QTRY_VERIFY(disposed.contains(QString(64,'b')));
        QVERIFY(!c.creationReady()); QVERIFY(c.previewLoading());
        Source::json(previews[1], preview('c')); QTRY_VERIFY(c.creationReady());
        QCOMPARE(c.taskId(), A); QVERIFY(c.synchronized());
        c.invalidateCreationPreview(); QVERIFY(!c.creationReady()); QTRY_VERIFY(disposed.contains(QString(64,'c')));
        QCOMPARE(c.creationPreview().value("changed_files").toStringList(), QStringList{"main.cpp"});
        c.previewCreation({{"repository_path", "D:/other"}}); QTRY_COMPARE(previews.size(), 3);
        Source::json(previews[2], preview('d')); QTRY_VERIFY(c.creationReady());
        c.createMission({{"repository_path", "D:/other"}, {"request", "fixture"}, {"preview_token", QString(64,'d')},
            {"baseline_paths", QJsonArray{"main.cpp"}.toVariantList()}, {"baseline_confirmed", true}});
        QTRY_VERIFY(!c.creationError().isEmpty()); QVERIFY(!c.creationReady()); QVERIFY(!c.busy());
        QVERIFY(!c.creationPreview().contains("preview_token"));
    }
    void historyDiscoverySurvivesClientRestartWithoutResuming()
    {
        Fixture f; f.current = mission(A, 8, "interrupted"); f.current.insert("recovery_required", true);
        {
            MissionController first; first.setBaseUrl(f.source.url());
            QTRY_VERIFY(first.historyLoaded()); QCOMPARE(first.missions()->count(), 1);
            first.selectMission(A); QTRY_VERIFY(first.synchronized());
        }
        MissionController restarted; restarted.setBaseUrl(f.source.url());
        QTRY_VERIFY(restarted.historyLoaded()); QCOMPARE(restarted.missions()->get(0).value("taskId").toString(), A);
        QVERIFY(restarted.taskId().isEmpty()); restarted.selectMission(A); QTRY_VERIFY(restarted.synchronized());
        QCOMPARE(restarted.status(), "interrupted"); QVERIFY(restarted.recovery().isEmpty());
        for (const auto &request : f.source.requests) QCOMPARE(request.method, "GET");
    }
    void historyPaginationFiltersAndRefreshDiscardLatePages()
    {
        Fixture f; QList<Request> held; bool hold = false;
        f.intercept = [&](const Request &r) {
            if (!r.path.startsWith("/api/v2/missions?")) return false;
            if (hold) { held.append(r); return true; }
            const QUrlQuery query(QUrl::fromEncoded(r.path));
            if (query.queryItemValue("status") == "failed") {
                Source::json(r, {{"items", QJsonArray{mission(B, 9, "failed")}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
            } else if (!query.queryItemValue("before").isEmpty()) {
                Source::json(r, {{"items", QJsonArray{mission(B)}}, {"total", 2}, {"next_cursor", QJsonValue::Null}});
            } else Source::json(r, {{"items", QJsonArray{mission()}}, {"total", 2}, {"next_cursor", "stable-page-two"}});
            return true;
        };
        MissionController c; c.setBaseUrl(f.source.url()); QTRY_VERIFY(c.historyLoaded());
        QCOMPARE(c.missions()->count(), 1); QVERIFY(c.historyHasMore());
        c.loadMoreMissions(); QTRY_COMPARE(c.missions()->count(), 2); QVERIFY(!c.historyHasMore());
        QCOMPARE(c.missions()->get(0).value("taskId").toString(), A); QCOMPARE(c.missions()->get(1).value("taskId").toString(), B);
        c.refreshMissions(); QTRY_COMPARE(c.missions()->count(), 1);
        hold = true; c.loadMoreMissions(); QTRY_COMPARE(held.size(), 1);
        hold = false; c.setStatusFilter("failed"); QTRY_VERIFY(!c.historyLoading());
        QCOMPARE(c.historyTotal(), 1); QCOMPARE(c.missions()->get(0).value("taskId").toString(), B);
        Source::json(held.first(), {{"items", QJsonArray{mission()}}, {"total", 2}, {"next_cursor", QJsonValue::Null}});
        c.selectMission(A); QTRY_VERIFY(c.synchronized()); QTest::qWait(50);
        QCOMPARE(c.missions()->count(), 1); QCOMPARE(c.missions()->get(0).value("taskId").toString(), B);
        c.setRepositoryFilter("D:/Projects/Example"); QTRY_VERIFY(!c.historyLoading());
        const QUrlQuery query(QUrl::fromEncoded(f.source.requests.last().path));
        QCOMPARE(query.queryItemValue("repository_path", QUrl::FullyDecoded), "D:/Projects/Example"); QCOMPARE(query.queryItemValue("status"), "failed");
    }
    void historyErrorsAreIndependentFromSelectedMission()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); QTRY_VERIFY(c.historyLoaded());
        c.selectMission(A); QTRY_VERIFY(c.synchronized());
        f.intercept = [&](const Request &r) {
            if (!r.path.startsWith("/api/v2/missions?")) return false;
            Source::json(r, conflict("mission_internal"), 500); return true;
        };
        c.refreshMissions(); QTRY_VERIFY(!c.historyError().isEmpty());
        QVERIFY(c.error().isEmpty()); QVERIFY(c.synchronized()); QCOMPARE(c.taskId(), A);
        f.intercept = {}; c.refreshMissions(); QTRY_VERIFY(!c.historyLoading()); QVERIFY(c.historyError().isEmpty());
    }
    void historyUsesServerRepositoryIdentityAndNewestObservedRevision()
    {
        Fixture f; f.current = mission(A, 11, "running");
        MissionController c; c.setBaseUrl(f.source.url()); QTRY_VERIFY(c.historyLoaded());
        c.setRepositoryFilter("d:\\projects\\example"); QTRY_VERIFY(!c.historyLoading());
        c.selectMission(A); QTRY_VERIFY(c.synchronized());
        QCOMPARE(c.missions()->count(), 1); // The server accepted the slash/case alias.
        QList<Request> held;
        f.intercept = [&](const Request &r) {
            if (!r.path.startsWith("/api/v2/missions?")) return false;
            held.append(r); return true;
        };
        c.setStatusFilter("running"); QTRY_COMPARE(held.size(), 1); QCOMPARE(c.missions()->count(), 0);
        Source::json(held.first(), {{"items", QJsonArray{mission(A, 8, "running")}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
        QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.missions()->get(0).value("revision").toLongLong(), 11);
        c.refreshMissions(); QTRY_COMPARE(held.size(), 2);
        f.current = mission(A, 12, "failed"); c.refresh(); QTRY_COMPARE(c.revision(), 12);
        QTRY_COMPARE(held.size(), 3); QCOMPARE(c.missions()->count(), 0);
        Source::json(held[1], {{"items", QJsonArray{mission(A, 8, "running")}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
        Source::json(held[2], {{"items", QJsonArray{}}, {"total", 0}, {"next_cursor", QJsonValue::Null}});
        QTRY_VERIFY(!c.historyLoading()); QCOMPARE(c.missions()->count(), 0); QCOMPARE(c.historyTotal(), 0);
    }
    void loadsModelsAndApprovesOnlyCurrentFrozenPackage()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url());
        QAbstractItemModelTester tester(c.graph(), QAbstractItemModelTester::FailureReportingMode::QtTest);
        c.selectMission(A); QTRY_VERIFY(c.synchronized()); QCOMPARE(c.revision(), 8); QCOMPARE(c.graph()->count(), 1);
        QCOMPARE(c.missions()->count(), 1); QVERIFY(c.approval()->available());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/approvals")) return false;
            EXPECT_EQ(r.body.value("expected_revision").toInteger(), 8);
            EXPECT_EQ(r.body.value("task_spec_hash").toString(), QString(64, 'a'));
            EXPECT_EQ(r.body.value("plan_hash").toString(), QString(64, 'b'));
            EXPECT_EQ(r.body.value("scope_hash").toString(), QString(64, 'c'));
            f.current = mission(A, 9, "running"); Source::json(r, f.current, 202); return true;
        };
        c.approval()->approve(); c.approval()->approve();
        QTRY_COMPARE(c.revision(), 9); QTRY_VERIFY(c.synchronized());
        QCOMPARE(c.status(), "running"); QCOMPARE(f.posts("/approvals"), 1); QVERIFY(!c.approval()->available());
    }
    void staleApprovalRefreshesWithoutResubmitting_data()
    {
        QTest::addColumn<QString>("code");
        for (const char *code : {"mission_revision_conflict", "mission_approval_conflict", "approval_stale"}) QTest::newRow(code) << QString(code);
    }
    void scopeDecisionsBindDisplayedProposal_data()
    {
        QTest::addColumn<bool>("approved");
        QTest::newRow("approve") << true;
        QTest::newRow("reject") << false;
    }
    void scopeDecisionsBindDisplayedProposal()
    {
        QFETCH(bool, approved);
        Fixture f; auto scope = protocolExample("mission_scope_expansion_preview");
        const QString nodeId = "edit_source";
        scope["node_id"] = nodeId;
        QFile artifactFile(QString(FORGEPILOT_SOURCE_DIR) + "/client/tests/fixtures/mission_artifacts.json");
        QVERIFY(artifactFile.open(QIODevice::ReadOnly));
        QJsonObject revision;
        for (const auto &item : QJsonDocument::fromJson(artifactFile.readAll()).object().value("items").toArray())
            if (item.toObject().value("artifact_type") == "TaskSpec") revision = item.toObject();
        QVERIFY(!revision.isEmpty());
        revision["task_id"] = A;
        revision["artifact_id"] = scope.value("task_spec_artifact_id");
        revision["producer"] = "orchestrator.v1";
        revision["producer_node"] = "scope_expansion";
        revision["content_sha256"] = scope.value("task_spec_hash");
        f.current = mission(A, 42);
        f.current["recovery_required"] = true;
        QList<Request> decisions;
        int attempt = 1;
        f.intercept = [&](const Request &r) {
            if (r.path.endsWith("/graph")) {
                auto body = graph(f.current, nodeId,
                    scope.value("status") == "pending" ? QJsonValue("scope_expansion") : QJsonValue(QJsonValue::Null));
                auto node = body.value("nodes").toArray().first().toObject();
                node["attempt"] = attempt;
                body["nodes"] = QJsonArray{node};
                Source::json(r, body);
            }
            else if (r.path.endsWith("/artifacts")) Source::json(r, {{"task_id", A}, {"items", QJsonArray{revision}}});
            else if (r.path.endsWith("/scope-expansion")) Source::json(r, scope);
            else if (r.path.endsWith("/approvals")) decisions.append(r);
            else return false;
            return true;
        };
        MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        auto *approval = c.approval();
        QSignalSpy resumed(approval, &ApprovalController::scopeExpansionApproved);
        QTRY_VERIFY(approval->scopeAvailable()); QVERIFY(!approval->available());
        const auto old = approval->scopeExpansion();
        approval->decideScopeExpansion(old, approved); approval->decideScopeExpansion(old, !approved);
        QTRY_COMPARE(decisions.size(), 1); QVERIFY(c.busy());
        QCOMPARE(decisions[0].body.value("approval_type").toString(), "scope_expansion");
        QCOMPARE(decisions[0].body.value("expected_revision").toInt(), 42);
        QCOMPARE(decisions[0].path, "/api/v2/missions/" + A.toUtf8() + "/approvals");
        for (const auto *key : {"plan_hash", "task_spec_hash", "scope_hash"})
            QCOMPARE(decisions[0].body.value(key), scope.value(key));

        f.current = mission(A, 43); scope.insert("revision", 43);
        Source::json(decisions.first(), conflict(), 409);
        QTRY_COMPARE(c.revision(), 43); QTRY_VERIFY(approval->scopeAvailable());
        QVERIFY(!approval->error().isEmpty()); QCOMPARE(resumed.count(), 0);
        approval->decideScopeExpansion(old, approved); // stale dialog after refresh
        QTRY_VERIFY(approval->scopeAvailable()); QCOMPARE(decisions.size(), 1);
        approval->decideScopeExpansion(approval->scopeExpansion(), approved);
        QTRY_COMPARE(decisions.size(), 2);
        QCOMPARE(decisions[1].body.value("decision").toString(), approved ? "approved" : "rejected");
        QCOMPARE(decisions[1].body.value("expected_revision").toInt(), 43);
        f.current = mission(A, 44, approved ? "running" : "awaiting_human");
        scope.insert("revision", 44); scope.insert("status", approved ? "approved" : "rejected");
        Source::json(decisions.last(), f.current, 202);
        QTRY_COMPARE(c.revision(), 44); QTRY_VERIFY(c.synchronized()); QCOMPARE(c.taskId(), A);
        QCOMPARE(resumed.count(), approved ? 1 : 0); QVERIFY(!approval->scopeAvailable());
        if (!approved) {
            QCOMPARE(approval->scopeExpansion().value("status").toString(), "rejected");
            c.clearSelection(); c.selectMission(A);
            QTRY_VERIFY(c.synchronized());
            QCOMPARE(approval->scopeExpansion(), scope.toVariantMap());
            QVERIFY(!approval->scopeAvailable());
        }
        QVERIFY(!approval->available()); QCOMPARE(f.posts("/approvals"), 2);
        ++attempt;
        f.current = mission(A, 45);
        scope["revision"] = 45;
        c.refresh(); QTRY_COMPARE(c.revision(), 45);
        QVERIFY(approval->scopeExpansion().isEmpty()); QVERIFY(!approval->scopeAvailable());
    }
    void olderProposalCannotAuthorizePausedWriter_data()
    {
        QTest::addColumn<QString>("nodeId"); QTest::addColumn<int>("attempt");
        QTest::newRow("another-writer") << QString("implementer") << 1;
        QTest::newRow("another-attempt") << QString("repair_1") << 2;
    }
    void olderProposalCannotAuthorizePausedWriter()
    {
        QFETCH(QString, nodeId); QFETCH(int, attempt);
        Fixture f; f.current = mission(A, 42);
        auto scope = protocolExample("mission_scope_expansion_preview");
        scope.insert("status", "approved");
        f.intercept = [&](const Request &r) {
            if (r.path.endsWith("/scope-expansion")) Source::json(r, scope);
            else if (r.path.endsWith("/graph")) {
                auto body = graph(f.current, nodeId);
                auto node = body.value("nodes").toArray().first().toObject();
                node.insert("attempt", attempt); body.insert("nodes", QJsonArray{node});
                Source::json(r, body);
            } else return false;
            return true;
        };
        MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A); QTRY_VERIFY(c.synchronized());
        QVERIFY(c.approval()->scopeExpansion().isEmpty());
        QVERIFY(!c.approval()->scopeAvailable()); QVERIFY(!c.approval()->available());
    }
    void staleApprovalRefreshesWithoutResubmitting()
    {
        QFETCH(QString, code); Fixture f; MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        QTRY_VERIFY(c.approval()->available());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/approvals")) return false;
            f.current = mission(A, 9); auto review = f.current.value("review").toObject();
            review.insert("plan_hash", QString(64, 'e')); f.current.insert("review", review);
            Source::json(r, conflict(code), 409); return true;
        };
        c.approval()->approve(); QTRY_COMPARE(c.revision(), 9); QTRY_VERIFY(c.approval()->available());
        QVERIFY(c.approval()->error().contains("旧审批不会自动重试"));
        QCOMPARE(c.approval()->review().value("plan_hash").toString(), QString(64, 'e'));
        QTest::qWait(150); QCOMPARE(f.posts("/approvals"), 1);
    }
    void changedRevisionDuringReadDiscardsEntireBundle()
    {
        Fixture f; bool changed = false;
        f.intercept = [&](const Request &r) {
            if (!changed && r.path.endsWith("/graph")) { changed = true; auto old = graph(f.current); f.current = mission(A, 9, "running"); Source::json(r, old); return true; }
            return false;
        };
        MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        QTRY_VERIFY(c.synchronized()); QCOMPARE(c.revision(), 9); QCOMPARE(c.graph()->revision(), 9);
        QCOMPARE(c.graph()->get(0).value("status").toString(), "running"); QVERIFY(f.reads >= 4);
    }
    void malformedBundlePreservesOldModelsAndBlocksApproval()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A); QTRY_VERIFY(c.synchronized());
        f.current = mission(A, 9, "running");
        f.intercept = [&](const Request &r) { if (!r.path.endsWith("/graph")) return false; Source::json(r, {{"task_id", A}}); return true; };
        c.refresh(); QTRY_VERIFY(!c.error().isEmpty()); QVERIFY(!c.synchronized()); QVERIFY(!c.approval()->available());
        QCOMPARE(c.revision(), 8); QCOMPARE(c.graph()->revision(), 8);
    }
    void duplicateEventsAndGapRecoverThroughSnapshot()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); QSignalSpy events(&c, &MissionController::eventReceived);
        c.selectMission(A); QTRY_COMPARE(f.streams.size(), 1);
        f.current = mission(A, 9, "running"); f.streams.last().socket->write(missionFrame(9) + missionFrame(9) + missionFrame(8));
        QTRY_COMPARE(c.revision(), 9); QCOMPARE(events.count(), 1);
        f.current = mission(A, 11, "succeeded"); f.streams.last().socket->write(missionFrame(11));
        QTRY_COMPARE_WITH_TIMEOUT(c.revision(), 11, 5000); QTRY_VERIFY(c.synchronized());
        QCOMPARE(events.count(), 1); QCOMPARE(c.status(), "succeeded"); QCOMPARE(c.graph()->revision(), 11);
    }
    void disconnectRefreshesThenResumesAtSnapshotCursor_data()
    {
        QTest::addColumn<bool>("businessError");
        QTest::newRow("clears-connection-warning") << false;
        QTest::newRow("retains-later-business-error") << true;
    }
    void disconnectRefreshesThenResumesAtSnapshotCursor()
    {
        QFETCH(bool, businessError);
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A); QTRY_COMPARE(f.streams.size(), 1);
        // Complete an existing read bundle before the reconnect timer can request
        // another refresh. This used to leave the connection warning stuck forever.
        Request held;
        f.intercept = [&](const Request &r) {
            if (r.path.endsWith("/graph")) { held = r; return true; }
            if (r.path.endsWith("/artifacts/" + B.toUtf8())) { Source::json(r, conflict("unknown_artifact"), 404); return true; }
            return false;
        };
        f.current = mission(A, 10, "running"); c.refresh(); QTRY_VERIFY(held.socket);
        f.streams.last().socket->disconnectFromHost();
        QTRY_COMPARE(c.connectionState(), "recovering"); QVERIFY(!c.error().isEmpty());
        QString expectedError;
        if (businessError) {
            c.loadArtifact(B); expectedError = MissionProtocol::errorMessage("unknown_artifact", 404);
            QTRY_COMPARE(c.error(), expectedError);
        }
        f.intercept = {}; Source::json(held, graph(f.current));
        QTRY_COMPARE_WITH_TIMEOUT(f.streams.size(), 2, 5000);
        QCOMPARE(c.revision(), 10); QVERIFY(f.streams.last().headers.toLower().contains("last-event-id: " + A.toUtf8() + ":10"));
        QVERIFY(c.synchronized()); QCOMPARE(c.error(), expectedError);
    }
    void selectionCancelsOldReadsAndDestructionCancelsMutations()
    {
        Fixture f; QList<Request> held;
        f.intercept = [&](const Request &r) {
            if (r.path.contains(A.toUtf8()) && r.path.endsWith("/graph")) { held.append(r); return true; }
            return false;
        };
        auto *c = new MissionController; c->setBaseUrl(f.source.url()); c->selectMission(A); QTRY_COMPARE(held.size(), 1);
        f.current = mission(B); c->selectMission(B); QTRY_VERIFY(c->synchronized());
        Source::json(held.first(), graph(mission())); QTest::qWait(50);
        QCOMPARE(c->taskId(), B); QCOMPARE(c->graph()->taskId(), B);
        f.intercept = [&](const Request &r) { if (r.path.endsWith("/approvals")) { held.append(r); return true; } return false; };
        c->approval()->approve(); QTRY_COMPARE(held.size(), 2); QPointer<MissionController> guard(c); delete c; QVERIFY(!guard);
        QTRY_VERIFY(!held.last().socket || held.last().socket->state() == QAbstractSocket::UnconnectedState);
        Source::json(held.last(), mission(B, 9, "running"), 202); QTest::qWait(50); QCOMPARE(f.posts("/approvals"), 1);
    }
    void uncertainCancellationRefreshesAndDoesNotRetry()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A); QTRY_VERIFY(c.synchronized());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/cancel")) return false;
            EXPECT_EQ(r.body.value("expected_revision").toInteger(), 8);
            f.current = mission(A, 9, "cancelled"); Source::json(r, conflict("mission_internal"), 500); return true;
        };
        c.cancel(); c.cancel(); QTRY_COMPARE(c.status(), "cancelled"); QTRY_VERIFY(c.synchronized());
        QTest::qWait(100); QCOMPARE(f.posts("/cancel"), 1); QVERIFY(!c.busy());
    }
    void mutationDiscardsAnAlreadyPendingSnapshotFence()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        QTRY_VERIFY(c.synchronized());
        Request mutation, fence; int reads = 0;
        f.intercept = [&](const Request &r) {
            if (r.path.endsWith("/cancel")) { mutation = r; return true; }
            if (r.path.endsWith(A.toUtf8()) && ++reads == 2) { fence = r; return true; }
            return false;
        };
        c.cancel(); QTRY_VERIFY(mutation.socket);
        c.refresh(); QTRY_VERIFY(fence.socket);
        f.current = mission(A, 9, "cancelled");
        Source::json(mutation, f.current);
        QTRY_COMPARE(c.revision(), 9); QTRY_VERIFY(c.synchronized());
        Source::json(fence, mission()); QTest::qWait(100);
        QCOMPARE(c.revision(), 9); QCOMPARE(c.graph()->revision(), 9);
        QCOMPARE(c.status(), "cancelled"); QCOMPARE(f.posts("/cancel"), 1);
    }
    void snapshotFailureSignalCanDeleteControllerAndCancelReadRetry()
    {
        Fixture f;
        f.intercept = [](const Request &r) {
            if (!r.path.endsWith("/graph")) return false;
            Source::json(r, conflict("mission_internal"), 500); return true;
        };
        QPointer<MissionController> c = new MissionController; c->setBaseUrl(f.source.url());
        connect(c, &MissionController::errorChanged, this, [c] { if (c && !c->error().isEmpty()) delete c.data(); });
        c->selectMission(A); QTRY_VERIFY(c.isNull());
        const auto reads = f.reads; QTest::qWait(400); QCOMPARE(f.reads, reads);
    }
    void deliveryRequiresExplicitCurrentPreviewAndNeverRetriesStaleToken()
    {
        Fixture f; f.current = mission(A, 8, "succeeded"); MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        QTRY_VERIFY(c.synchronized()); QVERIFY(!c.delivery()->canConfirm());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            if (!r.body.contains("token")) Source::json(r, {{"mission", f.current}, {"delivery", QJsonObject{{"task_id", A}, {"token", QString(64, 'f')}}}});
            else { EXPECT_EQ(r.body.value("expected_revision").toInteger(), 8); EXPECT_EQ(r.body.value("token").toString(), QString(64, 'f'));
                f.current = mission(A, 9, "succeeded"); Source::json(r, conflict("delivery_changed"), 409); }
            return true;
        };
        c.delivery()->prepare("patch"); QTRY_VERIFY(c.delivery()->canConfirm()); QCOMPARE(f.posts("/delivery"), 1);
        c.delivery()->confirm(); QTRY_COMPARE(c.revision(), 9); QTRY_VERIFY(c.synchronized());
        QVERIFY(!c.delivery()->canConfirm()); c.delivery()->confirm(); QTest::qWait(100); QCOMPARE(f.posts("/delivery"), 2);
    }
    void recoveryUsesCurrentRevisionAndRequiresExplicitConfirmation()
    {
        Fixture f; f.current = mission(A, 8, "interrupted"); MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        QTRY_VERIFY(c.synchronized());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/resume")) return false;
            if (!r.body.contains("recovery_token")) {
                f.current = mission(A, 9, "interrupted"); f.current.insert("recovery_required", true);
                Source::json(r, {{"mission", f.current}, {"recovery", QJsonObject{{"task_id", A}, {"revision", 9},
                    {"token", QString(64, 'f')}, {"blocked_reason", QJsonValue::Null}, {"actions", QJsonArray{}}, {"checks", QJsonArray{}},
                    {"checkpoint_id", QJsonValue::Null}, {"requires_confirmation", true}, {"evidence", QJsonValue::Null}}}}, 202);
            } else {
                EXPECT_EQ(r.body.value("expected_revision").toInteger(), 9); EXPECT_EQ(r.body.value("recovery_token").toString(), QString(64, 'f'));
                f.current = mission(A, 10, "running"); Source::json(r, {{"mission", f.current}, {"recovery", QJsonValue::Null}}, 202);
            }
            return true;
        };
        c.prepareRecovery(); QTRY_COMPARE(c.revision(), 9); QTRY_VERIFY(c.synchronized()); QVERIFY(!c.recovery().isEmpty());
        QCOMPARE(f.posts("/resume"), 1); QVERIFY(!c.approval()->available()); c.confirmRecovery();
        QTRY_COMPARE(c.status(), "running"); QCOMPARE(c.revision(), 10); QCOMPARE(f.posts("/resume"), 2); QVERIFY(c.recovery().isEmpty());
    }
    void createSelectionAndClearPendingCreate()
    {
        Fixture f; Request held; bool hold = false;
        f.intercept = [&](const Request &r) { if (r.method != "POST" || !r.path.endsWith("/missions")) return false;
            if (hold) held = r; else Source::json(r, f.current, 202); return true; };
        MissionController c; c.setBaseUrl(f.source.url());
        c.createMission({{"repository_path", "C:/fixture"}, {"request", "修复错误"}});
        QTRY_VERIFY(c.synchronized()); QCOMPARE(c.taskId(), A); QCOMPARE(c.missions()->count(), 1);
        hold = true; c.createMission({{"repository_path", "C:/fixture"}, {"request", "另一任务"}}); QTRY_VERIFY(held.socket);
        c.clearSelection(); Source::json(held, mission(B), 202); QTest::qWait(100); QVERIFY(c.taskId().isEmpty()); QVERIFY(!c.busy());
    }
    void recoveryPreviewSurvivesItsOwnLateSseEvent()
    {
        Fixture f; f.current = mission(A, 8, "interrupted");
        MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A);
        QTRY_VERIFY(c.synchronized()); QTRY_COMPARE(f.streams.size(), 1);
        Request heldGraph; bool prepared = false;
        QSignalSpy events(&c, &MissionController::eventReceived);
        f.intercept = [&](const Request &r) {
            if (prepared && r.path.endsWith("/graph")) { heldGraph = r; return true; }
            if (!r.path.endsWith("/resume")) return false;
            if (!r.body.contains("recovery_token")) {
                prepared = true; f.current = mission(A, 9, "interrupted"); f.current.insert("recovery_required", true);
                Source::json(r, {{"mission", f.current}, {"recovery", QJsonObject{{"task_id", A}, {"revision", 9},
                    {"token", QString(64, 'f')}, {"blocked_reason", QJsonValue::Null}}}}, 202);
            } else {
                EXPECT_EQ(r.body.value("expected_revision").toInteger(), 9);
                prepared = false; f.current = mission(A, 10, "running");
                Source::json(r, {{"mission", f.current}, {"recovery", QJsonValue::Null}}, 202);
            }
            return true;
        };
        c.prepareRecovery(); QTRY_VERIFY(heldGraph.socket); QVERIFY(!c.recovery().isEmpty());
        f.streams.last().socket->write(missionFrame(9)); QTRY_COMPARE(events.count(), 1);
        QCOMPARE(c.recovery().value("token").toString(), QString(64, 'f'));
        Source::json(heldGraph, graph(f.current)); QTRY_VERIFY(c.synchronized());
        c.confirmRecovery(); QTRY_COMPARE(c.status(), "running"); QCOMPARE(f.posts("/resume"), 2);
    }
    void deliveryPreviewOnlyExpiresOnNewerRevision()
    {
        Fixture f; f.current = mission(A, 8, "succeeded");
        MissionController c; c.setBaseUrl(f.source.url()); c.selectMission(A); QTRY_VERIFY(c.synchronized());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            Source::json(r, {{"mission", f.current}, {"delivery", QJsonObject{{"task_id", A}, {"token", QString(64, 'f')}}}}); return true;
        };
        c.delivery()->prepare(); QTRY_VERIFY(c.delivery()->canConfirm());
        c.delivery()->invalidatePreviewBefore(8); QVERIFY(c.delivery()->canConfirm());
        c.delivery()->invalidatePreviewBefore(9); QVERIFY(!c.delivery()->canConfirm());
    }
    void missionSignalCanDeleteControllerDuringSnapshotCommit()
    {
        Fixture f; QPointer<MissionController> c = new MissionController;
        c->setBaseUrl(f.source.url());
        connect(c, &MissionController::missionChanged, this, [c] {
            if (c && c->revision() == 8) delete c.data();
        });
        c->selectMission(A); QTRY_VERIFY(c.isNull());
    }
    void modelResetCanSwitchMissionDuringSnapshotCommit()
    {
        Fixture f; MissionController c; c.setBaseUrl(f.source.url()); bool switched = false;
        connect(c.graph(), &QAbstractItemModel::modelReset, this, [&] {
            if (switched || c.graph()->taskId() != A) return;
            switched = true; f.current = mission(B, 11); c.selectMission(B);
        });
        c.selectMission(A); QTRY_VERIFY(switched); QTRY_VERIFY(c.synchronized());
        QCOMPARE(c.taskId(), B); QCOMPARE(c.graph()->taskId(), B); QCOMPARE(c.revision(), 11);
        QCOMPARE(c.approval()->review().value("task_id").toString(), B);
        QVERIFY(c.approval()->available());
    }
    void approvalResponseCanDeleteOwningMissionController()
    {
        Fixture f; QPointer<MissionController> c = new MissionController;
        c->setBaseUrl(f.source.url()); c->selectMission(A); QTRY_VERIFY(c->approval()->available());
        connect(c->approval(), &ApprovalController::snapshotReceived, this, [c] { delete c.data(); });
        c->approval()->approve(); QTRY_VERIFY(c.isNull()); QCOMPARE(f.posts("/approvals"), 1);
    }
    void approvalConflictSignalCanDeleteOwningMissionController()
    {
        Fixture f; QPointer<MissionController> c = new MissionController;
        c->setBaseUrl(f.source.url()); c->selectMission(A); QTRY_VERIFY(c->approval()->available());
        f.intercept = [](const Request &r) {
            if (!r.path.endsWith("/approvals")) return false;
            Source::json(r, conflict(), 409); return true;
        };
        connect(c->approval(), &ApprovalController::refreshRequired, this, [c] { delete c.data(); });
        c->approval()->approve(); QTRY_VERIFY(c.isNull()); QCOMPARE(f.posts("/approvals"), 1);
    }
    void deliveryResponseCanDeleteOwningMissionController()
    {
        Fixture f; f.current = mission(A, 8, "succeeded");
        QPointer<MissionController> c = new MissionController;
        c->setBaseUrl(f.source.url()); c->selectMission(A); QTRY_VERIFY(c->synchronized());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            Source::json(r, {{"mission", f.current}, {"delivery", QJsonObject{{"task_id", A}, {"token", QString(64, 'f')}}}});
            return true;
        };
        connect(c->delivery(), &DeliveryController::snapshotReceived, this, [c] { delete c.data(); });
        c->delivery()->prepare("patch"); QTRY_VERIFY(c.isNull()); QCOMPARE(f.posts("/delivery"), 1);
    }
    void deliveryResponseDoesNotExposeOldRevisionDuringChangedSignal()
    {
        Fixture f; f.current = mission(A, 8, "succeeded"); MissionController c;
        c.setBaseUrl(f.source.url()); c.selectMission(A); QTRY_VERIFY(c.synchronized());
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            if (r.body.contains("token")) f.current = mission(A, 9, "succeeded");
            Source::json(r, {{"mission", f.current}, {"delivery", QJsonObject{{"task_id", A}, {"token", QString(64, 'f')}}}});
            return true;
        };
        c.delivery()->prepare("patch"); QTRY_VERIFY(c.delivery()->canConfirm());
        bool oldRevisionEnabled = false;
        connect(c.delivery(), &DeliveryController::changed, this, [&] {
            if (f.current.value("current_revision").toInteger() == 9 && c.revision() == 8
                && c.delivery()->canPreview()) oldRevisionEnabled = true;
        });
        c.delivery()->confirm(); QTRY_COMPARE(c.revision(), 9); QTRY_VERIFY(c.synchronized());
        QVERIFY(!oldRevisionEnabled); QCOMPARE(f.posts("/delivery"), 2);
    }
};
QTEST_GUILESS_MAIN(MissionControllerTests)
#include "tst_mission_controller.moc"
