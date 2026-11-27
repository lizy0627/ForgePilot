#include "ClientUi.h"
#include "ConversationController.h"
#include "TaskController.h"
#include "McpController.h"
#include "RagController.h"
#include "RuntimeConfigController.h"
#include "workbench/MissionController.h"
#include "HttpFixture.h"
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QCryptographicHash>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJSValue>
#include <QQmlApplicationEngine>
#include <QQuickItem>
#include <QQuickWindow>
#include <QSaveFile>
#include <QScopeGuard>
#include <QTemporaryDir>
#include <QThread>
#include <QUrlQuery>
#include <QtTest>
#include <algorithm>
#include <functional>

namespace {
int runtimeConfigFixture()
{
    QFile input; if (!input.open(stdin, QIODevice::ReadOnly)) return 3;
    const auto request = QJsonDocument::fromJson(input.readAll()).object();
    const auto action = request.value("action").toString();
    const auto path = QCoreApplication::arguments().last();
    auto fields = QJsonObject{{"base_url", "http://127.0.0.1:1/v1"}, {"port", 8765},
        {"chat_model", "fixture-chat"}, {"task_model", "fixture-task"}, {"review_model", "fixture-review"},
        {"summary_model", "fixture-summary"}, {"task_model_family", "writer"}, {"review_model_family", "reviewer"},
        {"cloud_model_allowed", false}, {"embedding_enabled", false}};
    QFile previous(path);
    bool configured = previous.exists();
    if (previous.open(QIODevice::ReadOnly)) fields = QJsonDocument::fromJson(previous.readAll()).object();
    previous.close();
    bool ok = true;
    if (action == "save") {
        const auto proposed = request.value("fields").toObject();
        ok = proposed.value("task_model") != "reject" && request.value("api_key") == "private-pipe-key";
        if (ok) {
            QSaveFile saved(path); if (!saved.open(QIODevice::WriteOnly)) return 4;
            saved.write(QJsonDocument(proposed).toJson()); if (!saved.commit()) return 5;
            fields = proposed; configured = true;
        }
    }
    QThread::msleep(80);
    const QJsonObject result{{"ok", ok}, {"configured", configured}, {"fields", fields},
        {"has_api_key", configured}, {"has_embedding_key", false}, {"config_revision", "fixture-revision"},
        {"restart_required", configured}, {"restart_allowed", configured}, {"active_count", 0},
        {"notice", ok ? "配置已保存，受控重启后生效。" : "任务模型配置无效，旧配置保留。"},
        {"errors", ok ? QJsonArray{} : QJsonArray{QJsonObject{{"purpose", "task"}, {"code", "llm_config"}, {"message", "任务模型配置无效"}}}}};
    QFile output; if (!output.open(stdout, QIODevice::WriteOnly)) return 6;
    output.write(QJsonDocument(result).toJson(QJsonDocument::Compact)); output.flush(); return 0;
}
const QString MissionId = "10000000-0000-4000-a000-000000000001";
const QString McpId = "20000000-0000-4000-a000-000000000001";
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

bool showsText(QQuickItem *item, const QString &text)
{
    if (!item || !item->isVisible()) return false;
    if (item->property("text").toString().contains(text)) return true;
    for (auto *child : item->childItems())
        if (showsText(child, text)) return true;
    return false;
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
    int previewCount = 0;
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
            if (request.method == "GET" && request.path.startsWith("/api/v2/missions?")) {
                const QJsonArray items = creates.isEmpty() ? QJsonArray{} : QJsonArray{mission};
                Source::json(request, {{"items", items}, {"total", items.size()}, {"next_cursor", QJsonValue::Null}});
            } else if (request.path == "/api/v1/mcp/servers") {
                Source::json(request, {{"disabled", false}, {"items", QJsonArray{
                    QJsonObject{{"server_id", McpId}, {"name", "本地资料"}, {"revision", 1}, {"enabled", true}, {"selected_tool_count", 1}},
                    QJsonObject{{"server_id", Validation0}, {"name", "仅聊天"}, {"revision", 1}, {"enabled", true}, {"selected_tool_count", 1}},
                    QJsonObject{{"server_id", Validation1}, {"name", "缺少只读声明"}, {"revision", 1}, {"enabled", true}, {"selected_tool_count", 1}}}}});
            } else if (request.path.startsWith("/api/v1/mcp/servers/")) {
                const auto serverId = QString::fromUtf8(request.path.mid(QByteArray("/api/v1/mcp/servers/").size()));
                const bool task = serverId != Validation0;
                Source::json(request, {{"server_id", serverId}, {"name", serverId == McpId ? "本地资料" : "其他服务"},
                    {"revision", 1}, {"enabled", true}, {"effect", "read"}, {"modes", task ? QJsonArray{"task"} : QJsonArray{"chat"}},
                    {"selected_tools", QJsonArray{"lookup"}}, {"tools", QJsonArray{
                        QJsonObject{{"name", "lookup"}, {"compatible", true}, {"annotations", QJsonObject{{"readOnlyHint", serverId != Validation1}, {"destructiveHint", false}}}},
                        QJsonObject{{"name", "write"}, {"compatible", false}, {"annotations", QJsonObject{{"readOnlyHint", false}}}}}}});
            } else if (request.path.startsWith("/api/v1/")) {
                Source::json(request, {{"items", QJsonArray{}}, {"total", 0}, {"next_cursor", QJsonValue::Null}});
            } else if (request.path == "/api/v2/skills/recommendations" && request.method == "POST") {
                Source::json(request, {{"recommendations", QJsonArray{}}});
            } else if (request.path.endsWith("/skills") && request.method == "GET") {
                Source::json(request, {{"task_id", MissionId}, {"selected_skills", QJsonArray{}}, {"injections", QJsonArray{}}});
            } else if (request.path == "/api/v2/missions/preview" && request.method == "POST") {
                const auto paths = request.body.value("baseline_paths").toArray();
                const auto token = QString::number(++previewCount, 16).rightJustified(64, '0');
                Source::json(request, {{"preview_token", token}, {"base_commit", QString(40, 'a')},
                    {"source_head", QString(40, 'a')}, {"baseline_paths", paths}, {"changed_files", QJsonArray{"main.cpp"}},
                    {"patch", paths.isEmpty() ? "" : "--- a/main.cpp\n+++ b/main.cpp\n@@ -1 +1 @@\n-old\n+selected\n"},
                    {"build", QJsonObject{{"source", "default"}, {"note", "受控构建目录"}}}});
            } else if (request.path.startsWith("/api/v2/missions/previews/") && request.method == "DELETE") {
                Source::json(request, {{"discarded", true}});
            } else if (request.path == "/api/v2/missions" && request.method == "POST") {
                creates.append(request.body);
                mission["request"] = request.body.value("request");
                mission["repository_path"] = request.body.value("repository_path");
                mission["use_rag"] = request.body.value("use_rag").toBool();
                mission["mcp_server_ids"] = request.body.value("mcp_server_ids").toArray();
                const bool selected = !mission["mcp_server_ids"].toArray().isEmpty();
                mission["tool_context"] = QJsonObject{
                    {"rag", QJsonObject{{"requested", mission["use_rag"]}, {"enabled", mission["use_rag"]}, {"used", mission["use_rag"]},
                        {"fallback_reasons", mission["use_rag"].toBool() ? QJsonArray{"vector_unavailable"} : QJsonArray{}}}},
                    {"clangd", QJsonObject{{"enabled", false}, {"used", false}, {"fallback_reasons", QJsonArray{"clangd_unavailable"}}}},
                    {"mcp", QJsonObject{{"server_ids", mission["mcp_server_ids"]}, {"used", selected}, {"fallback_reasons", QJsonArray{}},
                        {"servers", selected ? QJsonArray{QJsonObject{{"server_id", McpId}, {"name", "本地资料"}, {"tools", QJsonArray{"lookup"}}}} : QJsonArray{}}}}};
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
    McpController mcp{&tasks};
    RagController rag;
    QQmlApplicationEngine engine;
    QQuickWindow *window = nullptr;
    QStringList warnings;

    explicit Ui(Fixture &fixture, int width = 1600, int height = 900, RuntimeConfigController *runtime = nullptr)
        : Ui(fixture.source.url(), width, height, runtime) {}

    explicit Ui(const QUrl &url, int width = 1600, int height = 900, RuntimeConfigController *runtime = nullptr)
    {
        tasks.setBaseUrl(url); missions.setBaseUrl(url);
        QObject::connect(&engine, &QQmlEngine::warnings, &engine, [this](const QList<QQmlError> &errors) {
            for (const auto &error : errors) warnings.append(error.toString());
        });
        engine.setInitialProperties({{"controller", QVariant::fromValue(&tasks)},
            {"conversations", QVariant::fromValue(&conversations)}, {"missions", QVariant::fromValue(&missions)},
            {"mcp", QVariant::fromValue(&mcp)}, {"rag", QVariant::fromValue(&rag)},
            {"runtimeConfig", QVariant::fromValue(runtime)},
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
        // Let newly published sections finish layout before measuring the scroll range.
        window->update(); QTest::qWait(20); reveal(item); window->update(); QTest::qWait(20);
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
        QPointer<QQuickItem> stage, button;
        // Snapshot commit precedes the QML callLater refresh and delegate layout.
        // Wait for a clickable delegate, then deliver exactly one real click.
        if (!QTest::qWaitFor([&] {
            stage = find("workflowStage", "stageId", id);
            button = find("workflowStageExpand", "stageId", id);
            return stage && (stage->property("expanded").toBool()
                || (button && button->isVisible() && button->isEnabled()
                    && button->width() > 0 && button->height() > 0));
        }, 5000)) return false;
        return stage && (stage->property("expanded").toBool() || click(button));
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
    void historyNavigationPreservesTheDraft_data()
    {
        QTest::addColumn<int>("width");
        QTest::addColumn<bool>("docked");
        QTest::newRow("desktop") << 1320 << true;
        QTest::newRow("minimum") << 860 << false;
    }
    void historyNavigationPreservesTheDraft()
    {
        QFETCH(int, width);
        QFETCH(bool, docked);
        Fixture fixture;
        fixture.intercept = [&](const Request &request) {
            if (request.method != "GET" || !request.path.startsWith("/api/v2/missions?")) return false;
            Source::json(request, {{"items", QJsonArray{fixture.mission}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
            return true;
        };
        Ui ui(fixture, width, 640);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QTRY_COMPARE(page->property("historyVisible").toBool(), docked);
        auto *goal = ui.find("missionRequestInput"); QVERIFY(goal && goal->isVisible());
        auto *repository = ui.find("missionRepositoryInput"); QVERIFY(repository && repository->isVisible());
        auto *rag = ui.find("missionUseRag"); QVERIFY(rag && !rag->isVisible());
        auto *graph = ui.find("workflowGraph"); QVERIFY(graph && !graph->isVisible());
        if (docked) {
            auto *sidebar = ui.find("workflowHistorySidebar"); QVERIFY(sidebar && sidebar->isVisible());
            QVERIFY(sidebar->mapToScene(QPointF(sidebar->width(), 0)).x() <= goal->mapToScene(QPointF()).x());
        }
        QVERIFY(ui.capture(docked ? "workflow-compose-wide" : "workflow-compose-narrow"));
        goal->setProperty("text", "保留尚未提交的任务目标");
        repository->setProperty("text", "D:/fixture/draft");
        if (docked) {
            QVERIFY(ui.click("workflowHistoryToggle"));
            QTRY_VERIFY(!page->property("historyVisible").toBool());
        }
        QVERIFY(ui.click("workflowHistoryToggle"));
        QTRY_VERIFY(page->property("historyVisible").toBool());
        QTRY_VERIFY(ui.find("workflowHistorySidebar"));
        auto *sidebar = ui.find("workflowHistorySidebar");
        QTRY_VERIFY(sidebar->isVisible() && sidebar->mapToScene(QPointF()).x() >= -1);
        QTRY_VERIFY(ui.find("missionTaskRow", "taskId", MissionId));
        QVERIFY(ui.click(ui.find("missionTaskRow", "taskId", MissionId)));
        QTRY_COMPARE(ui.missions.taskId(), MissionId);
        QTRY_VERIFY(graph->isVisible());
        QVERIFY(!goal->isVisible());
        QTRY_COMPARE(page->property("historyVisible").toBool(), docked);
        QVERIFY(ui.click("workflowDemandToggle"));
        QTRY_VERIFY(goal->isVisible());
        QVERIFY(!graph->isVisible());
        QCOMPARE(goal->property("text").toString(), QString("保留尚未提交的任务目标"));
        QCOMPARE(repository->property("text").toString(), QString("D:/fixture/draft"));
        if (docked) {
            ui.window->resize(860, 640);
            QTRY_VERIFY(page->property("compactHistory").toBool());
            QTRY_VERIFY(!page->property("historyVisible").toBool());
            QVERIFY(ui.click("workflowHistoryToggle"));
            QTRY_VERIFY(sidebar->isVisible() && sidebar->mapToScene(QPointF()).x() >= -1);
            ui.window->resize(width, 640);
            QTRY_VERIFY(!page->property("compactHistory").toBool());
            QTRY_VERIFY(sidebar->isVisible());
            // An orphaned modal overlay would intercept this click after the drawer redocks.
            QTRY_VERIFY(ui.click(goal) && goal->hasActiveFocus());
            QTest::keyClick(ui.window, Qt::Key_End, Qt::ControlModifier);
            QTest::keyClick(ui.window, Qt::Key_X);
            QCOMPARE(goal->property("text").toString(), QString("保留尚未提交的任务目标x"));
        }
        QVERIFY(fixture.creates.isEmpty());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void openingMissionByIdShowsErrorsInEmptyWorkbench()
    {
        Fixture fixture;
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QVERIFY(ui.click("missionHistoryFiltersToggle"));
        auto *input = ui.find("missionIdInput"); QVERIFY(input && input->isVisible());
        auto *open = ui.find("missionOpenById"); QVERIFY(open);
        QVERIFY(ui.missions.taskId().isEmpty());
        input->setProperty("text", "invalid-id");
        QVERIFY(ui.click(open));
        QCOMPARE(ui.missions.error(), QStringLiteral("任务标识无效。"));
        auto *message = ui.find({}, "text", ui.missions.error());
        QVERIFY(message && message->isVisible());

        input->setProperty("text", MissionId);
        QVERIFY(ui.click(open));
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(ui.missions.taskId(), MissionId);
        QVERIFY(ui.missions.error().isEmpty());
        QVERIFY(!message->isVisible());
    }
    void productionWorkflowLatencyProfile()
    {
        Fixture fixture;
        // A realistic large immutable ChangeSet stays open while other nodes advance.
        const QString largeDiff = QString("+ local offline large diff payload 0123456789\n").repeated(20000);
        auto artifacts = fixture.artifacts.value("items").toArray();
        for (int i = 0; i < artifacts.size(); ++i) {
            auto artifact = artifacts[i].toObject();
            if (artifact.value("artifact_type") != "ChangeSet") continue;
            auto content = artifact.value("content").toObject();
            auto files = content.value("files").toArray();
            auto file = files.first().toObject(); file["unified_diff"] = largeDiff;
            files[0] = file; content["files"] = files; artifact["content"] = content;
            artifact["content_sha256"] = QString::fromLatin1(QCryptographicHash::hash(
                QJsonDocument(content).toJson(QJsonDocument::Compact), QCryptographicHash::Sha256).toHex());
            artifacts[i] = artifact;
        }
        fixture.artifacts["items"] = artifacts;
        fixture.setPhase("running", "reviewer_0");
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QTRY_VERIFY(!page->property("selectedNodeId").toString().isEmpty());
        QVERIFY(ui.expandStage("implementation"));
        QVERIFY(ui.expandStage("verification"));
        QVERIFY(ui.chooseNode("implementer"));
        QTRY_VERIFY(ui.find("workflowNodeDiff"));
        auto *diff = ui.find("workflowNodeDiff");
        QCOMPARE(diff->property("value").toString(), largeDiff);
        auto rawClick = [&](QQuickItem *item) {
            if (!item || !item->isVisible() || !item->isEnabled()) return false;
            QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
                item->mapToScene(QPointF(item->width() / 2, item->height() / 2)).toPoint());
            return true;
        };
        auto render = [&] {
            QCoreApplication::processEvents(); ui.window->update();
            return !ui.window->grabWindow().isNull();
        };
        auto measure = [](QElapsedTimer &timer) { return timer.nsecsElapsed() / 1000000.0; };
        QJsonObject results;
        auto record = [&](const QString &name, QList<double> samples) {
            std::sort(samples.begin(), samples.end());
            const auto median = (samples[(samples.size() - 1) / 2] + samples[samples.size() / 2]) / 2;
            const auto p95 = samples[(95 * samples.size() + 99) / 100 - 1];
            results[name] = QJsonObject{{"samples", samples.size()}, {"p50_ms", median}, {"p95_ms", p95}};
            qInfo().noquote() << "workflow_profile" << name << "p50_ms=" << median << "p95_ms=" << p95;
        };
        QList<double> modes, nodes, pages, refreshes, updates;
        QElapsedTimer timer;
        for (int i = 0; i < 20; ++i) {
            timer.start(); QVERIFY(rawClick(ui.find(i % 2 ? "agentWorkbenchModeButton" : "freeChatModeButton")));
            QCOMPARE(page->isVisible(), bool(i % 2)); QVERIFY(render()); modes.append(measure(timer));
        }
        for (int i = 0; i < 20; ++i) {
            const QString node = i % 2 ? "implementer" : "reviewer_0";
            auto *button = ui.find("workflowNode", "nodeId", node); QVERIFY(button); ui.reveal(button);
            timer.start(); QVERIFY(rawClick(button));
            QCOMPARE(page->property("selectedNodeId").toString(), node); QVERIFY(render()); nodes.append(measure(timer));
        }
        diff = ui.find("workflowNodeDiff"); QVERIFY(diff);
        for (int i = 0; i < 20; ++i) {
            auto *button = ui.find("workflowNextOutput", {}, {}, diff); QVERIFY(button); ui.reveal(button);
            const auto offset = diff->property("offset").toInt();
            timer.start(); QVERIFY(rawClick(button));
            QCOMPARE(diff->property("offset").toInt(), offset + 12000); QVERIFY(render()); pages.append(measure(timer));
        }
        const auto retainedOffset = diff->property("offset").toInt();
        QPointer<QQuickItem> retainedDiff(diff);
        for (int i = 0; i < 20; ++i) {
            timer.start(); QVERIFY(QMetaObject::invokeMethod(page, "refreshView"));
            refreshes.append(measure(timer));
        }
        for (int i = 0; i < 12; ++i) {
            const auto revision = 9 + i;
            fixture.setPhase("running", i % 2 ? "validation_1" : "repair_1", revision);
            QTRY_VERIFY(!fixture.streams.isEmpty() && fixture.streams.last().socket);
            const QJsonObject event{{"event_id", MissionId + ':' + QString::number(revision)}, {"task_id", MissionId},
                {"task_revision", revision}, {"timestamp", "2026-11-16T00:00:00Z"}, {"event_type", "node.completed"},
                {"node_id", "reviewer_0"}, {"actor", "fixture"}, {"data", QJsonObject{}}};
            const auto frame = "id: " + event.value("event_id").toString().toUtf8() + "\nevent: node.completed\ndata: "
                + QJsonDocument(event).toJson(QJsonDocument::Compact) + "\n\n";
            const auto requestStart = fixture.source.requests.size();
            timer.start(); fixture.streams.last().socket->write(frame); fixture.streams.last().socket->flush();
            QVERIFY(QTest::qWaitFor([&] { return ui.missions.revision() == revision && ui.missions.synchronized(); }, 10000));
            QVERIFY(render()); updates.append(measure(timer));
            // Each update reads the graph and artifacts between the two Mission reads.
            int detailReads = 0;
            for (int r = requestStart; r < fixture.source.requests.size(); ++r)
                if (fixture.source.requests[r].method == "GET"
                        && fixture.source.requests[r].path.startsWith("/api/v2/missions/")) ++detailReads;
            QCOMPARE(detailReads, 4);
            const auto records = page->property("nodeRecords").value<QJSValue>().toVariant().toList();
            bool activeVisible = false;
            for (const auto &record : records) {
                const auto node = record.toMap();
                if (node.value("nodeId") == (i % 2 ? "validation_1" : "repair_1")
                        && node.value("status") == "running") activeVisible = true;
            }
            QVERIFY(activeVisible);
            QVERIFY(retainedDiff); QCOMPARE(ui.find("workflowNodeDiff"), retainedDiff.data());
            QCOMPARE(retainedDiff->property("offset").toInt(), retainedOffset);
            QCOMPARE(page->property("selectedNodeId").toString(), QString("implementer"));
        }
        record("mode_switch_to_frame", modes); record("node_selection_to_frame", nodes);
        record("long_diff_page_to_frame", pages); record("unchanged_view_refresh_cpu", refreshes);
        record("sse_to_view_frame", updates);
        results["scope"] = "Real Main.qml / production WorkflowPage, loopback HTTP/SSE fixture, offscreen software renderer; frame readback included except refresh CPU";
        results["diff_characters"] = largeDiff.size();
        const auto outputPath = qEnvironmentVariable("FORGEPILOT_WORKFLOW_PROFILE");
        if (!outputPath.isEmpty()) {
            QVERIFY2(!QFileInfo::exists(outputPath), "Profile output already exists; choose a new evidence path");
            QSaveFile output(outputPath); QVERIFY(output.open(QIODevice::WriteOnly));
            output.write(QJsonDocument(results).toJson()); QVERIFY(output.commit());
        }
        QVERIFY(ui.capture("stage5-main-workflow-long-diff"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void ragSettingsUseVisibleRepository()
    {
        QTemporaryDir chat, task;
        QVERIFY(chat.isValid() && task.isValid());
        Fixture fixture;
        fixture.mission["repository_path"] = task.path();
        QStringList operations, targets;
        fixture.intercept = [&](const Request &request) {
            if (request.path == "/api/v1/conversations/" + A.toUtf8()) {
                Source::json(request, {{"conversation_id", A}, {"title", "聊天 A"},
                    {"repository_path", chat.path()}, {"messages", QJsonArray{}}});
                return true;
            }
            if (!request.path.startsWith("/api/v1/rag/")) return false;
            const QUrl url = QUrl::fromEncoded(request.path);
            if (url.path().endsWith("/events")) {
                Source::stream(request, ": ready\n\n", false);
                return true;
            }
            const QString operation = request.method + ' ' + url.path().section('/', -1);
            operations.append(operation);
            targets.append(request.method == "POST" ? request.body.value("repository_path").toString()
                : QUrlQuery(url).queryItemValue("repository_path", QUrl::FullyDecoded));
            const bool running = operation == "POST index";
            const bool deleted = operation == "DELETE index";
            Source::json(request, {{"repository_id", QString(64, 'a')}, {"enabled", true}, {"available", true},
                {"status", running ? "indexing" : deleted ? "empty" : "ready"}, {"running", running},
                {"revision", operations.size()}, {"operation_id", running ? QJsonValue(QString(32, 'c')) : QJsonValue::Null},
                {"total_files", 1}, {"processed", 0}, {"chunks", 0}, {"file_count", deleted ? 0 : 1},
                {"dirty_file_count", 0}, {"failed_file_count", 0}, {"last_indexed_at", QJsonValue::Null},
                {"error_code", QJsonValue::Null}});
            return true;
        };
        Ui ui(fixture);
        ui.rag.setBaseUrl(fixture.source.url());
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        auto exerciseIndex = [&](const QString &settingsButton, const QString &expected) {
            operations.clear(); targets.clear();
            QVERIFY(ui.click(settingsButton));
            QTRY_COMPARE(ui.rag.status(), QString("ready"));
            QVERIFY(ui.find("codeIndexTarget")->property("text").toString().contains(expected));
            QVERIFY(ui.click("buildCodeIndexButton"));
            QTRY_VERIFY(ui.rag.running());
            QVERIFY(ui.click("cancelCodeIndexButton"));
            QTRY_VERIFY(!ui.rag.running());
            QVERIFY(ui.click("deleteCodeIndexButton"));
            QVERIFY(ui.click("confirmDeleteCodeIndexButton"));
            QTRY_COMPARE(ui.rag.status(), QString("empty"));
            QCOMPARE(operations, QStringList({"GET status", "POST index", "POST cancel", "DELETE index"}));
            QCOMPARE(targets, QStringList(4, expected));
            QVERIFY(ui.click("closeSettingsButton"));
        };
        ui.conversations.selectConversation(A);
        QTRY_COMPARE(ui.conversations.repositoryPath(), chat.path());
        exerciseIndex("settingsButton", chat.path());
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QVERIFY(ui.click("workbenchSettingsButton"));
        QTRY_VERIFY(ui.rag.repositoryPath().isEmpty());
        QVERIFY(ui.find("codeIndexTarget")->property("text").toString().contains("待选择"));
        QVERIFY(!ui.find("buildCodeIndexButton")->isEnabled());
        QVERIFY(!ui.find("deleteCodeIndexButton")->isEnabled());
        QVERIFY(ui.click("closeSettingsButton"));
        auto *input = ui.find("missionRepositoryInput"); QVERIFY(input);
        input->setProperty("text", task.path().left(3));
        input->setProperty("text", task.path());
        QVERIFY(ui.rag.repositoryPath().isEmpty());
        exerciseIndex("workbenchSettingsButton", task.path());
        input->setProperty("text", chat.path());
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        exerciseIndex("workbenchSettingsButton", task.path());
        QVERIFY(ui.click("workflowDemandToggle"));
        exerciseIndex("workbenchSettingsButton", chat.path());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void runtimeConfigurationFirstRunAndSettingsShareFormWithoutBackend()
    {
        QTemporaryDir directory; QVERIFY(directory.isValid());
        const auto path = directory.filePath("config.json");
        RuntimeConfigController runtime;
        runtime.setHelperCommand(QCoreApplication::applicationFilePath(), {"--runtime-config-fixture"}, path);
        Fixture fixture; Ui ui(fixture, 1280, 900, &runtime);
        ui.tasks.setBaseUrl(QUrl("http://127.0.0.1:1")); ui.missions.setBaseUrl(QUrl("http://127.0.0.1:1"));
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QTRY_VERIFY(runtime.loaded()); QVERIFY(runtime.wizardRequired());
        auto *dialog = ui.window->findChild<QObject *>("runtimeConfigDialog"); QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool());
        auto *key = ui.find("runtimeApiKey"); QVERIFY(key);
        key->setProperty("text", "private-pipe-key");
        QVERIFY(ui.click("runtimeConfigSave"));
        QTRY_VERIFY(!runtime.busy()); QVERIFY(runtime.configured()); QVERIFY(!runtime.wizardRequired());
        QCOMPARE(key->property("text").toString(), QString{});
        QVERIFY(ui.find("runtimeConfigEffectiveTime")->property("text").toString().contains("重启"));
        QVERIFY(ui.capture("runtime-configuration-saved"));
        QFile saved(path); QVERIFY(saved.open(QIODevice::ReadOnly)); const auto original = saved.readAll(); saved.close();
        QVERIFY(!original.contains("private-pipe-key"));
        QVERIFY(ui.click("runtimeConfigClose")); QTRY_VERIFY(!dialog->property("visible").toBool());
        QVERIFY(ui.click("settingsButton")); QVERIFY(ui.click("openRuntimeConfigButton"));
        QTRY_VERIFY(dialog->property("visible").toBool());
        QCOMPARE(ui.window->findChild<QObject *>("runtimeConfigDialog"), dialog);
        auto *task = ui.find("runtimeTaskModel"); QVERIFY(task); task->setProperty("text", "reject");
        QVERIFY(QMetaObject::invokeMethod(task, "textEdited")); key->setProperty("text", "private-pipe-key");
        QVERIFY(ui.click("runtimeConfigSave")); QTRY_VERIFY(!runtime.busy());
        QVERIFY(runtime.configured()); QVERIFY(!runtime.errors().isEmpty());
        QCOMPARE(runtime.fields().value("task_model").toString(), "fixture-task");
        QCOMPARE(key->property("text").toString(), QString{});
        QVERIFY(saved.open(QIODevice::ReadOnly)); QCOMPARE(saved.readAll(), original);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void runtimeConfigurationRealPythonHelperSavesDpapiAndMissionInputs()
    {
        QTemporaryDir directory; QVERIFY(directory.isValid());
        const auto root = QString(FORGEPILOT_SOURCE_DIR);
        const auto python = qEnvironmentVariable("FORGEPILOT_TEST_PYTHON", root + "/agent.venv/Scripts/python.exe");
        QVERIFY2(QFileInfo::exists(python), "Set FORGEPILOT_TEST_PYTHON to the existing Python 3.12 environment");
        const auto configPath = directory.filePath("config.json");
        const QStringList launcher{"-X", "utf8", "-I", root + "/agent/desktop.py"};
        RuntimeConfigController runtime;
        runtime.setHelperCommand(python, launcher, configPath);
        Fixture fixture; Ui ui(fixture, 1280, 900, &runtime);
        ui.tasks.setBaseUrl(QUrl("http://127.0.0.1:1")); ui.missions.setBaseUrl(QUrl("http://127.0.0.1:1"));
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QTRY_VERIFY_WITH_TIMEOUT(runtime.loaded() && !runtime.busy(), 15000);
        auto *dialog = ui.window->findChild<QObject *>("runtimeConfigDialog"); QVERIFY(dialog);
        QTRY_VERIFY(dialog->property("visible").toBool()); QVERIFY(!runtime.configured());
        const QList<QPair<QString, QString>> values{{"runtimeBaseUrl", "http://127.0.0.1:1/v1"},
            {"runtimeChatModel", "offline-chat"}, {"runtimeTaskModel", "offline-task"},
            {"runtimeReviewModel", "offline-review"}, {"runtimeSummaryModel", "offline-summary"},
            {"runtimeTaskFamily", "writer"}, {"runtimeReviewFamily", "reviewer"}};
        for (const auto &[name, value] : values) {
            auto *input = ui.find(name); QVERIFY2(input, qPrintable(name)); input->setProperty("text", value);
            QVERIFY(QMetaObject::invokeMethod(input, "textEdited"));
        }
        QTcpServer occupied;
        QVERIFY(occupied.listen(QHostAddress::LocalHost));
        auto *port = ui.find("runtimePort"); QVERIFY(port);
        port->setProperty("value", occupied.serverPort());
        QVERIFY(QMetaObject::invokeMethod(port, "valueModified"));
        auto *key = ui.find("runtimeApiKey"); QVERIFY(key);
        const QString dummyKey = "offline-gui-dummy-key-not-a-real-credential";
        key->setProperty("text", dummyKey); QVERIFY(ui.click("runtimeConfigSave"));
        QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 15000);
        QVERIFY2(runtime.configured(), qPrintable(runtime.message()));
        QVERIFY(runtime.hasApiKey()); QVERIFY(runtime.errors().isEmpty()); QVERIFY(!runtime.warnings().isEmpty());
        QCOMPARE(key->property("text").toString(), QString{});
        QFile config(configPath); QVERIFY(config.open(QIODevice::ReadOnly));
        const auto bytes = config.readAll(); config.close(); QVERIFY(!bytes.contains(dummyKey.toUtf8()));
        const auto saved = QJsonDocument::fromJson(bytes).object();
        QCOMPARE(saved.value("chat_model").toString(), "offline-chat");
        QCOMPARE(saved.value("task_model").toString(), "offline-task");
        QCOMPARE(saved.value("review_model").toString(), "offline-review");
        QCOMPARE(saved.value("summary_model").toString(), "offline-summary");
        QCOMPARE(saved.value("task_model_family").toString(), "writer");
        QCOMPARE(saved.value("review_model_family").toString(), "reviewer");
        QFile encrypted(directory.filePath(saved.value("credential_file").toString())); QVERIFY(encrypted.open(QIODevice::ReadOnly));
        const auto protectedKey = encrypted.readAll(); QVERIFY(!protectedKey.isEmpty()); QVERIFY(!protectedKey.contains(dummyKey.toUtf8()));
        QVERIFY(runtime.restartRequired());
        QProcess routes;
        const auto inspect = QString::fromUtf8(R"PY(
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'agent'))
from forgepilot.desktop_configuration import read_config, load_saved_key
from forgepilot.settings import model_gateway_from_args
from forgepilot.server import parser
config = Path(sys.argv[2])
data = read_config(config)
os.environ['FORGEPILOT_API_KEY'] = load_saved_key(config)
args = parser().parse_args(data['server_args'])
for name in ('base_url', 'chat_model', 'task_model', 'review_model', 'summary_model',
             'task_model_family', 'review_model_family'):
    setattr(args, name, data[name])
gateway = model_gateway_from_args(args)
names = ('chat', 'task', 'orchestration', 'analysis', 'implementation', 'repair', 'review', 'summary')
result = {name: {'model': gateway.router.resolve(name).primary.model,
                 'family': gateway.router.resolve(name).primary.family} for name in names}
assert not gateway._providers
asyncio.run(gateway.aclose())
print(json.dumps(result))
)PY");
        routes.start(python, {"-X", "utf8", "-I", "-c", inspect, root, configPath});
        QTRY_VERIFY_WITH_TIMEOUT(routes.state() == QProcess::NotRunning, 10000);
        QCOMPARE(routes.exitCode(), 0);
        const auto routing = QJsonDocument::fromJson(routes.readAllStandardOutput()).object();
        for (const auto &role : {"task", "orchestration", "analysis", "implementation", "repair"})
            QCOMPARE(routing.value(role).toObject().value("model").toString(), "offline-task");
        QCOMPARE(routing.value("review").toObject().value("model").toString(), "offline-review");
        QVERIFY(routing.value("review").toObject().value("family") != routing.value("implementation").toObject().value("family"));
        QCOMPARE(routing.value("chat").toObject().value("model").toString(), "offline-chat");
        QCOMPARE(routing.value("summary").toObject().value("model").toString(), "offline-summary");
        QVERIFY(ui.capture("runtime-configuration-real-helper"));
        {
            const auto stop = qScopeGuard([&] {
                QCOMPARE(QProcess::execute(python, launcher + QStringList{"stop", "--config", configPath}), 0);
            });
            QSignalSpy accepted(&runtime, &RuntimeConfigController::restartAccepted);
            QVERIFY(ui.click("runtimeConfigRestart"));
            QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 30000);
            QCOMPARE(accepted.count(), 0);
            QVERIFY(dialog->property("visible").toBool());
            QVERIFY(runtime.configured() && runtime.restartAllowed() && runtime.restartRequired());
            auto *notice = ui.find("runtimeConfigMessage"); QVERIFY(notice && notice->isVisible());
            QVERIFY2(notice->property("text").toString().contains("端口"), qPrintable(runtime.message()));
            QVERIFY(config.open(QIODevice::ReadOnly)); QCOMPARE(config.readAll(), bytes); config.close();

            occupied.close();
            QVERIFY(ui.click("runtimeConfigRestart"));
            QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 180000);
            QCOMPARE(accepted.count(), 1);
            QVERIFY(!runtime.restartRequired());
        }
        // A Python-generated configuration is reloaded through the same helper,
        // without checking model endpoints.
        QVERIFY(ui.click("runtimeConfigReload")); QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 15000);
        QVERIFY(runtime.configured()); QVERIFY(runtime.hasApiKey());
        QCOMPARE(runtime.fields().value("task_model").toString(), "offline-task");
        auto *modelPath = ui.find("runtimeExternalModelConfig"); QVERIFY(modelPath);
        QVERIFY(!modelPath->isVisible());
        QVERIFY(ui.find("runtimeReviewModel")->isVisible());

        // Point the same launcher at a manually managed file, then reload the dialog.
        const auto manualPath = directory.filePath("manual routes.json");
        auto args = saved.value("server_args").toArray();
        const auto modelFlag = args.toVariantList().indexOf(QStringLiteral("--model-config"));
        QVERIFY(modelFlag >= 0);
        Source general, reviewer;
        general.handle = [](const Request &request) {
            const QJsonObject reply{{"message", QJsonObject{{"role", "assistant"}, {"content", "OK"}}},
                                    {"finish_reason", "stop"}};
            Source::json(request, {{"choices", QJsonArray{reply}}});
        };
        reviewer.handle = [](const Request &request) { Source::json(request, {}, 403); };
        QFile generated(args.at(modelFlag + 1).toString()); QVERIFY(generated.open(QIODevice::ReadOnly));
        auto models = QJsonDocument::fromJson(generated.readAll()).object();
        auto profiles = models.value("profiles").toArray();
        for (auto it = profiles.begin(); it != profiles.end(); ++it) {
            auto profile = (*it).toObject();
            const auto endpoint = profile.value("profile_id") == "desktop-review" ? reviewer.url() : general.url();
            profile["base_url"] = endpoint.toString() + "/v1";
            profile["model"] = "same-name";
            *it = profile;
        }
        models["profiles"] = profiles;
        QFile manual(manualPath); QVERIFY(manual.open(QIODevice::WriteOnly));
        manual.write(QJsonDocument(models).toJson()); manual.close();
        args[modelFlag + 1] = manualPath;
        auto external = saved;
        external["server_args"] = args;
        QVERIFY(config.open(QIODevice::WriteOnly | QIODevice::Truncate));
        config.write(QJsonDocument(external).toJson()); config.close();
        QVERIFY(ui.click("runtimeConfigReload")); QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 15000);
        QVERIFY(modelPath->isVisible());
        QVERIFY(QDir::fromNativeSeparators(modelPath->property("text").toString()).contains(manualPath));
        QVERIFY(!ui.find("runtimeBaseUrl")->isVisible());
        QVERIFY(!ui.find("runtimeReviewModel")->isVisible());
        QVERIFY(ui.click("runtimeCloudAllowed")); QVERIFY(ui.click("runtimeConfigSave"));
        QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 15000);
        QVERIFY2(runtime.errors().isEmpty(), qPrintable(runtime.message()));
        QVERIFY(config.open(QIODevice::ReadOnly));
        const auto updatedArgs = QJsonDocument::fromJson(config.readAll()).object().value("server_args").toArray();
        QVERIFY(updatedArgs.contains("--mission-allow-cloud-model"));
        QVERIFY(updatedArgs.contains(manualPath));
        QVERIFY(general.requests.isEmpty()); QVERIFY(reviewer.requests.isEmpty());
        QVERIFY(ui.click("runtimeConfigCheck")); QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 15000);
        QCOMPARE(general.requests.size(), 1); QCOMPARE(reviewer.requests.size(), 1);
        QCOMPARE(general.requests.first().body.value("model").toString(), "same-name");
        QCOMPARE(reviewer.requests.first().body.value("model").toString(), "same-name");
        QCOMPARE(runtime.errors().size(), 1);
        const auto error = runtime.errors().first().toMap();
        QCOMPARE(error.value("purpose").toString(), "review");
        auto *errorLabel = ui.find({}, "text", QStringLiteral("审查：") + error.value("message").toString());
        QVERIFY(errorLabel && errorLabel->isVisible());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void mcpEditorOffersScopesSupportedByTransport()
    {
        Fixture fixture;
        fixture.intercept = [](const Request &request) {
            if (request.path != "/api/v1/mcp/servers/" + McpId.toUtf8()) return false;
            Source::json(request, {{"name", "旧本地服务"}, {"transport", "stdio"}, {"revision", 1},
                {"connection", QJsonObject{{"command", QCoreApplication::applicationFilePath()}}},
                {"modes", QJsonArray{"chat"}}, {"catalog_hash", "test-catalog"}, {"enabled", true},
                {"selected_tools", QJsonArray{"lookup"}}});
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("settingsButton"));
        QTRY_VERIFY(ui.mcp.statusKnown() && !ui.mcp.loading());
        ui.mcp.edit();
        auto *chat = ui.find("mcpChatScope"); QVERIFY(chat);
        auto *work = ui.find("mcpTaskScope"); QVERIFY(work);
        QTRY_VERIFY(chat->isVisible() && work->isVisible());
        QVERIFY(chat->property("checked").toBool() && work->property("checked").toBool());
        ui.mcp.setKind(2);
        QVERIFY(!chat->isVisible() && work->isVisible());
        QCOMPARE(ui.mcp.draft().value("modes").toList(), QVariantList{"task"});

        ui.mcp.closeEditor();
        ui.mcp.edit(McpId);
        QTRY_VERIFY(ui.mcp.existing());
        QVERIFY(!chat->isVisible() && work->isVisible());
        QVERIFY(!work->property("checked").toBool());
        QVERIFY(!ui.mcp.canSave());
        QVERIFY(ui.click(work));
        QCOMPARE(ui.mcp.draft().value("modes").toList(), QVariantList{"task"});
        QVERIFY(ui.mcp.canSave());
    }
    void terminalSkillDraftFromHistory_data()
    {
        QTest::addColumn<QString>("status");
        QTest::newRow("success") << QString("succeeded");
        QTest::newRow("failure") << QString("failed");
        QTest::newRow("cancelled") << QString("cancelled");
    }
    void terminalSkillDraftFromHistory()
    {
        QFETCH(QString, status);
        Fixture fixture;
        fixture.setPhase(status, "");
        const auto examples = fixtureJson("protocol/examples.json");
        auto draft = examples.value("skill_draft_create_response").toObject();
        draft["source_mission_id"] = MissionId;
        const QString note = "Check the row value before inspecting view notifications.";
        QJsonObject create, patch;
        bool sourceFilter = false;
        fixture.intercept = [&](const Request &request) {
            if (request.path.startsWith("/api/v2/missions?")) {
                Source::json(request, {{"items", QJsonArray{fixture.mission}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
            } else if (request.path.startsWith("/api/v2/skill-drafts?")) {
                sourceFilter = QUrlQuery(QUrl::fromEncoded(request.path)).queryItemValue("source_mission_id") == MissionId;
                Source::json(request, {{"drafts", QJsonArray{}}, {"next_offset", QJsonValue::Null}});
            } else if (request.path == "/api/v2/missions/" + MissionId.toUtf8() + "/skill-drafts") {
                create = request.body;
                auto data = draft.value("data").toObject();
                data["developer_note"] = create.value("developer_note");
                draft["data"] = data;
                Source::json(request, draft, 201);
            } else if (request.path == "/api/v2/skill-drafts/" + draft["draft_id"].toString().toUtf8()) {
                if (request.method == "PATCH") {
                    patch = request.body;
                    Source::json(request, examples.value("skill_save_conflict_error").toObject(), 409);
                } else Source::json(request, draft);
            } else return false;
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY(ui.find("missionTaskRow", "taskId", MissionId));
        QVERIFY(ui.click(ui.find("missionTaskRow", "taskId", MissionId)));
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workflowExtractSkillButton"));
        QTRY_VERIFY(sourceFilter);
        QTRY_VERIFY(ui.find("skillDraftNote") && ui.find("skillDraftNote")->isVisible());
        ui.find("skillDraftNote")->setProperty("text", note);
        QVERIFY(ui.click("skillDraftCreateManual"));
        QTRY_COMPARE(create.value("developer_note").toString(), note);
        QCOMPARE(create.value("generation_mode").toString(), QString("manual"));
        const QString edited = "Inspect the model row";
        QTRY_VERIFY(ui.find("skillDraftName") && ui.find("skillDraftName")->isVisible());
        ui.find("skillDraftName")->setProperty("text", edited);
        QVERIFY(ui.click("skillDraftSave"));
        QTRY_COMPARE(patch.value("name").toString(), edited);
        QCOMPARE(patch.value("expected_revision").toInt(), 1);
        QTRY_VERIFY(ui.find("skillDraftError") && ui.find("skillDraftError")->isVisible());
        QVERIFY(ui.find("skillDraftError")->property("text").toString().contains("冲突"));
        QCOMPARE(ui.find("skillDraftName")->property("text").toString(), edited);
        QVERIFY(ui.missions.error().isEmpty());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void retrySkillGenerationKeepsDeveloperEdit()
    {
        Fixture fixture;
        fixture.setPhase("succeeded", "");
        const auto examples = fixtureJson("protocol/examples.json");
        auto draft = examples.value("skill_generate_accepted_response").toObject();
        draft["source_mission_id"] = MissionId;
        const auto path = "/api/v2/skill-drafts/" + draft.value("draft_id").toString().toUtf8();
        const auto completed = [&](const QString &example) {
            auto result = draft;
            auto attempt = examples.value(example).toObject();
            auto data = draft.value("data").toObject();
            data["generation_status"] = attempt.value("status");
            result["data"] = data;
            result["latest_attempt"] = attempt;
            return result;
        };
        int retries = 0;
        Request pendingPoll;
        fixture.intercept = [&](const Request &request) {
            if (request.path.startsWith("/api/v2/skill-drafts?"))
                Source::json(request, {{"drafts", QJsonArray{}}, {"next_offset", QJsonValue::Null}});
            else if (request.path == "/api/v2/missions/" + MissionId.toUtf8() + "/skill-drafts")
                Source::json(request, draft, 202);
            else if (request.path == path + "/regenerate") {
                ++retries;
                Source::json(request, draft, 202);
            } else if (request.path == path && !retries)
                Source::json(request, completed("skill_generation_insufficient_response"));
            else if (request.path == path) pendingPoll = request;
            else return false;
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workflowExtractSkillButton"));
        QTRY_VERIFY(ui.find("skillDraftCreateModel") && ui.find("skillDraftCreateModel")->isEnabled());
        QVERIFY(ui.click("skillDraftCreateModel"));
        QTRY_VERIFY(ui.find("skillDraftStatus")->property("text").toString().contains("起草失败"));
        QVERIFY(ui.click("skillDraftRetry"));
        QTRY_COMPARE(retries, 1);
        QTRY_VERIFY(pendingPoll.socket);
        const QString localEdit = "Keep the developer diagnosis";
        QVERIFY(ui.find("skillDraftBody")->isEnabled());
        ui.find("skillDraftBody")->setProperty("text", localEdit);
        const auto ready = completed("skill_generation_ready_response");
        Source::json(pendingPoll, ready);
        const auto suggestion = ready["latest_attempt"].toObject()["suggestion"].toObject();
        QTRY_VERIFY(ui.find("skillDraftSuggestion")->isVisible());
        QVERIFY(ui.find("skillDraftSuggestion")->property("text").toString().contains(suggestion["conclusion"].toString()));
        QCOMPARE(ui.find("skillDraftBody")->property("text").toString(), localEdit);
        QVERIFY(ui.find("skillDraftSave")->isEnabled());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void reopenedSkillEditorIgnoresEarlierResponse()
    {
        Fixture fixture;
        fixture.setPhase("failed", "");
        Request held;
        int listings = 0;
        fixture.intercept = [&](const Request &request) {
            if (!request.path.startsWith("/api/v2/skill-drafts?")) return false;
            if (++listings == 1) held = request;
            else Source::json(request, {{"drafts", QJsonArray{}}, {"next_offset", QJsonValue::Null}});
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workflowExtractSkillButton"));
        QTRY_VERIFY(held.socket);
        // Close the editor while the read is still in flight. Both openings
        // share the same controller, backend and Mission identity.
        ui.window->setProperty("agentMode", false);
        QTRY_VERIFY(!ui.find("skillDraftNote") || !ui.find("skillDraftNote")->isVisible());
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QVERIFY(ui.click("workflowExtractSkillButton"));
        QTRY_COMPARE(listings, 2);
        QTRY_VERIFY(ui.find("skillDraftCreateManual") && ui.find("skillDraftCreateManual")->isEnabled());
        const QString note = "Second editor note";
        ui.find("skillDraftNote")->setProperty("text", note);
        Source::json(held, {{"detail", "Not Found"}}, 404);
        QTRY_VERIFY(!held.socket);
        QCOMPARE(ui.find("skillDraftNote")->property("text").toString(), note);
        QVERIFY(!ui.find("skillDraftError") || !ui.find("skillDraftError")->isVisible());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void skillLibraryOnLegacyBackend()
    {
        Fixture fixture;
        fixture.intercept = [](const Request &request) {
            if (!request.path.startsWith("/api/v2/skills?") && request.path != "/api/v2/skills") return false;
            Source::json(request, {{"detail", "Not Found"}}, 404);
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QVERIFY(ui.click("workbenchSkillLibraryButton"));
        QTRY_VERIFY(ui.find("skillDraftError") && ui.find("skillDraftError")->isVisible());
        QVERIFY(ui.find("skillDraftError")->property("text").toString().contains("后端"));
        QVERIFY(ui.missions.error().isEmpty());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void skillChoicesFromMainSubmitOnlyCheckedVersions_data()
    {
        QTest::addColumn<bool>("selectSkill");
        QTest::addColumn<bool>("preset");
        QTest::newRow("zero-checked") << false << false;
        QTest::newRow("one-checked") << true << false;
        QTest::newRow("preview-confirms-cmake") << true << true;
    }
    void skillChoicesFromMainSubmitOnlyCheckedVersions()
    {
        QFETCH(bool, selectSkill);
        QFETCH(bool, preset);
        Fixture fixture;
        const auto examples = fixtureJson("protocol/examples.json");
        const auto response = examples.value("skill_recommendations_response").toObject();
        const auto candidate = response.value("recommendations").toArray().first().toObject();
        const auto identity = examples.value("mission_create_with_skills_request").toObject()
                                  .value("selected_skills").toArray().first();
        QList<QJsonObject> recommendations;
        int previews = 0, discards = 0;
        fixture.intercept = [&](const Request &request) {
            if (request.path == "/api/v2/skills/recommendations" && request.method == "POST") {
                recommendations.append(request.body);
                Source::json(request, response);
                return true;
            }
            if (request.path == "/api/v2/missions/preview") {
                ++previews;
                if (preset) {
                    Source::json(request, {{"preview_token", QString(64, 'c')}, {"base_commit", QString(40, 'a')},
                        {"source_head", QString(40, 'a')}, {"baseline_paths", QJsonArray{}}, {"changed_files", QJsonArray{}},
                        {"patch", ""}, {"build", QJsonObject{{"source", "preset"}, {"configure_preset", "release"}}}});
                    return true;
                }
            }
            if (request.method == "DELETE" && request.path.startsWith("/api/v2/missions/previews/")) ++discards;
            return false;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        const QString description = "保存成功但列表仍显示旧值";
        auto *prompt = ui.find("missionRequestInput");
        QVERIFY(prompt);
        prompt->setProperty("text", "保存");
        prompt->setProperty("text", description);
        ui.find("missionRepositoryInput")->setProperty("text", "D:/fixture/Qt");
        ui.find("missionTaskType")->setProperty("currentIndex", 1);
        QTRY_VERIFY(ui.find("missionSkillChoice"));
        auto *choice = ui.find("missionSkillChoice");
        QVERIFY(choice->isVisible());
        QVERIFY(!choice->property("checked").toBool());
        QVERIFY(choice->property("text").toString().contains(candidate.value("name").toString()));
        QVERIFY(choice->property("text").toString().contains("1"));
        QVERIFY(ui.find("missionSkillReasons")->property("text").toString()
                    .contains(candidate.value("reasons").toArray().first().toString()));
        auto *summary = ui.find("missionSkillSummary");
        QVERIFY(summary && !summary->isVisible());
        QVERIFY(ui.click("missionSkillSummaryToggle"));
        QTRY_VERIFY(summary->isVisible());
        QCOMPARE(summary->property("text").toString(), candidate.value("summary").toString());
        QCOMPARE(recommendations.size(), 1);
        const auto body = recommendations.first();
        QCOMPARE(body.value("description").toString(), description);
        QCOMPARE(body.value("task_type").toString(), QString("bug_fix"));
        QVERIFY(!body.contains("repository_path"));
        QVERIFY(body.value("project_tags").toArray().isEmpty());
        QCOMPARE(previews, 0);
        if (preset) {
            QVERIFY(ui.click(choice));
            QVERIFY(choice->property("checked").toBool());
        }
        QVERIFY(ui.click("missionPreviewButton"));
        QTRY_VERIFY(ui.missions.creationReady());
        if (preset) {
            QTRY_COMPARE(recommendations.size(), 2);
            QCOMPARE(recommendations.last().value("project_tags").toArray(), QJsonArray{"cmake"});
            QTRY_VERIFY(ui.find("missionSkillChoice"));
            choice = ui.find("missionSkillChoice");
            QVERIFY(!choice->property("checked").toBool());
        }
        const auto token = ui.missions.creationPreview().value("preview_token").toString();
        if (selectSkill) {
            QVERIFY(ui.click(choice));
            QTRY_VERIFY(ui.find("missionSkillChoice")->property("checked").toBool());
            if (!preset) QVERIFY(ui.find("missionSkillSummary")->isVisible());
        }
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_COMPARE(fixture.creates.size(), 1);
        const auto posted = fixture.creates.first();
        QCOMPARE(posted.value("request").toString(), description);
        QCOMPARE(posted.value("preview_token").toString(), token);
        if (selectSkill) QCOMPARE(posted.value("selected_skills").toArray(), QJsonArray{identity});
        else QVERIFY(!posted.contains("selected_skills"));
        QCOMPARE(previews, 1);
        QCOMPARE(discards, 0);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void thirdSkillChoiceIsDisabledUntilOneIsDeselected()
    {
        Fixture fixture;
        const auto example = fixtureJson("protocol/examples.json").value("skill_recommendations_response").toObject();
        auto skill = example.value("recommendations").toArray().first().toObject();
        QJsonArray choices;
        for (const auto *id : {"first", "second", "third"}) {
            skill["skill_id"] = id;
            choices.append(skill);
        }
        fixture.intercept = [&](const Request &request) {
            if (request.path != "/api/v2/skills/recommendations") return false;
            Source::json(request, {{"recommendations", choices}});
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.find("missionRequestInput")->setProperty("text", "Review these changes");
        QTRY_VERIFY(ui.find("missionSkillChoice", "skillId", "third"));
        auto *first = ui.find("missionSkillChoice", "skillId", "first");
        auto *third = ui.find("missionSkillChoice", "skillId", "third");
        QVERIFY(ui.click(first));
        QVERIFY(ui.click(ui.find("missionSkillChoice", "skillId", "second")));
        QTRY_VERIFY(!third->isEnabled());
        QVERIFY(!third->property("checked").toBool());
        QVERIFY(ui.click(first));
        QTRY_VERIFY(third->isEnabled());
    }
    void rejectedSkillSelectionPreservesDraft_data()
    {
        QTest::addColumn<bool>("legacy");
        QTest::newRow("version-conflict") << false;
        QTest::newRow("backend-rejects-selected-field") << true;
    }
    void rejectedSkillSelectionPreservesDraft()
    {
        QFETCH(bool, legacy);
        Fixture fixture;
        const auto examples = fixtureJson("protocol/examples.json");
        const auto offered = examples.value("skill_recommendations_response").toObject();
        auto candidate = offered.value("recommendations").toArray().first().toObject();
        QList<QJsonObject> attempts;
        fixture.intercept = [&](const Request &request) {
            if (request.path == "/api/v2/skills/recommendations") {
                if (attempts.isEmpty()) Source::json(request, offered);
                else if (legacy) Source::json(request, {{"error", QJsonObject{{"code", "route_not_found"},
                    {"message", "Route not found"}, {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}}, 404);
                else {
                    candidate["version"] = 2;
                    candidate["content_sha256"] = QString(64, 'd');
                    Source::json(request, {{"recommendations", QJsonArray{candidate}}});
                }
                return true;
            }
            if (request.path == "/api/v2/missions" && request.method == "POST") {
                attempts.append(request.body);
                if (attempts.size() > 1) return false;
                auto error = examples.value("mission_skill_selection_changed_error").toObject();
                if (legacy) {
                    auto detail = error.value("error").toObject();
                    detail["code"] = "invalid_request";
                    detail["message"] = "Request does not match the v2 schema";
                    error["error"] = detail;
                }
                Source::json(request, error, legacy ? 422 : 409);
                return true;
            }
            return false;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        const QString description = "保存成功但列表仍显示旧值";
        const QString repository = "D:/fixture/repository";
        auto *prompt = ui.find("missionRequestInput");
        auto *repo = ui.find("missionRepositoryInput");
        prompt->setProperty("text", description);
        repo->setProperty("text", repository);
        QTRY_VERIFY(ui.find("missionSkillChoice"));
        QVERIFY(ui.click("missionPreviewButton"));
        QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionBaselineFile"));
        QVERIFY(!ui.missions.creationReady());
        QVERIFY(ui.click("missionPreviewButton"));
        QTRY_VERIFY(ui.missions.creationReady());
        auto *confirmation = ui.find("missionBaselineConfirmation");
        QVERIFY(ui.click(confirmation));
        const auto firstToken = ui.missions.creationPreview().value("preview_token").toString();
        QVERIFY(ui.click("missionSkillChoice"));
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_VERIFY(!ui.missions.creationError().isEmpty());
        QVERIFY(ui.find("missionCreationError")->isVisible());
        QVERIFY(prompt->isVisible());
        QCOMPARE(prompt->property("text").toString(), description);
        QCOMPARE(repo->property("text").toString(), repository);
        if (legacy) {
            QTRY_VERIFY(!ui.missions.recommendationError().isEmpty());
            QVERIFY(!ui.find("missionSkillChoice"));
            QVERIFY(ui.missions.creationReady());
            QVERIFY(confirmation->property("checked").toBool());
        } else {
            QVERIFY(!ui.missions.creationReady());
            QVERIFY(!confirmation->property("checked").toBool());
            QVERIFY(!ui.find("missionCreateButton")->isEnabled());
            QTRY_VERIFY(ui.find("missionSkillChoice") && ui.find("missionSkillChoice")->property("text").toString().contains("v2"));
            QVERIFY(!ui.find("missionSkillChoice")->property("checked").toBool());
            QVERIFY(ui.click("missionSkillChoice"));
            QVERIFY(ui.click("missionPreviewButton"));
            QTRY_VERIFY(ui.missions.creationReady());
            QVERIFY(!confirmation->property("checked").toBool());
            QVERIFY(!ui.find("missionCreateButton")->isEnabled());
            QVERIFY(ui.click(confirmation));
        }
        QCOMPARE(attempts.size(), 1);
        const auto retryToken = ui.missions.creationPreview().value("preview_token").toString();
        QCOMPARE(retryToken == firstToken, legacy);
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_COMPARE(fixture.creates.size(), 1);
        QCOMPARE(attempts.size(), 2);
        QCOMPARE(attempts.first().value("preview_token").toString(), firstToken);
        QCOMPARE(attempts.last().value("preview_token").toString(), retryToken);
        QCOMPARE(attempts.last().value("baseline_paths").toArray(), QJsonArray{"main.cpp"});
        QVERIFY(attempts.last().value("baseline_confirmed").toBool());
        QCOMPARE(attempts.first().value("selected_skills").toArray().first().toObject().value("version").toInt(), 1);
        if (legacy) QVERIFY(!attempts.last().contains("selected_skills"));
        else {
            const QJsonObject updated{{"skill_id", candidate.value("skill_id")}, {"version", 2},
                                      {"content_sha256", candidate.value("content_sha256")}};
            QCOMPARE(attempts.last().value("selected_skills").toArray(), QJsonArray{updated});
        }
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void skillHistoryFromMainLoadsFrozenUsageOnDemand_data()
    {
        QTest::addColumn<QString>("status");
        QTest::newRow("active") << QString("running");
        QTest::newRow("history") << QString("succeeded");
    }
    void skillHistoryFromMainLoadsFrozenUsageOnDemand()
    {
        QFETCH(QString, status);
        Fixture fixture;
        fixture.setPhase(status, status == "running" ? "implementer" : "");
        auto usage = fixtureJson("protocol/examples.json").value("mission_skills_response").toObject();
        usage["task_id"] = MissionId;
        int usageReads = 0;
        fixture.intercept = [&](const Request &request) {
            if (request.path.startsWith("/api/v2/missions?")) {
                Source::json(request, {{"items", QJsonArray{fixture.mission}}, {"total", 1}, {"next_cursor", QJsonValue::Null}});
                return true;
            }
            if (request.method == "GET" && request.path == "/api/v2/missions/" + MissionId.toUtf8() + "/skills") {
                ++usageReads;
                Source::json(request, usage);
                return true;
            }
            return false;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY(ui.find("missionTaskRow", "taskId", MissionId));
        QVERIFY(ui.click(ui.find("missionTaskRow", "taskId", MissionId)));
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(usageReads, 0);
        QVERIFY(ui.click("workflowSkillHistoryButton"));
        QTRY_COMPARE(usageReads, 1);
        QTRY_VERIFY(ui.find("missionSkillHistoryText") && ui.find("missionSkillHistoryText")->isVisible());
        auto *history = ui.find("missionSkillHistoryText");
        for (const auto &selected : usage.value("selected_skills").toArray()) {
            const auto skill = selected.toObject();
            QTRY_VERIFY(history->property("text").toString().contains(
                skill.value("skill_id").toString() + " · v" + QString::number(skill.value("version").toInt())));
            QVERIFY(history->property("text").toString().contains(skill.value("content_sha256").toString()));
        }
        const auto text = history->property("text").toString();
        QVERIFY(text.contains("已提供给模型 · 分析问题根因 · 第 1 次尝试"));
        QVERIFY(text.contains("尚无提供给模型的记录"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void missionMcpChoicesRequireExplicitReadOnlyAnnotation()
    {
        Fixture fixture; Ui ui(fixture); QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton")); QTRY_VERIFY(!ui.mcp.missionLoading());
        QCOMPARE(ui.mcp.missionChoices().size(), 1);
        QVERIFY(ui.click("missionAdvancedToggle"));
        QVERIFY(ui.find("missionMcpChoice", "serverId", McpId));
        QVERIFY(!ui.find("missionMcpChoice", "serverId", Validation0));
        // compatible=true and effect=read cannot substitute for the tool annotation.
        QVERIFY(!ui.find("missionMcpChoice", "serverId", Validation1));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void mcpSelectionRefreshFailureRemovesStaleGrant()
    {
        Fixture fixture; Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n'))); QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY(!ui.mcp.missionLoading());
        QVERIFY(ui.click("missionAdvancedToggle"));
        auto *choice = ui.find("missionMcpChoice", "serverId", McpId); QVERIFY(choice); QVERIFY(ui.click(choice));
        fixture.intercept = [](const Request &request) {
            if (request.path != "/api/v1/mcp/servers") return false;
            Source::json(request, {{"error", QJsonObject{{"code", "mcp_unavailable"}, {"message", "fixture unavailable"}}}}, 503);
            return true;
        };
        QVERIFY(ui.click("missionMcpRefresh")); QTRY_VERIFY(!ui.mcp.missionLoading());
        QVERIFY(!ui.mcp.missionError().isEmpty()); QCOMPARE(ui.mcp.missionChoices().size(), 0);
        QVERIFY(ui.find("missionMcpAvailability")->property("text").toString().contains("服务暂不可用"));
        auto *ragChoice = ui.find("missionUseRag"); QVERIFY(ragChoice); QVERIFY(ragChoice->property("checked").toBool());
        QVERIFY(ui.click(ragChoice)); QVERIFY(!ragChoice->property("checked").toBool());
        QVERIFY(ui.rag.useForTasks()); // A per-task choice does not rewrite the global default.
        ui.find("missionRequestInput")->setProperty("text", "Continue with repository tools");
        ui.find("missionRepositoryInput")->setProperty("text", "D:/fixture/repository");
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionCreateButton")); QTRY_COMPARE(fixture.creates.size(), 1);
        QVERIFY(fixture.creates.first().value("mcp_server_ids").toArray().isEmpty());
        QVERIFY(!fixture.creates.first().value("use_rag").toBool());
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workflowToolContextToggle"));
        QVERIFY(ui.find("missionToolContext")->property("text").toString().contains("MCP：未选择"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void missionToolsFromMainUsesFrozenSelection()
    {
        Fixture fixture; Ui ui(fixture);
        const auto backend = qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_URL");
        const auto serverId = backend.isEmpty() ? McpId : qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_MCP_ID");
        const auto repository = backend.isEmpty() ? QString("D:/fixture/repository") : qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_REPOSITORY");
        const auto restoredTask = qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_TASK_ID");
        const auto toolName = qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_TOOL_NAME", "lookup");
        if (!backend.isEmpty()) {
            QVERIFY(!serverId.isEmpty()); QVERIFY(!repository.isEmpty());
            ui.tasks.setBaseUrl(QUrl(backend)); ui.missions.setBaseUrl(QUrl(backend));
        }
        ui.rag.setUseForTasks(false);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY_WITH_TIMEOUT(!ui.mcp.missionLoading(), 15000);
        QVERIFY2(ui.mcp.missionError().isEmpty(), qPrintable(ui.mcp.missionError()));
        if (restoredTask.isEmpty()) {
            QVERIFY(ui.click("missionAdvancedToggle"));
            if (backend.isEmpty()) QCOMPARE(ui.mcp.missionChoices().size(), 1);
            auto *choice = ui.find("missionMcpChoice", "serverId", serverId); QVERIFY(choice);
            QVERIFY(choice->property("text").toString().contains(toolName));
            QVERIFY(!choice->property("checked").toBool()); QVERIFY(ui.click(choice));
            auto *rag = ui.find("missionUseRag"); QVERIFY(rag); QVERIFY(!rag->property("checked").toBool()); QVERIFY(ui.click(rag));
            QVERIFY(!ui.rag.useForTasks());
            ui.find("missionRequestInput")->setProperty("text", "Inspect selected local tools and current source");
            ui.find("missionRepositoryInput")->setProperty("text", repository);
            QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY_WITH_TIMEOUT(ui.missions.creationReady(), 20000);
            QVERIFY(ui.click("missionCreateButton")); QTRY_VERIFY_WITH_TIMEOUT(!ui.missions.taskId().isEmpty(), 20000);
            if (backend.isEmpty()) {
                QCOMPARE(fixture.creates.size(), 1);
                QCOMPARE(fixture.creates.first().value("mcp_server_ids").toArray(), QJsonArray{serverId});
                QVERIFY(fixture.creates.first().value("use_rag").toBool());
            }
        } else {
            QTRY_VERIFY_WITH_TIMEOUT(!ui.missions.historyLoading(), 15000);
            auto *row = ui.find("missionTaskRow", "taskId", restoredTask); QVERIFY(row); QVERIFY(ui.click(row));
        }
        const bool expectedMcpUsed = qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_EXPECT_MCP_USED", "1") == "1";
        QTRY_VERIFY_WITH_TIMEOUT(ui.missions.snapshot().value("tool_context").toMap().value("mcp").toMap().value("used").toBool() == expectedMcpUsed
            && ui.missions.synchronized(), 30000);
        if (qEnvironmentVariableIsSet("FORGEPILOT_MISSION_TOOLS_EXPECT_RAG_USED"))
            QTRY_COMPARE_WITH_TIMEOUT(ui.missions.snapshot().value("tool_context").toMap().value("rag").toMap().value("used").toBool(),
                qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_EXPECT_RAG_USED") == "1", 30000);
        const auto snapshot = QJsonObject::fromVariantMap(ui.missions.snapshot());
        QVERIFY(snapshot.value("use_rag").toBool()); QCOMPARE(snapshot.value("mcp_server_ids").toArray(), QJsonArray{serverId});
        QVERIFY(snapshot.value("tool_context").toObject().value("rag").toObject().value("requested").toBool());
        auto *context = ui.find("missionToolContext"); QVERIFY(context); QVERIFY(!context->isVisible());
        QVERIFY(ui.click("workflowToolContextToggle")); QTRY_VERIFY(context->isVisible());
        QTRY_VERIFY(context->property("text").toString().contains(toolName));
        if (backend.isEmpty()) {
            QVERIFY(context->property("text").toString().contains("vector_unavailable"));
            QVERIFY(context->property("text").toString().contains("clangd_unavailable"));
            ui.rag.setUseForTasks(false);
            QVERIFY(ui.missions.snapshot().value("use_rag").toBool());
            ui.missions.clearSelection(); ui.missions.selectMission(MissionId);
            QTRY_VERIFY(ui.missions.synchronized());
            QVERIFY(ui.missions.snapshot().value("use_rag").toBool());
        }
        const auto expectedFallback = qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_EXPECTED_FALLBACK");
        if (!expectedFallback.isEmpty()) QTRY_VERIFY(context->property("text").toString().contains(expectedFallback));
        const auto output = qEnvironmentVariable("FORGEPILOT_MISSION_TOOLS_OUTPUT");
        if (!output.isEmpty()) {
            QVERIFY(QDir().mkpath(output)); QFile evidence(output + "/mission-tools-qt-snapshot.json");
            QVERIFY(evidence.open(QIODevice::WriteOnly)); evidence.write(QJsonDocument(QJsonObject::fromVariantMap(ui.missions.snapshot())).toJson());
            ui.window->update(); QTest::qWait(80); QVERIFY(ui.window->grabWindow().save(output + "/mission-tools-main.png"));
        }
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void legacyWorkModePreviewsBaselineBeforeCreatingMission_data()
    {
        QTest::addColumn<bool>("switchChat");
        QTest::newRow("same-repository") << false;
        QTest::newRow("other-repository") << true;
    }
    void legacyWorkModePreviewsBaselineBeforeCreatingMission()
    {
        QFETCH(bool, switchChat);
        const QString nextRepository = switchChat ? "D:/fixture/other" : "D:/fixture/repository";
        Fixture fixture;
        fixture.intercept = [](const Request &request) {
            const bool other = request.path == "/api/v1/conversations/" + B.toUtf8();
            if(other || request.path == "/api/v1/conversations/" + A.toUtf8()) {
                Source::json(request, {{"conversation_id", other ? B : A}, {"title", "旧工作对话"},
                    {"repository_path", other ? "D:/fixture/other" : "D:/fixture/repository"}, {"messages", QJsonArray{}}});
                return true;
            }
            return false;
        };
        Ui ui(fixture); QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        ui.conversations.selectConversation(A); QTRY_VERIFY(!ui.conversations.loading());
        ui.conversations.setMode("task"); ui.conversations.sendMessage("旧入口的新代码任务");
        QCOMPARE(fixture.creates.size(), 0);
        QTRY_COMPARE(ui.find("missionRepositoryInput")->property("text").toString(), QString("D:/fixture/repository"));
        QVERIFY(!ui.find("missionCreateButton")->isEnabled());
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.find("missionBaselineSummary")->property("text").toString().contains(QString(40, 'a')));
        QVERIFY(ui.click("missionBaselineFile"));
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionAdvancedToggle"));
        QVERIFY(ui.click(ui.find("missionMcpChoice", "serverId", McpId)));
        QVERIFY(ui.click("missionUseRag"));
        QVERIFY(ui.click("freeChatModeButton"));
        if (switchChat) {
            ui.conversations.selectConversation(B);
            QTRY_COMPARE(ui.conversations.repositoryPath(), nextRepository);
        }
        ui.conversations.setMode("task"); ui.conversations.sendMessage("聊天中的下一项需求");
        QCOMPARE(ui.find("missionRequestInput")->property("text").toString(), QString("聊天中的下一项需求"));
        QCOMPARE(ui.find("missionRepositoryInput")->property("text").toString(), nextRepository);
        QVERIFY(ui.missions.creationPreview().isEmpty());
        QVERIFY(!ui.find("missionCreateButton")->isEnabled());
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(!ui.find("missionBaselineFile")->property("checked").toBool());
        QVERIFY(ui.click(ui.find("missionCreateButton")));
        QTRY_COMPARE(fixture.creates.size(), 1);
        QTRY_COMPARE(ui.missions.taskId(), MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.window->property("agentMode").toBool());
        QCOMPARE(ui.conversations.mode(), QString("chat"));
        QCOMPARE(fixture.creates.first().value("repository_path").toString(), nextRepository);
        QCOMPARE(fixture.creates.first().value("request").toString(), QString("聊天中的下一项需求"));
        QCOMPARE(fixture.creates.first().value("mcp_server_ids"), QJsonValue(QJsonArray{}));
        QCOMPARE(fixture.creates.first().value("use_rag"), QJsonValue(ui.rag.useForTasks()));
        for(const auto &request:fixture.source.requests)
            if(request.method == "POST") QVERIFY(request.path == "/api/v2/missions" || request.path == "/api/v2/missions/preview"
                || request.path == "/api/v2/skills/recommendations");
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void convertedChatDraftCreatesMissionWithoutHistory()
    {
        Fixture fixture; Ui ui(fixture, 1600, 1400);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        auto *prompt=ui.find("conversationPrompt"); QVERIFY(prompt);
        prompt->setProperty("text", "选定的任务文本");
        QTRY_VERIFY(ui.find("convertDraftToAgentTaskButton")->isVisible());
        QVERIFY(ui.click(ui.find("convertDraftToAgentTaskButton")));
        auto *input=ui.find("missionRequestInput"); QVERIFY(input);
        QTRY_COMPARE(input->property("text").toString(), QString("选定的任务文本"));
        QCOMPARE(fixture.creates.size(), 0);
        auto *repository=ui.find("missionRepositoryInput"); QVERIFY(repository);
        input->setProperty("text", "编辑中的任务需求");
        repository->setProperty("text", "D:/fixture/repository");
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionBaselineFile"));
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionBaselineConfirmation"));

        QVERIFY(ui.click("freeChatModeButton"));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QCOMPARE(input->property("text").toString(), QString("编辑中的任务需求"));
        QCOMPARE(repository->property("text").toString(), QString("D:/fixture/repository"));
        QVERIFY(ui.find("missionCreateButton")->isEnabled());

        QVERIFY(ui.click("freeChatModeButton"));
        QVERIFY(ui.click("convertDraftToAgentTaskButton"));
        QCOMPARE(input->property("text").toString(), QString("选定的任务文本"));
        QVERIFY(repository->property("text").toString().isEmpty());
        QVERIFY(ui.missions.creationPreview().isEmpty());
        QVERIFY(!ui.find("missionCreateButton")->isEnabled());

        repository->setProperty("text", "D:/fixture/other");
        QVERIFY(ui.click("missionAdvancedToggle"));
        QVERIFY(ui.click(ui.find("missionMcpChoice", "serverId", McpId)));
        QVERIFY(ui.click("missionUseRag"));
        repository->setProperty("text", "D:/fixture/repository");
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_COMPARE(fixture.creates.size(), 1);
        QVERIFY(fixture.creates.first().value("baseline_paths").toArray().isEmpty());
        QCOMPARE(fixture.creates.first().value("mcp_server_ids"), QJsonValue(QJsonArray{}));
        QCOMPARE(fixture.creates.first().value("use_rag"), QJsonValue(ui.rag.useForTasks()));
        QCOMPARE(fixture.creates.first().value("request").toString(), QString("选定的任务文本"));
        QVERIFY(!fixture.creates.first().contains("conversation_id"));
        QCOMPARE(ui.conversations.mode(), QString("chat"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
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
        QVERIFY(!create->isEnabled());
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(ui.missions.creationReady());
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
    void draftToolsSurviveFailedCreateAndResetForNextMission()
    {
        Fixture fixture;
        QJsonObject failedRequest;
        fixture.intercept = [&](const Request &request) {
            if (request.path != "/api/v2/missions" || request.method != "POST" || !failedRequest.isEmpty())
                return false;
            failedRequest = request.body;
            Source::json(request, {{"error", QJsonObject{{"code", "preview_stale"}, {"message", "Refresh the preview"}}}}, 409);
            return true;
        };
        Ui ui(fixture);
        ui.rag.setUseForTasks(false);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workflowDemandToggle"));
        auto *prompt = ui.find("missionRequestInput"); QVERIFY(prompt);
        prompt->setProperty("text", "Task A");
        ui.find("missionRepositoryInput")->setProperty("text", "D:/fixture/repository");
        QTRY_VERIFY(!ui.mcp.missionLoading());
        QVERIFY(ui.click("missionAdvancedToggle"));
        QVERIFY(ui.click(ui.find("missionMcpChoice", "serverId", McpId)));
        QVERIFY(ui.click("missionUseRag"));
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("workflowDemandToggle"));
        QVERIFY(ui.click("workflowDemandToggle"));
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_VERIFY(!ui.missions.creationError().isEmpty());
        QCOMPARE(failedRequest.value("mcp_server_ids"), QJsonValue(QJsonArray{McpId}));
        QCOMPARE(failedRequest.value("use_rag"), QJsonValue(true));

        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_COMPARE(fixture.creates.size(), 1);
        QCOMPARE(fixture.creates.first().value("request"), QJsonValue("Task A"));
        QCOMPARE(fixture.creates.first().value("mcp_server_ids"), failedRequest.value("mcp_server_ids"));
        QCOMPARE(fixture.creates.first().value("use_rag"), failedRequest.value("use_rag"));
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workflowDemandToggle"));
        prompt->setProperty("text", "Task B");
        QVERIFY(ui.click("missionPreviewButton")); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_COMPARE(fixture.creates.size(), 2);
        const auto next = fixture.creates.last();
        QCOMPARE(next.value("request"), QJsonValue("Task B"));
        QCOMPARE(next.value("repository_path"), failedRequest.value("repository_path"));
        QCOMPARE(next.value("mcp_server_ids"), QJsonValue(QJsonArray{}));
        QCOMPARE(next.value("use_rag"), QJsonValue(false));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void selectedFilesRequirePatchPreviewAndFreshConfirmation()
    {
        Fixture fixture; Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.find("missionRequestInput")->setProperty("text", "使用选定的源码改动");
        ui.find("missionRepositoryInput")->setProperty("text", "D:/fixture/repository");
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(ui.missions.creationReady());
        auto *file=ui.find("missionBaselineFile"); QVERIFY(file);
        QVERIFY(ui.click(file)); QVERIFY(!ui.missions.creationReady());
        QVERIFY(!ui.find("missionCreateButton")->isEnabled());
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.find("missionBaselinePatch")->property("text").toString().contains("+selected"));
        auto *confirmation=ui.find("missionBaselineConfirmation"); QVERIFY(confirmation);
        QVERIFY(!confirmation->property("checked").toBool()); QVERIFY(!ui.find("missionCreateButton")->isEnabled());
        QVERIFY(ui.click(confirmation)); QVERIFY(ui.find("missionCreateButton")->isEnabled());
        QVERIFY(ui.click("missionAdvancedToggle"));
        QVERIFY(ui.click(ui.find("missionUsePresets")));
        QVERIFY(!ui.missions.creationReady()); QVERIFY(!confirmation->property("checked").toBool());
        ui.find("missionConfigurePreset")->setProperty("text", "unsupported");
        fixture.intercept=[](const Request &request) {
            if(request.path != "/api/v2/missions/preview" || !request.body.contains("configure_preset")) return false;
            Source::json(request, {{"error", QJsonObject{{"code", "preset_fields"},
                {"message", "test preset output.outputLogFile 不支持迁移到受控构建目录"}, {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}}, 422);
            return true;
        };
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(!ui.missions.creationError().isEmpty());
        QVERIFY(ui.missions.creationError().contains("output.outputLogFile")); QVERIFY(!ui.find("missionCreateButton")->isEnabled());
        QCOMPARE(fixture.creates.size(), 0);
        QVERIFY(ui.click(ui.find("missionUsePresets")));
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(!confirmation->property("checked").toBool()); QVERIFY(ui.click(confirmation));
        const auto confirmedToken = ui.missions.creationPreview().value("preview_token").toString();
        QVERIFY(ui.click(ui.find("missionCreateButton"))); QTRY_COMPARE(fixture.creates.size(), 1);
        QCOMPARE(fixture.creates.first().value("baseline_paths").toArray(), QJsonArray{"main.cpp"});
        QCOMPARE(fixture.creates.first().value("baseline_confirmed").toBool(), true);
        QCOMPARE(fixture.creates.first().value("preview_token").toString(), confirmedToken);
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
        QVERIFY(ui.click("workflowFollowButton"));
        QTRY_VERIFY(!page->property("selectionPinned").toBool());
        QTRY_COMPARE(page->property("selectedNodeId").toString(), QString("validation_1"));
        fixture.setPhase("running", "reviewer_1", 10);
        ui.missions.refresh();
        QTRY_COMPARE(ui.missions.revision(), 10);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_COMPARE(page->property("selectedNodeId").toString(), QString("reviewer_1"));
        QVERIFY(!page->property("selectionPinned").toBool());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void unrelatedSnapshotUpdatesPreserveReaderPosition_data()
    {
        QTest::addColumn<QString>("artifactType");
        QTest::addColumn<QString>("nodeId");
        QTest::addColumn<QString>("stageId");
        QTest::addColumn<QString>("selectorName");
        QTest::addColumn<QString>("selectionProperty");
        QTest::addColumn<QString>("outputName");
        QTest::newRow("selected-file") << QString("ChangeSet") << QString("implementer") << QString("implementation")
            << QString("workflowFileSelect") << QString("selectedFile") << QString("workflowNodeDiff");
        QTest::newRow("selected-command") << QString("ValidationReport") << QString("validation_0") << QString("verification")
            << QString("workflowCommandSelect") << QString("selectedCommand") << QString("workflowNodeCommandOutput");
    }
    void unrelatedSnapshotUpdatesPreserveReaderPosition()
    {
        QFETCH(QString, artifactType); QFETCH(QString, nodeId); QFETCH(QString, stageId);
        QFETCH(QString, selectorName); QFETCH(QString, selectionProperty); QFETCH(QString, outputName);
        Fixture fixture;
        auto artifacts = fixture.artifacts.value("items").toArray();
        for (int i = 0; i < artifacts.size(); ++i) {
            auto artifact = artifacts[i].toObject();
            if (artifact.value("artifact_type") != artifactType) continue;
            auto content = artifact.value("content").toObject();
            const auto entriesKey = artifactType == "ChangeSet" ? "files" : "commands";
            auto entries = content.value(entriesKey).toArray();
            auto second = entries.first().toObject();
            if (artifactType == "ChangeSet") {
                second["path"] = "src/worker.cpp";
                second["unified_diff"] = QString("+ reader position fixture\n").repeated(1500);
            } else {
                second["command_summary"] = "ctest --output-on-failure";
                second["output_summary"] = QString("reader position fixture\n").repeated(1500);
            }
            entries.append(second); content[entriesKey] = entries;
            artifact["content"] = content; artifacts[i] = artifact;
        }
        fixture.artifacts["items"] = artifacts;
        fixture.setPhase("running", "reviewer_0");
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_VERIFY(ui.find("workflowStage", "stageId", stageId));
        QVERIFY(ui.expandStage(stageId));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", nodeId));
        QVERIFY(ui.chooseNode(nodeId));
        QPointer<QQuickItem> panel = ui.find("workflowArtifactPanel"); QVERIFY(panel);
        QPointer<QQuickItem> selector = ui.find(selectorName); QVERIFY(selector);
        QTRY_COMPARE(selector->property("count").toInt(), 2);
        QVERIFY(ui.click(selector));
        QTest::keyClick(ui.window, Qt::Key_End);
        QTest::keyClick(ui.window, Qt::Key_Return);
        QTRY_COMPARE(panel->property(selectionProperty.toUtf8()).toInt(), 1);
        QCOMPARE(selector->property("currentIndex").toInt(), 1);
        QPointer<QQuickItem> output = ui.find(outputName); QVERIFY(output);
        QVERIFY(ui.click(ui.find("workflowNextOutput", {}, {}, output)));
        QTRY_COMPARE(output->property("offset").toInt(), output->property("pageSize").toInt());
        const auto offset = output->property("offset").toInt();
        auto *scroll = ui.find("workflowDetailsScroll"); QVERIFY(scroll);
        QPointer<QQuickItem> viewport = qobject_cast<QQuickItem *>(scroll->property("contentItem").value<QObject *>());
        QVERIFY(viewport);
        QTRY_VERIFY(viewport->property("contentHeight").toReal() > viewport->height() + 160);
        QVERIFY(viewport->setProperty("contentY", 160));
        QTRY_COMPARE(viewport->property("contentY").toReal(), 160.0);

        // Only another node progresses. Repeated coherent snapshots must not
        // rebuild this pinned reader or reset its chosen entry, page or scroll.
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        for (int revision = 9; revision <= 10; ++revision) {
            const QString activeNode = revision == 9 ? "repair_1" : "validation_1";
            fixture.setPhase("running", activeNode, revision);
            ui.missions.refresh();
            QTRY_COMPARE(ui.missions.revision(), revision);
            QTRY_VERIFY(ui.missions.synchronized());
            QTRY_COMPARE(ui.missions.graph()->revision(), revision);
            const auto viewHasActiveNode = [&] {
                const auto value = page->property("nodeRecords");
                const auto records = value.metaType() == QMetaType::fromType<QJSValue>()
                    ? value.value<QJSValue>().toVariant().toList() : value.toList();
                for (const auto &record : records) {
                    const auto node = record.toMap();
                    if (node.value("nodeId") == activeNode && node.value("status") == "running") return true;
                }
                return false;
            };
            QTRY_VERIFY(viewHasActiveNode());
            QVERIFY(panel); QVERIFY(selector); QVERIFY(output); QVERIFY(viewport);
            QCOMPARE(ui.find("workflowArtifactPanel"), panel.data());
            QCOMPARE(page->property("selectedNodeId").toString(), nodeId);
            QVERIFY(page->property("selectionPinned").toBool());
            QCOMPARE(panel->property(selectionProperty.toUtf8()).toInt(), 1);
            QCOMPARE(selector->property("currentIndex").toInt(), 1);
            QCOMPARE(output->property("offset").toInt(), offset);
            QCOMPARE(viewport->property("contentY").toReal(), 160.0);
        }
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void pinnedReaderUpdatesWhenNodePublishesAnotherAttempt()
    {
        Fixture fixture; fixture.setPhase("running", "reviewer_0"); Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId); QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.expandStage("implementation")); QVERIFY(ui.chooseNode("implementer"));
        QTRY_VERIFY(ui.find("workflowNodeDiff"));
        QPointer<QQuickItem> previous = ui.find("workflowNodeDiff");
        auto artifacts = fixture.artifacts.value("items").toArray();
        const QString replacementId = "10000000-0000-4000-a000-000000000036";
        for (const auto &value : fixture.artifacts.value("items").toArray()) {
            auto replacement = value.toObject();
            if (replacement.value("artifact_type") != "ChangeSet") continue;
            replacement["artifact_id"] = replacementId;
            auto content = replacement.value("content").toObject();
            auto files = content.value("files").toArray();
            auto file = files.first().toObject(); file["unified_diff"] = "+ corrected next attempt\n";
            files[0] = file; content["files"] = files; replacement["content"] = content;
            replacement["content_sha256"] = QString::fromLatin1(QCryptographicHash::hash(
                QJsonDocument(content).toJson(QJsonDocument::Compact), QCryptographicHash::Sha256).toHex());
            artifacts.append(replacement);
        }
        fixture.artifacts["items"] = artifacts;
        fixture.setOutputs("implementer", QJsonArray{replacementId});
        fixture.setPhase("running", "validation_1", 9); ui.missions.refresh();
        QTRY_COMPARE(ui.missions.revision(), 9); QTRY_VERIFY(ui.missions.synchronized());
        QTRY_VERIFY(!previous);
        auto *output = ui.find("workflowNodeDiff"); QVERIFY(output);
        QCOMPARE(output->property("value").toString(), QString("+ corrected next attempt\n"));
        QCOMPARE(output->property("offset").toInt(), 0);
        auto *page = ui.find("workflowPage"); QVERIFY(page);
        QCOMPARE(page->property("selectedNodeId").toString(), QString("implementer"));
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
        QQuickItem *stage = nullptr, *expand = nullptr;
        QTRY_VERIFY((stage = ui.find("workflowStage", "stageId", "analysis")));
        QTRY_VERIFY((expand = ui.find("workflowStageExpand", "stageId", "analysis")));
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
    void futureArtifactAlongsideValidationKeepsMissionDetailsReadable()
    {
        Fixture fixture;
        const QString futureId = "10000000-0000-4000-a000-000000000023";
        const QString futureType = "FutureDiagnostics";
        const QString opaqueText = "Future-only diagnostic payload";
        QJsonObject content{{"summary", opaqueText}};
        auto items = fixture.artifacts.value("items").toArray();
        items.append(QJsonObject{{"artifact_id", futureId}, {"artifact_type", futureType},
            {"schema_version", "1.0"}, {"task_id", MissionId}, {"producer", "orchestrator.v1"},
            {"producer_node", "validation_0"}, {"created_at", "2026-10-27T08:01:00Z"},
            {"parent_artifact_ids", QJsonArray{Validation0}}, {"content", content},
            {"content_sha256", QString::fromLatin1(QCryptographicHash::hash(
                QJsonDocument(content).toJson(QJsonDocument::Compact), QCryptographicHash::Sha256).toHex())}});
        fixture.artifacts["items"] = items;
        fixture.setOutputs("validation_0", QJsonArray{Validation0, futureId});
        fixture.setPhase("succeeded", "");
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(ui.missions.validation()->reports().size(), 1);
        QCOMPARE(ui.missions.validation()->status(), QString("passed"));
        QCOMPARE(ui.missions.diff()->patch(), QString("@@ -1 +1 @@\n-old\n+new"));

        QVERIFY(ui.expandStage("plan"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "generate_plan"));
        QVERIFY(ui.chooseNode("generate_plan"));
        QTRY_VERIFY(showsText(ui.find("workflowDetails"), "Cancel, then verify."));

        QVERIFY(ui.expandStage("verification"));
        QTRY_VERIFY(ui.find("workflowNode", "nodeId", "validation_0"));
        QVERIFY(ui.chooseNode("validation_0"));
        QQuickItem *report = nullptr;
        QTRY_VERIFY((report = ui.find("workflowArtifactPanel", "kind", "ValidationReport")));
        QVERIFY(showsText(report, "Validation passed."));
        QVERIFY(showsText(report, "All passed."));
        auto *future = ui.find("workflowArtifactPanel", "kind", futureType);
        QVERIFY(showsText(future, futureType));
        QVERIFY(showsText(future, "当前客户端暂不支持详情"));
        QVERIFY(!showsText(future, opaqueText));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void validationArtifactsStayWithTheirOwnRound()
    {
        Fixture fixture; fixture.addSecondValidation(); fixture.setPhase("succeeded", ""); Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_VERIFY(ui.find("workflowStage", "stageId", "verification"));
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
    void scopePanelFollowsExplicitActionOnCustomNode()
    {
        Fixture fixture;
        const QString writer = "apply_changes";
        fixture.nodes.append(QJsonObject{{"node_id", writer}, {"node_type", "agent"},
            {"pending_action", "scope_expansion"}, {"result", QJsonValue::Null}});
        fixture.definitions.append(QJsonObject{{"node_id", writer}, {"node_type", "agent"},
            {"handler", "apply_patch"}, {"dependencies", QJsonArray{}}, {"agent_id", "writer.v1"},
            {"agent_definition_hash", QJsonValue::Null}, {"settings", QJsonObject{}}});
        auto proposal = fixtureJson("protocol/examples.json").value("mission_scope_expansion_preview").toObject();
        proposal["task_id"] = MissionId; proposal["node_id"] = writer;
        auto items = fixture.artifacts.value("items").toArray();
        QJsonObject spec, plan;
        for (const auto &item : items) {
            const auto artifact = item.toObject();
            if (artifact.value("artifact_type") == "TaskSpec") spec = artifact;
            if (artifact.value("artifact_type") == "ExecutionPlan") plan = artifact;
        }
        const auto oldSpec = spec.value("artifact_id"), oldPlan = plan.value("artifact_id");
        spec["artifact_id"] = proposal.value("task_spec_artifact_id");
        spec["parent_artifact_ids"] = QJsonArray{oldSpec};
        spec["content_sha256"] = proposal.value("task_spec_hash");
        plan["artifact_id"] = proposal.value("plan_artifact_id");
        plan["parent_artifact_ids"] = QJsonArray{spec.value("artifact_id"), oldPlan};
        plan["content_sha256"] = proposal.value("plan_hash");
        auto content = plan.value("content").toObject();
        content["task_spec_artifact_id"] = spec.value("artifact_id");
        plan["content"] = content;
        items.append(spec); items.append(plan);
        fixture.artifacts["items"] = items;
        fixture.setPhase("awaiting_human", writer, proposal.value("revision").toInteger());
        fixture.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/scope-expansion")) return false;
            Source::json(r, proposal);
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.approval()->scopeAvailable());
        QVERIFY(ui.click("workflowAttentionButton"));
        QTRY_VERIFY(ui.find("workflowScopePanel") && ui.find("workflowScopePanel")->isVisible());
        auto *approve = ui.find("workflowScopeApprove");
        QVERIFY(approve && approve->isEnabled());
        QSignalSpy accepted(ui.missions.approval(), &ApprovalController::scopeExpansionApproved);
        QVERIFY(ui.click(approve));
        QTRY_COMPARE(fixture.approvals.size(), 1);
        const auto decision = fixture.approvals.first();
        QCOMPARE(decision.value("decision").toString(), "approved");
        QCOMPARE(decision.value("approval_type").toString(), "scope_expansion");
        QCOMPARE(decision.value("expected_revision"), proposal.value("revision"));
        for (const auto key : {"task_spec_hash", "plan_hash", "scope_hash"})
            QCOMPARE(decision.value(key), proposal.value(key));
        QTRY_COMPARE(accepted.size(), 1);
        QTRY_COMPARE(ui.missions.status(), QString("running"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void missionSkillsFromMain()
    {
        const QString url = qEnvironmentVariable("FORGEPILOT_SKILLS_URL");
        if (url.isEmpty()) QSKIP("Run client/tests/run_mission_skills_integration.py for the local backend");
        const QString phase = qEnvironmentVariable("FORGEPILOT_SKILLS_PHASE");
        Ui ui{QUrl(url)};
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        if (phase == "publish") {
            const auto source = qEnvironmentVariable("FORGEPILOT_SKILLS_SOURCE_ID");
            QTRY_VERIFY(ui.find("missionTaskRow", "taskId", source));
            QVERIFY(ui.click(ui.find("missionTaskRow", "taskId", source)));
            QTRY_VERIFY(ui.missions.synchronized() && ui.missions.snapshot().value("status") == "cancelled");
            QVERIFY(ui.click("workflowExtractSkillButton"));
            auto fill = [&](const QString &field, const QString &value) {
                auto *input = ui.find(field);
                if (!ui.click(input)) return false;
                QTest::keyClick(ui.window, Qt::Key_A, Qt::ControlModifier);
                for (const QChar ch : value) QTest::keyClick(ui.window, ch.toLatin1());
                return input->property("text").toString() == value;
            };
            QTRY_VERIFY(ui.find("skillDraftNote") && ui.find("skillDraftNote")->isVisible());
            QVERIFY(fill("skillDraftNote", "Checked at cancellation."));
            QTRY_VERIFY(ui.find("skillDraftCreateManual") && ui.find("skillDraftCreateManual")->isEnabled());
            QVERIFY(ui.click("skillDraftCreateManual"));
            QTRY_VERIFY(ui.find("skillDraftName") && ui.find("skillDraftName")->isVisible());
            // Locate a specific JSON pointer in the immutable TaskSpec. The
            // runner reads the expected ID and value independently over HTTP.
            const auto locator = qEnvironmentVariable("FORGEPILOT_SKILLS_SOURCE_ARTIFACT") + "/objective";
            auto *pointer = ui.find("skillDraftEvidence", "text", QStringLiteral("来源记录 · ") + locator);
            QVERIFY(ui.click(pointer));
            QTRY_VERIFY(ui.find("skillDraftSourceRecord") && ui.find("skillDraftSourceRecord")->isVisible());
            QCOMPARE(ui.find("skillDraftSourceRecord")->property("text").toString(),
                source + "\n\n" + locator + "\n\n" + qEnvironmentVariable("FORGEPILOT_SKILLS_SOURCE_VALUE"));
            QTest::keyClick(ui.window, Qt::Key_Escape);
            QTRY_VERIFY(!ui.find("skillDraftSourceRecord") || !ui.find("skillDraftSourceRecord")->isVisible());
            QTest::keyClick(ui.window, Qt::Key_Escape);
            QTRY_VERIFY(!ui.find("skillDraftName") || !ui.find("skillDraftName")->isVisible());
            QVERIFY(ui.click("workflowExtractSkillButton"));
            QTRY_VERIFY(ui.find("skillDraftListItem") && ui.find("skillDraftListItem")->isEnabled());
            QVERIFY(ui.click("skillDraftListItem"));
            QTRY_VERIFY(ui.find("skillDraftObservation") && ui.find("skillDraftObservation")->isVisible());
            QCOMPARE(ui.find("skillDraftObservation")->property("text").toString(), QString("Checked at cancellation."));
            QVERIFY(fill("skillDraftName", "Clamp investigation"));
            QVERIFY(fill("skillDraftTriggers", "negative inputs"));
            QVERIFY(fill("skillDraftBody", "Check the value before clamping."));
            QVERIFY(fill("skillDraftBoundaries", "Only signed input validation."));
            for (const auto *name : {"skillDraftRootStage", "skillDraftImpactStage"}) {
                auto *stage = ui.find(name);
                QVERIFY(stage);
                if (!stage->property("checked").toBool()) QVERIFY(ui.click(stage));
                QVERIFY(stage->property("checked").toBool());
            }
            QVERIFY(ui.click("skillDraftSave"));
            QTRY_VERIFY(ui.find("skillDraftPublish") && ui.find("skillDraftPublish")->isEnabled());
            QVERIFY(ui.click("skillDraftPublish"));
            QTRY_VERIFY(ui.find("skillDraftPublishedVersion") && ui.find("skillDraftPublishedVersion")->isVisible());
        } else {
            QVERIFY(phase == "selected" || phase == "zero");
            const bool selected = phase == "selected";
            ui.find("missionRequestInput")->setProperty("text", "Clamp negative inputs");
            ui.find("missionRepositoryInput")->setProperty("text", qEnvironmentVariable("FORGEPILOT_SKILLS_REPO"));
            ui.find("missionTaskType")->setProperty("currentIndex", 1);
            QTRY_VERIFY(ui.find("missionSkillChoice") && ui.find("missionSkillChoice")->isVisible());
            QVERIFY(!ui.find("missionSkillChoice")->property("checked").toBool());
            QVERIFY(ui.click("missionPreviewButton"));
            QTRY_VERIFY_WITH_TIMEOUT(ui.missions.creationReady(), 20000);
            QTRY_VERIFY(ui.find("missionSkillChoice") && ui.find("missionSkillChoice")->isEnabled());
            auto *choice = ui.find("missionSkillChoice");
            QVERIFY(choice->property("text").toString().contains("Clamp investigation"));
            QVERIFY(!choice->property("checked").toBool());
            if (selected) {
                QVERIFY(ui.click(choice));
                QVERIFY(choice->property("checked").toBool());
            }
            QVERIFY(ui.click("missionCreateButton"));
            QTRY_VERIFY_WITH_TIMEOUT(ui.missions.approval()->available() && ui.missions.synchronized(), 30000);
            QVERIFY(ui.click("workflowSkillHistoryButton"));
            auto *history = ui.find("missionSkillHistoryText");
            QVERIFY(history);
            QTRY_VERIFY(history->property("text").toString().contains(selected
                ? QStringLiteral("已提供给模型 · 分析问题根因") : QStringLiteral("创建此任务时未选择技能")));
            if (selected) {
                QVERIFY(history->property("text").toString().contains(QStringLiteral("已提供给模型 · 分析影响与测试")));
                QVERIFY(history->property("text").toString().contains(qEnvironmentVariable("FORGEPILOT_SKILLS_HASH")));
            }
        }
        QFile receipt(qEnvironmentVariable("FORGEPILOT_SKILLS_RECEIPT"));
        QVERIFY(!ui.missions.taskId().isEmpty() && receipt.open(QIODevice::WriteOnly));
        QCOMPARE(receipt.write(ui.missions.taskId().toUtf8()), ui.missions.taskId().size());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void scopeExpansionFromMain()
    {
        const auto backend = qEnvironmentVariable("FORGEPILOT_SCOPE_URL");
        if (backend.isEmpty()) QSKIP("Run client/tests/run_scope_expansion_integration.py for the local backend");
        const auto mode = qEnvironmentVariable("FORGEPILOT_SCOPE_MODE");
        const bool repair = mode == "approve";
        const QString writer = repair ? "repair_1" : "implementer";
        Ui ui{QUrl(backend)};
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.find("missionRequestInput")->setProperty("text", "Clamp negative inputs via a shared header");
        ui.find("missionRepositoryInput")->setProperty("text", qEnvironmentVariable("FORGEPILOT_SCOPE_REPOSITORY"));
        QVERIFY(ui.click("missionPreviewButton"));
        QTRY_VERIFY_WITH_TIMEOUT(ui.missions.creationReady(), 20000);
        QVERIFY(ui.click("missionCreateButton"));
        QTRY_VERIFY_WITH_TIMEOUT(ui.missions.approval()->available(), 30000);
        const auto task = ui.missions.taskId();
        QVERIFY(!task.isEmpty());
        QVERIFY(ui.click("workflowAttentionButton"));
        QTRY_VERIFY(ui.find("missionApproveButton") && ui.find("missionApproveButton")->isEnabled());
        QVERIFY(ui.click("missionApproveButton"));
        auto pausedWriter = [&] {
            for (int i = 0; i < ui.missions.graph()->count(); ++i) {
                const auto row = ui.missions.graph()->get(i);
                if (row.value("nodeId") == writer) return row.value("status") == "awaiting_human";
            }
            return false;
        };
        QTRY_VERIFY_WITH_TIMEOUT(pausedWriter() && ui.missions.synchronized(), 90000);
        auto *page = ui.find("workflowPage");
        QVERIFY(page);
        if (repair) {
            QTRY_COMPARE(page->property("attentionNodeId").toString(), writer);
            QVERIFY(ui.click("workflowAttentionButton"));
        } else {
            QVERIFY(ui.expandStage("implementation"));
            QTRY_VERIFY(ui.find("workflowNode", "nodeId", writer));
            QVERIFY(ui.chooseNode(writer));
        }
        QTRY_COMPARE(page->property("selectedNodeId").toString(), writer);
        auto findVisible = [&](const QString &id) {
            auto *item = ui.find(id);
            return item && item->isVisible();
        };
        if (mode == "legacy") {
            QTRY_VERIFY(ui.find("workflowReadableText", "text", "Please allow clamp.hpp for STEP-1"));
            QVERIFY(!findVisible("workflowScopeApprove"));
        } else {
            QTRY_VERIFY(findVisible("workflowScopeApprove"));
            auto displayed = [&](const QString &id) {
                auto *block = ui.find(id);
                return block && block->isVisible() ? block->property("value").toString() : QString{};
            };
            QVERIFY(displayed("workflowScopeOriginal").contains("src/**"));
            const auto files = displayed("workflowScopeFiles");
            for (const auto text : {"clamp.hpp", "STEP-1", "AC-1", "Share the clamp implementation"})
                QVERIFY2(files.contains(text), qPrintable(files));
            auto *approval = ui.missions.approval();
            const auto checkpoint = approval->scopeExpansion().value("checkpoint_id").toString();
            QVERIFY(!checkpoint.isEmpty());
            QVERIFY(displayed("workflowScopeReplay").contains(checkpoint));
            QVERIFY(displayed("workflowScopeDiff").contains("// awaiting approval for header"));
            QVERIFY(ui.capture("writer-request"));
            ui.reveal(ui.find("workflowScopeDiff"));
            QVERIFY(ui.capture("writer-replay-and-diff"));
            if (repair) {
                const auto revision = ui.missions.revision();
                QVERIFY(ui.click("workflowScopeApprove"));
                QTRY_VERIFY_WITH_TIMEOUT(approval->scopeAvailable() && ui.missions.revision() > revision, 15000);
                QCOMPARE(ui.missions.status(), QString("awaiting_human"));
                QVERIFY(displayed("workflowScopeReplay").contains(checkpoint));
            }
            // Two physical clicks at the same position before the request resolves.
            auto *action = ui.find(repair ? "workflowScopeApprove" : "workflowScopeReject");
            QVERIFY(action && action->isVisible() && action->isEnabled());
            // Refresh can reset the details scroll while QML is laying out the new proposal.
            // Send both clicks only once the button's center is in the actual viewport.
            auto revealDecision = [&] {
                ui.reveal(action);
                const QPointF center(action->width() / 2, action->height() / 2);
                for (auto *parent = action->parentItem(); parent; parent = parent->parentItem())
                    if (parent->clip() && !parent->contains(action->mapToItem(parent, center))) return false;
                return ui.window->contentItem()->contains(action->mapToScene(center));
            };
            QTRY_VERIFY(revealDecision());
            const auto point = action->mapToScene(QPointF(action->width() / 2, action->height() / 2)).toPoint();
            QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier, point);
            QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier, point);
            if (repair) {
                QTRY_VERIFY_WITH_TIMEOUT(!page->property("selectionPinned").toBool(), 15000);
                QTRY_COMPARE_WITH_TIMEOUT(ui.missions.status(), QString("succeeded"), 90000);
                QTRY_COMPARE(page->property("selectedNodeId").toString(), QString("delivery_bundle"));
                QTRY_VERIFY(findVisible("missionDeliveryPreviewButton"));
                QTRY_VERIFY(ui.find("missionDeliveryPreviewButton")->isEnabled());
                QVERIFY(ui.click("missionDeliveryPreviewButton"));
                QTRY_VERIFY_WITH_TIMEOUT(ui.missions.delivery()->canConfirm(), 15000);
                QVERIFY(ui.capture("delivery"));
            } else {
                QTRY_COMPARE_WITH_TIMEOUT(approval->scopeExpansion().value("status").toString(), QString("rejected"), 15000);
                QVERIFY(!approval->scopeAvailable());
                QVERIFY(displayed("workflowScopeDiff").contains("// awaiting approval for header"));
                QVERIFY(ui.capture("rejected"));
            }
        }
        if (!repair) {
            QVERIFY(ui.click("workflowScopeCancel"));
            QVERIFY(ui.click("workflowCancelConfirm"));
            QTRY_COMPARE_WITH_TIMEOUT(ui.missions.status(), QString("cancelled"), 15000);
        }
        QCOMPARE(ui.missions.taskId(), task);
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
    void deliveryPanelVerifiesInterruptedSquash_data()
    {
        QTest::addColumn<QString>("outcome");
        QTest::addColumn<QString>("label");
        QTest::newRow("published") << QString("completed") << QString("合并已完成");
        QTest::newRow("rolled-back") << QString("restored") << QString("目标已恢复原状");
        QTest::newRow("different") << QString("manual_inspection") << QString("需要人工检查");
    }
    void deliveryPanelVerifiesInterruptedSquash()
    {
        QFETCH(QString, outcome);
        QFETCH(QString, label);
        const bool manual = outcome == "manual_inspection";
        const QString branch = "release/fix", reason = "目标工作区有未提交的差异。";
        const QString parent(40, 'a'), published(40, 'b'), token(64, 'c');
        const auto target = outcome == "completed" ? published : parent;
        Fixture fixture;
        fixture.setPhase("succeeded", "");
        fixture.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            fixture.deliveries.append(r.body);
            QJsonObject evidence{{"outcome", outcome}, {"target_branch", branch}};
            if (r.body.contains("token")) {
                fixture.setPhase("succeeded", "", fixture.revision() + 1);
                evidence["mode"] = "squash";
                evidence["status"] = outcome == "completed" ? "delivered" : "active";
                evidence["target_commit"] = target;
                if (outcome == "completed") evidence["commit"] = target;
            } else {
                evidence["task_id"] = MissionId;
                evidence["token"] = token;
                evidence["task_commit"] = QString(40, 'd');
                evidence["parent_commit"] = parent;
                evidence["delivery_commit"] = published;
                evidence["current_target_commit"] = target;
                evidence["target_clean"] = !manual;
                evidence["file_hashes"] = QJsonObject{};
                if (manual) evidence["reason"] = reason;
            }
            Source::json(r, {{"mission", fixture.mission}, {"delivery", evidence}});
            return true;
        };
        Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.expandStage("delivery"));
        QVERIFY(ui.chooseNode("delivery_bundle"));
        auto *mode = ui.find("missionDeliveryMode"); QVERIFY(mode);
        mode->setProperty("currentIndex", 5);
        QCOMPARE(mode->property("currentText").toString(), QString("核验中断的合并"));
        QVERIFY(ui.click("missionDeliveryPreviewButton"));
        QTRY_COMPARE(fixture.deliveries.size(), 1);
        QCOMPARE(fixture.deliveries.first(), (QJsonObject{{"expected_revision", 8}, {"mode", "recover"}}));
        auto *preview = ui.find("missionDeliveryPreviewDetails"); QVERIFY(preview);
        QTRY_VERIFY(preview->isVisible());
        QTRY_VERIFY(ui.missions.delivery()->canPreview());
        const auto details = preview->property("text").toString();
        QVERIFY(details.contains(label) && details.contains(branch) && details.contains(target));
        if (manual) {
            QVERIFY(details.contains(reason));
            QVERIFY(!ui.missions.delivery()->canConfirm());
            auto *confirm = ui.find("missionDeliveryConfirmButton"); QVERIFY(confirm);
            QVERIFY(!confirm->isVisible());
            QVERIFY(ui.click("missionDeliveryPreviewButton"));
            QTRY_COMPARE(fixture.deliveries.size(), 2);
            QVERIFY(!fixture.deliveries.last().contains("token"));
        } else {
            QVERIFY(ui.click("missionDeliveryConfirmButton"));
            auto *accept = ui.find("missionDeliveryDialogConfirmButton"); QVERIFY(accept);
            QVERIFY(accept->property("text").toString().contains(label));
            QVERIFY(ui.click(accept));
            QTRY_COMPARE(fixture.deliveries.size(), 2);
            QCOMPARE(fixture.deliveries.last(), (QJsonObject{{"expected_revision", 8}, {"mode", "recover"}, {"token", token}}));
            auto *result = ui.find("missionDeliveryResultDetails"); QVERIFY(result);
            QTRY_VERIFY(result->isVisible());
            QVERIFY(result->property("text").toString().contains(label));
            QVERIFY(!ui.missions.delivery()->canConfirm());
        }
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
int main(int argc, char **argv)
{
    if (argc > 1 && QByteArray(argv[1]) == "--runtime-config-fixture") {
        QCoreApplication application(argc, argv); return runtimeConfigFixture();
    }
#ifdef Q_OS_WIN
    if (qEnvironmentVariableIsEmpty("QT_QPA_FONTDIR"))
        qputenv("QT_QPA_FONTDIR", QDir(qEnvironmentVariable("WINDIR", "C:/Windows")).filePath("Fonts").toUtf8());
#endif
    QGuiApplication application(argc, argv);
    WorkflowQmlTests tests; return QTest::qExec(&tests, argc, argv);
}
#include "tst_workflow_qml.moc"
