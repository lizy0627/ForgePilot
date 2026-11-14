#include "ClientUi.h"
#include "ConversationController.h"
#include "TaskController.h"
#include "workbench/MissionController.h"
#include "HttpFixture.h"
#include <QFile>
#include <QDir>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJSValue>
#include <QQmlApplicationEngine>
#include <QQuickItem>
#include <QQuickWindow>
#include <QtTest>
#include <algorithm>
#include <functional>

namespace {
const QString MissionId = "10000000-0000-4000-a000-000000000001";
const QString Validation0 = "10000000-0000-4000-a000-000000000005";
const QString Validation1 = "10000000-0000-4000-a000-000000000015";

QJsonObject fixtureJson(const QString &path)
{
    QFile file(QString(FORGEPILOT_SOURCE_DIR) + '/' + path);
    if (!file.open(QIODevice::ReadOnly)) qFatal("Cannot open workflow fixture");
    const auto document = QJsonDocument::fromJson(file.readAll());
    if (!document.isObject()) qFatal("Invalid workflow fixture JSON");
    return document.object();
}

QJsonObject nodeResult(const QJsonArray &artifacts, const QString &route = {})
{
    return {{"status", "succeeded"}, {"output_artifact_ids", artifacts},
        {"decision_summary", "Fixture node completed"},
        {"next_route", route.isEmpty() ? QJsonValue::Null : QJsonValue(route)},
        {"retryable", false}, {"failure_kind", QJsonValue::Null}, {"error_summary", QJsonValue::Null},
        {"metrics", QJsonObject{{"duration_ms", 12}, {"model_calls", 1}, {"tool_calls", 2}, {"total_tokens", 30}}}};
}

// Real HTTP, MissionController, GraphModel and ArtifactModel projections are used.
// Only the backend is a deterministic local fixture; no model or Git command runs.
struct Fixture {
    Source source;
    QJsonObject mission = fixtureJson("protocol/examples.json").value("mission_query_response").toObject();
    QJsonArray nodes, definitions;
    QJsonObject artifacts = fixtureJson("client/tests/fixtures/mission_artifacts.json");
    QList<Request> streams;
    QList<QJsonObject> creates, approvals, deliveries;
    Request pendingConfirmation;
    std::function<bool(const Request &)> intercept;

    Fixture()
    {
        const QList<QPair<QString, QString>> ids{
            {"draft_task_spec", "agent"}, {"analyst_fanout", "parallel_fanout"},
            {"root_cause_analysis", "agent"}, {"impact_test_analysis", "agent"},
            {"join_evidence", "join"}, {"generate_plan", "agent"},
            {"execution_approval", "human_gate"}, {"baseline_capture", "deterministic"},
            {"implementer", "agent"}, {"validation_0", "deterministic"},
            {"reviewer_0", "agent"}, {"repair_1", "agent"},
            {"validation_1", "deterministic"}, {"reviewer_1", "agent"},
            {"repair_2", "agent"}, {"validation_2", "deterministic"}, {"reviewer_2", "agent"},
            {"repair_3", "agent"}, {"validation_3", "deterministic"}, {"reviewer_3", "agent"},
            {"delivery_bundle", "delivery"}};
        QString previous;
        for (const auto &[id, type] : ids) {
            const auto agent = type == "agent" ? QJsonValue(id + ".v1") : QJsonValue::Null;
            definitions.append(QJsonObject{{"node_id", id}, {"node_type", type}, {"handler", id},
                {"dependencies", previous.isEmpty() ? QJsonArray{} : QJsonArray{previous}},
                {"agent_id", agent}, {"agent_definition_hash", QJsonValue::Null}, {"settings", QJsonObject{}}});
            nodes.append(QJsonObject{{"node_id", id}, {"node_type", type}, {"status", "pending"},
                {"attempt", 1}, {"result", QJsonValue::Null}});
            previous = id;
        }
        QJsonArray projected;
        QHash<QString, QString> producers{{"TaskSpec", "draft_task_spec"}, {"EvidenceBundle", "root_cause_analysis"},
            {"ExecutionPlan", "generate_plan"}, {"ChangeSet", "implementer"},
            {"ValidationReport", "validation_0"}, {"ReviewReport", "reviewer_0"}, {"DeliveryBundle", "delivery_bundle"}};
        for (const auto &value : artifacts.value("items").toArray()) {
            auto artifact = value.toObject();
            const auto producer = producers.value(artifact.value("artifact_type").toString());
            artifact["producer_node"] = producer;
            projected.append(artifact);
            setOutputs(producer, QJsonArray{artifact.value("artifact_id")});
        }
        artifacts["items"] = projected;
        setPhase("running", "implementer");
        source.handle = [this](const Request &request) {
            if (intercept && intercept(request)) return;
            if (request.path.startsWith("/api/v1/")) {
                Source::json(request, {{"items", QJsonArray{}}, {"total", 0}, {"next_cursor", QJsonValue::Null}});
            } else if (request.path == "/api/v2/missions" && request.method == "POST") {
                creates.append(request.body);
                mission["request"] = request.body.value("request");
                mission["repository_path"] = request.body.value("repository_path");
                Source::json(request, mission, 202);
            } else if (request.path.endsWith("/events")) {
                streams.append(request);
                Source::stream(request, ": fixture ready\n\n", false);
            } else if (request.path.endsWith("/graph")) Source::json(request, graph());
            else if (request.path.endsWith("/artifacts")) Source::json(request, artifacts);
            else if (request.path.endsWith("/diff")) {
                QJsonArray changes;
                for (const auto &a : artifacts.value("items").toArray())
                    if (a.toObject().value("artifact_type") == "ChangeSet") changes.append(a);
                Source::json(request, {{"task_id", MissionId}, {"patch", ""}, {"change_sets", changes}});
            } else if (request.path.endsWith("/validation")) {
                QJsonArray reports;
                for (const auto &a : artifacts.value("items").toArray())
                    if (a.toObject().value("artifact_type") == "ValidationReport") reports.append(a);
                Source::json(request, {{"task_id", MissionId}, {"items", reports},
                    {"latest", reports.isEmpty() ? QJsonValue::Null : reports.last()}});
            } else if (request.path.endsWith("/approvals") && request.method == "POST") {
                approvals.append(request.body);
                setPhase("running", "baseline_capture", revision() + 1);
                Source::json(request, mission, 202);
            } else if (request.path.endsWith("/delivery") && request.method == "POST") {
                deliveries.append(request.body);
                if (request.body.contains("token")) { pendingConfirmation = request; return; }
                setPhase("succeeded", "", revision() + 1);
                Source::json(request, {{"mission", mission}, {"delivery", QJsonObject{{"task_id", MissionId},
                    {"token", QString(64, 'f')}, {"mode", "preview"}, {"validated", true}, {"target_clean", true}}}});
            } else Source::json(request, mission);
        };
    }

    qint64 revision() const { return mission.value("current_revision").toInteger(); }
    void setOutputs(const QString &nodeId, const QJsonArray &outputs, const QString &route = {})
    {
        for (int i = 0; i < nodes.size(); ++i) {
            auto node = nodes[i].toObject();
            if (node.value("node_id") != nodeId) continue;
            node["result"] = nodeResult(outputs, route); nodes[i] = node; return;
        }
    }
    void setPhase(const QString &status, const QString &activeNode, qint64 nextRevision = 8)
    {
        mission["task_id"] = MissionId;
        mission["graph_id"] = "validated-delivery.v2";
        mission["graph_version"] = 2;
        mission["status"] = status;
        mission["current_revision"] = nextRevision;
        mission["last_event_id"] = MissionId + ':' + QString::number(nextRevision);
        mission["recovery_required"] = false;
        mission["review"] = QJsonValue::Null;
        if (status == "awaiting_human") {
            auto review = fixtureJson("protocol/examples.json").value("mission_query_response").toObject().value("review").toObject();
            for (const auto &a : artifacts.value("items").toArray()) {
                const auto artifact = a.toObject();
                if (artifact.value("artifact_type") == "TaskSpec") {
                    review["task_spec_artifact_id"] = artifact.value("artifact_id");
                    review["task_spec_hash"] = artifact.value("content_sha256");
                }
                if (artifact.value("artifact_type") == "ExecutionPlan") {
                    review["plan_artifact_id"] = artifact.value("artifact_id");
                    review["plan_hash"] = artifact.value("content_sha256");
                }
            }
            review["task_id"] = MissionId; review["revision"] = nextRevision;
            mission["review"] = review;
        }
        bool reachedActive = false;
        for (int i = 0; i < nodes.size(); ++i) {
            auto node = nodes[i].toObject();
            const bool active = node.value("node_id") == activeNode;
            node["status"] = active ? (status == "awaiting_human" ? "awaiting_human" : "running")
                : reachedActive ? "pending" : "succeeded";
            node["attempt"] = node.value("status") == "pending" ? 0 : 1;
            if (active) reachedActive = true;
            nodes[i] = node;
        }
    }
    QJsonObject graph() const
    {
        return {{"task_id", MissionId}, {"graph_id", mission.value("graph_id")}, {"graph_version", 2},
            {"definition_hash", QString(64, 'a')}, {"status", mission.value("status")},
            {"current_revision", mission.value("current_revision")}, {"nodes", nodes},
            {"definition", QJsonObject{{"graph_id", mission.value("graph_id")}, {"version", 2},
                {"nodes", definitions}, {"edges", QJsonArray{}}}}};
    }
    void addSecondValidation()
    {
        auto entries = artifacts.value("items").toArray();
        for (int i = 0; i < entries.size(); ++i) {
            auto first = entries[i].toObject();
            if (first.value("artifact_id") != Validation0) continue;
            auto content = first.value("content").toObject();
            content["summary"] = "Round zero validation result"; first["content"] = content; entries[i] = first;
            auto second = first;
            second["artifact_id"] = Validation1; second["producer_node"] = "validation_1";
            second["created_at"] = "2026-10-27T08:10:00Z";
            content["summary"] = "Round one validation result"; second["content"] = content;
            entries.append(second); break;
        }
        artifacts["items"] = entries;
        setOutputs("validation_0", QJsonArray{Validation0});
        setOutputs("validation_1", QJsonArray{Validation1});
    }
};

struct Ui {
    TaskController tasks;
    ConversationController conversations{&tasks};
    MissionController missions;
    QQmlApplicationEngine engine;
    QQuickWindow *window = nullptr;
    QStringList warnings;

    explicit Ui(Fixture &fixture, int width = 1600, int height = 900)
    {
        tasks.setBaseUrl(fixture.source.url()); missions.setBaseUrl(fixture.source.url());
        QObject::connect(&engine, &QQmlEngine::warnings, &engine, [this](const QList<QQmlError> &errors) {
            for (const auto &error : errors) warnings.append(error.toString());
        });
        engine.setInitialProperties({{"controller", QVariant::fromValue(&tasks)},
            {"conversations", QVariant::fromValue(&conversations)}, {"missions", QVariant::fromValue(&missions)},
            {"width", width}, {"height", height}});
    }
    bool load()
    {
        engine.load(QUrl::fromLocalFile(QString(FORGEPILOT_SOURCE_DIR) + "/client/Main.qml"));
        if (engine.rootObjects().isEmpty()) return false;
        window = qobject_cast<QQuickWindow *>(engine.rootObjects().first());
        return window && QTest::qWaitForWindowExposed(window);
    }
    QQuickItem *find(const QString &name, const QString &property = {}, const QString &value = {}, QQuickItem *root = nullptr) const
    {
        if (!root) root = window ? window->contentItem() : nullptr;
        if (!root) return nullptr;
        if (root->objectName() == name && (property.isEmpty() || root->property(property.toUtf8()).toString() == value)) return root;
        for (auto *child : root->childItems()) if (auto *found = find(name, property, value, child)) return found;
        return nullptr;
    }
    // Scroll the real viewports before clicking a delegate, including at minimum width.
    void reveal(QQuickItem *item) const
    {
        for (auto *parent = item ? item->parentItem() : nullptr; parent; parent = parent->parentItem()) {
            if (!parent->property("contentY").isValid() || !parent->property("contentHeight").isValid()) continue;
            const auto top = item->mapToItem(parent, QPointF(0, 0)).y();
            const auto bottom = top + item->height();
            auto offset = parent->property("contentY").toReal();
            if (top < 0) offset += top;
            else if (bottom > parent->height()) offset += bottom - parent->height();
            const auto maximum = qMax<qreal>(0, parent->property("contentHeight").toReal() - parent->height());
            parent->setProperty("contentY", qBound<qreal>(0, offset, maximum));
            QCoreApplication::processEvents();
        }
    }
    bool click(QQuickItem *item) const
    {
        if (!item || !item->isVisible() || !item->isEnabled()) return false;
        reveal(item); window->update(); QTest::qWait(20);
        QTest::mouseClick(window, Qt::LeftButton, Qt::NoModifier,
            item->mapToScene(QPointF(item->width() / 2, item->height() / 2)).toPoint());
        return true;
    }
    bool click(const QString &name) const { return click(find(name)); }
    bool capture(const QString &name) const
    {
        const auto directory = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (directory.isEmpty()) return true;
        if (!QDir().mkpath(directory)) return false;
        window->update(); QTest::qWait(60);
        return window->grabWindow().save(directory + '/' + name + ".png");
    }
    bool chooseNode(const QString &id) const
    {
        auto *node = find("workflowNode", "nodeId", id);
        return node && click(node);
    }
    bool expandStage(const QString &id) const
    {
        auto *stage = find("workflowStage", "stageId", id);
        if (!stage) return false;
        return stage->property("expanded").toBool() || click(find("workflowStageExpand", "stageId", id));
    }
    QStringList visibleArtifactIds() const
    {
        QStringList ids;
        std::function<void(QQuickItem *)> visit = [&](QQuickItem *item) {
            if (!item) return;
            if (item->objectName() == "workflowArtifactPanel" && item->isVisible()) {
                const auto value = item->property("artifact");
                const auto artifact = value.metaType() == QMetaType::fromType<QJSValue>()
                    ? value.value<QJSValue>().toVariant().toMap() : value.toMap();
                ids.append(artifact.value("artifactId", artifact.value("artifact_id")).toString());
            }
            for (auto *child : item->childItems()) visit(child);
        };
        visit(find("workflowDetails"));
        return ids;
    }
};
}

class WorkflowQmlTests : public QObject {
    Q_OBJECT
private slots:
    void initTestCase()
    {
        // The offscreen Windows platform does not discover the system font folder.
        // Register installed fonts for faithful Chinese screenshot evidence.
        const QDir fonts(QDir(qEnvironmentVariable("WINDIR", "C:/Windows")).filePath("Fonts"));
        for (const auto &name : {"msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "seguisym.ttf", "seguiemj.ttf"})
            if (QFile::exists(fonts.filePath(name))) QFontDatabase::addApplicationFont(fonts.filePath(name));
        configureClientUi(*qobject_cast<QGuiApplication *>(QCoreApplication::instance()));
    }
    void createsMissionAndCollapsesDemandWithoutLosingRequest()
    {
        Fixture fixture; Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QVERIFY(!page->property("demandCollapsed").toBool());
        auto *request = ui.find("missionRequestInput"); QVERIFY(request);
        auto *repository = ui.find("missionRepositoryInput"); QVERIFY(repository);
        QVERIFY(request->setProperty("text", "修复窗口关闭后后台任务仍运行"));
        QVERIFY(repository->setProperty("text", "D:/fixture/repository"));
        auto *create = ui.find("missionCreateButton"); QVERIFY(create);
        QTRY_VERIFY(create->isEnabled());
        QVERIFY(ui.click(create));
        QTRY_COMPARE(ui.missions.taskId(), MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_VERIFY(page->property("demandCollapsed").toBool());
        QCOMPARE(fixture.creates.size(), 1);
        QCOMPARE(fixture.creates.first().value("request").toString(), QString("修复窗口关闭后后台任务仍运行"));
        QCOMPARE(fixture.creates.first().value("repository_path").toString(), QString("D:/fixture/repository"));
        QVERIFY(ui.click("workflowDemandToggle"));
        QTRY_VERIFY(!page->property("demandCollapsed").toBool());
        QCOMPARE(request->property("text").toString(), QString("修复窗口关闭后后台任务仍运行"));
        QVERIFY(ui.click("workflowDemandToggle"));
        QTRY_VERIFY(page->property("demandCollapsed").toBool());
        QCOMPARE(fixture.creates.size(), 1);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void clickedNodeStaysSelectedWhenProgressAdvances()
    {
        Fixture fixture; Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QTRY_COMPARE(page->property("selectedNodeId").toString(), QString("implementer"));
        QVERIFY(ui.capture("workflow-overview-wide"));
        QVERIFY(ui.expandStage("analysis"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "root_cause_analysis"));
        QVERIFY(ui.chooseNode("root_cause_analysis"));
        QTRY_COMPARE(page->property("selectedNodeId").toString(), QString("root_cause_analysis"));
        QVERIFY(page->property("selectionPinned").toBool());
        QVERIFY(page->property("detailsVisible").toBool());
        QVERIFY(ui.find("workflowDetails"));
        QVERIFY(ui.capture("workflow-details-wide"));
        fixture.setPhase("running", "validation_1", 9);
        ui.missions.refresh();
        QTRY_COMPARE(ui.missions.revision(), 9);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(page->property("selectedNodeId").toString(), QString("root_cause_analysis"));
        QVERIFY(page->property("selectionPinned").toBool());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void completedStagesCanExpandAndCollapse()
    {
        Fixture fixture; fixture.setPhase("succeeded", ""); Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *stage = ui.find("workflowStage", "stageId", "analysis"); QVERIFY(stage);
        auto *expand = ui.find("workflowStageExpand", "stageId", "analysis"); QVERIFY(expand);
        const bool initiallyExpanded = stage->property("expanded").toBool();
        QVERIFY(ui.click(expand));
        QTRY_COMPARE(stage->property("expanded").toBool(), !initiallyExpanded);
        QVERIFY(ui.click(expand));
        QTRY_COMPARE(stage->property("expanded").toBool(), initiallyExpanded);
        QVERIFY(ui.expandStage("analysis"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "root_cause_analysis"));
        QVERIFY(ui.find("workflowNode", "nodeId", "root_cause_analysis")->isVisible());
        QVERIFY(ui.chooseNode("root_cause_analysis"));
        QCOMPARE(ui.find("workflowPage")->property("selectedNodeId").toString(), QString("root_cause_analysis"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void validationArtifactsStayWithTheirOwnRound()
    {
        Fixture fixture; fixture.addSecondValidation(); fixture.setPhase("succeeded", ""); Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.expandStage("verification"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "validation_0"));
        QVERIFY(ui.chooseNode("validation_0"));
        QTRY_COMPARE(ui.visibleArtifactIds(), QStringList{Validation0});
        QVERIFY(ui.chooseNode("validation_1"));
        QTRY_COMPARE(ui.visibleArtifactIds(), QStringList{Validation1});
        QVERIFY(ui.chooseNode("validation_0"));
        QTRY_COMPARE(ui.visibleArtifactIds(), QStringList{Validation0});
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void repairRoutingShowsTheOutcomeWithoutChangingBackendState()
    {
        Fixture fixture;
        fixture.setPhase("running", "repair_1");
        fixture.setOutputs("validation_0", QJsonArray{Validation0}, "repair");
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.expandStage("verification"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "validation_0"));
        QVERIFY(ui.chooseNode("validation_0"));
        QTRY_VERIFY(ui.find("workflowDetails"));
        QTRY_COMPARE(ui.find("workflowDetails")->property("status").toString(), QString("needs_repair"));
        bool checkedValidationNode = false;
        for (int i = 0; i < ui.missions.graph()->count(); ++i) {
            const auto node = ui.missions.graph()->get(i);
            if (node.value("nodeId").toString() != "validation_0") continue;
            QCOMPARE(node.value("status").toString(), QString("succeeded"));
            QCOMPARE(node.value("result").toMap().value("next_route").toString(), QString("repair"));
            checkedValidationNode = true;
        }
        QVERIFY(checkedValidationNode);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void approvalUsesTheCurrentFrozenControllerPackage()
    {
        Fixture fixture; fixture.setPhase("awaiting_human", "execution_approval"); Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QTRY_COMPARE(page->property("selectedNodeId").toString(), QString("execution_approval"));
        QVERIFY(ui.click("workflowAttentionButton"));
        QTRY_VERIFY(ui.find("missionApproveButton"));
        auto *approve = ui.find("missionApproveButton");
        QTRY_VERIFY(approve->isEnabled());
        const auto frozen = fixture.mission.value("review").toObject();
        QVERIFY(ui.click(approve));
        QTRY_COMPARE(fixture.approvals.size(), 1);
        const auto body = fixture.approvals.first();
        QCOMPARE(body.value("expected_revision").toInteger(), 8);
        QCOMPARE(body.value("decision").toString(), QString("approved"));
        QCOMPARE(body.value("task_spec_hash"), frozen.value("task_spec_hash"));
        QCOMPARE(body.value("plan_hash"), frozen.value("plan_hash"));
        QCOMPARE(body.value("scope_hash"), frozen.value("scope_hash"));
        QTRY_COMPARE(ui.missions.status(), QString("running"));
        ui.missions.approval()->approve();
        QTest::qWait(30);
        QCOMPARE(fixture.approvals.size(), 1);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void interruptedApprovalCanPreviewAndConfirmRecoveryWithoutResumeAction()
    {
        Fixture fixture; fixture.setPhase("awaiting_human", "execution_approval");
        fixture.mission["recovery_required"] = true;
        QList<QJsonObject> resumePosts;
        fixture.intercept = [&](const Request &request) {
            if (!request.path.endsWith("/resume")) return false;
            resumePosts.append(request.body);
            if (!request.body.contains("recovery_token")) {
                fixture.setPhase("awaiting_human", "execution_approval", 9);
                fixture.mission["recovery_required"] = true;
                Source::json(request, {{"mission", fixture.mission},
                    {"recovery", QJsonObject{{"task_id", MissionId}, {"revision", 9},
                        {"token", QString(64, 'e')}, {"blocked_reason", QJsonValue::Null},
                        {"checks", QJsonArray{"workspace_unchanged"}},
                        {"actions", QJsonArray{QJsonObject{{"node_id", "execution_approval"}, {"action", "execution_approval"}}}}}}}, 202);
            } else {
                fixture.setPhase("running", "baseline_capture", 10);
                Source::json(request, {{"mission", fixture.mission}, {"recovery", QJsonValue::Null}}, 202);
            }
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(ui.missions.status(), QString("awaiting_human"));
        QVERIFY(!ui.missions.approval()->available());
        bool checkedApprovalNode = false;
        for (int i = 0; i < ui.missions.graph()->count(); ++i) {
            const auto node = ui.missions.graph()->get(i);
            if (node.value("nodeId").toString() != "execution_approval") continue;
            QVERIFY(node.value("actions").toStringList().isEmpty());
            checkedApprovalNode = true;
        }
        QVERIFY(checkedApprovalNode);
        QVERIFY(ui.click("workflowAttentionButton"));
        QTRY_VERIFY(ui.find("workflowRecoveryPreview"));
        QTRY_VERIFY(ui.find("workflowRecoveryPreview")->isVisible());
        QVERIFY(ui.find("workflowRecoveryPreview")->isEnabled());
        QVERIFY(ui.click("workflowRecoveryPreview"));
        QTRY_COMPARE(ui.missions.revision(), 9);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(resumePosts.size(), 1);
        QCOMPARE(resumePosts.first().value("expected_revision").toInteger(), 8);
        QVERIFY(!resumePosts.first().contains("recovery_token"));
        QTRY_VERIFY(ui.find("workflowRecoveryConfirm"));
        QTRY_VERIFY(ui.find("workflowRecoveryConfirm")->isVisible());
        QVERIFY(ui.find("workflowRecoveryConfirm")->isEnabled());
        QVERIFY(ui.click("workflowRecoveryConfirm"));
        QTRY_COMPARE(ui.missions.revision(), 10);
        QTRY_COMPARE(ui.missions.status(), QString("running"));
        QCOMPARE(resumePosts.size(), 2);
        QCOMPARE(resumePosts.last().value("expected_revision").toInteger(), 9);
        QCOMPARE(resumePosts.last().value("recovery_token").toString(), QString(64, 'e'));
        QCOMPARE(fixture.approvals.size(), 0);
        QVERIFY(ui.missions.recovery().isEmpty());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void deliveryRequiresPreviewAndExplicitConfirmation()
    {
        Fixture fixture; fixture.setPhase("succeeded", ""); Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.expandStage("delivery"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "delivery_bundle"));
        QVERIFY(ui.chooseNode("delivery_bundle"));
        QTRY_VERIFY(ui.find("missionDeliveryPreviewButton"));
        auto *preview = ui.find("missionDeliveryPreviewButton");
        QTRY_VERIFY(preview->isEnabled());
        QVERIFY(ui.click(preview));
        QTRY_VERIFY(ui.missions.delivery()->canConfirm());
        QCOMPARE(fixture.deliveries.size(), 1);
        QCOMPARE(fixture.deliveries.first().value("expected_revision").toInteger(), 8);
        QVERIFY(!fixture.deliveries.first().contains("token"));
        QVERIFY(ui.click("missionDeliveryConfirmButton"));
        QTRY_VERIFY(ui.find("missionDeliveryDialogConfirmButton"));
        QCOMPARE(fixture.deliveries.size(), 1);
        QVERIFY(ui.click("missionDeliveryDialogConfirmButton"));
        QTRY_VERIFY(fixture.pendingConfirmation.socket);
        QCOMPARE(fixture.deliveries.size(), 2);
        QCOMPARE(fixture.deliveries.last().value("expected_revision").toInteger(), 9);
        QCOMPARE(fixture.deliveries.last().value("token").toString(), QString(64, 'f'));
        ui.missions.delivery()->confirm();
        QTest::qWait(30);
        QCOMPARE(fixture.deliveries.size(), 2);
        fixture.setPhase("succeeded", "", 10);
        Source::json(fixture.pendingConfirmation, {{"mission", fixture.mission},
            {"delivery", QJsonObject{{"mode", "patch"}, {"patch", QJsonObject{{"path", "D:/fixture/change.patch"}}}}}});
        QTRY_COMPARE(ui.missions.delivery()->result().value("mode").toString(), QString("patch"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void narrowWindowReturnsFromDetailsToTheSameSelectedNode()
    {
        Fixture fixture; Ui ui(fixture, 860, 640);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QVERIFY(page->property("compactLayout").toBool());
        QVERIFY(ui.expandStage("implementation"));
        QVERIFY(ui.capture("workflow-graph-narrow"));
        QVERIFY(ui.chooseNode("implementer"));
        QTRY_VERIFY(page->property("detailsVisible").toBool());
        auto *back = ui.find("workflowDetailsClose"); QVERIFY(back);
        QTRY_VERIFY(back->isVisible());
        QVERIFY(ui.capture("workflow-details-narrow"));
        QVERIFY(ui.click(back));
        QTRY_VERIFY(!page->property("detailsVisible").toBool());
        QCOMPARE(page->property("selectedNodeId").toString(), QString("implementer"));
        QVERIFY(ui.find("workflowGraph")->isVisible());
        QVERIFY(ui.chooseNode("implementer"));
        QTRY_VERIFY(page->property("detailsVisible").toBool());
        QCOMPARE(page->property("selectedNodeId").toString(), QString("implementer"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void keyboardFocusRevealsLaterRepairNodesInNarrowWindow()
    {
        Fixture fixture; fixture.setPhase("succeeded", ""); Ui ui(fixture, 860, 640);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.expandStage("verification"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "reviewer_1"));
        auto *earlier = ui.find("workflowNode", "nodeId", "reviewer_1");
        earlier->forceActiveFocus(Qt::TabFocusReason);
        QTRY_VERIFY(earlier->hasActiveFocus());
        QTest::keyClick(ui.window, Qt::Key_Tab);
        auto focusedNodeVisible = [&] {
            auto *focused = qobject_cast<QQuickItem *>(ui.window->activeFocusItem());
            if (!focused || focused->objectName() != "workflowNode" || focused->property("nodeId").toString() != "repair_2") return false;
            for (auto *parent = focused->parentItem(); parent; parent = parent->parentItem()) {
                if (!parent->clip()) continue;
                const auto rect = focused->mapRectToItem(parent, QRectF(0, 0, focused->width(), focused->height()));
                if (rect.left() < -1 || rect.top() < -1 || rect.right() > parent->width() + 1 || rect.bottom() > parent->height() + 1) return false;
            }
            return true;
        };
        auto focusDiagnostic = [&] {
            auto *focused = ui.window->activeFocusItem();
            if (!focused) return QString("No active focus item");
            QString detail = "Focused " + focused->objectName() + " node=" + focused->property("nodeId").toString();
            for (auto *parent = focused->parentItem(); parent; parent = parent->parentItem()) {
                if (!parent->clip()) continue;
                const auto rect = focused->mapRectToItem(parent, QRectF(0, 0, focused->width(), focused->height()));
                detail += QString(" | clip %1 %2x%3 item=(%4,%5,%6,%7)").arg(parent->metaObject()->className())
                    .arg(parent->width()).arg(parent->height()).arg(rect.x()).arg(rect.y()).arg(rect.width()).arg(rect.height());
            }
            return detail;
        };
        QTRY_VERIFY2(focusedNodeVisible(), qPrintable(focusDiagnostic()));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
};
QTEST_MAIN(WorkflowQmlTests)
#include "tst_workflow_qml.moc"
