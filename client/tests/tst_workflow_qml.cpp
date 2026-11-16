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
#include <QTemporaryDir>
#include <QThread>
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
            } else if (request.path == "/api/v2/missions/preview" && request.method == "POST") {
                const auto paths = request.body.value("baseline_paths").toArray();
                Source::json(request, {{"preview_token", QString(64, 'c')}, {"base_commit", QString(40, 'a')},
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
    {
        tasks.setBaseUrl(fixture.source.url()); missions.setBaseUrl(fixture.source.url());
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
            // One coherent read bundle per update: start, four parts and fence.
            int detailReads = 0;
            for (int r = requestStart; r < fixture.source.requests.size(); ++r)
                if (fixture.source.requests[r].method == "GET"
                        && fixture.source.requests[r].path.startsWith("/api/v2/missions/")) ++detailReads;
            QCOMPARE(detailReads, 6);
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
        RuntimeConfigController runtime;
        runtime.setHelperCommand(python, {"-X", "utf8", "-I", root + "/scripts/desktop_launcher.py"}, configPath);
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
        // A Python-generated configuration is reloaded through the same helper,
        // without checking model endpoints or restarting any real service.
        QVERIFY(ui.click("runtimeConfigReload")); QTRY_VERIFY_WITH_TIMEOUT(!runtime.busy(), 15000);
        QVERIFY(runtime.configured()); QVERIFY(runtime.hasApiKey());
        QCOMPARE(runtime.fields().value("task_model").toString(), "offline-task");
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void missionMcpChoicesRequireExplicitReadOnlyAnnotation()
    {
        Fixture fixture; Ui ui(fixture); QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton")); QTRY_VERIFY(!ui.mcp.missionLoading());
        QCOMPARE(ui.mcp.missionChoices().size(), 1);
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
        auto *context = ui.find("missionToolContext"); QVERIFY(context); QVERIFY(context->isVisible());
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
    void legacyWorkModePreviewsBaselineBeforeCreatingMission()
    {
        Fixture fixture;
        fixture.intercept = [](const Request &request) {
            if(request.path == "/api/v1/conversations/" + A.toUtf8()) {
                Source::json(request, {{"conversation_id", A}, {"title", "旧工作对话"},
                    {"repository_path", "D:/fixture/repository"}, {"messages", QJsonArray{}}});
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
        QVERIFY(ui.click(ui.find("missionCreateButton")));
        QTRY_COMPARE(fixture.creates.size(), 1);
        QTRY_COMPARE(ui.missions.taskId(), MissionId);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.window->property("agentMode").toBool());
        QCOMPARE(ui.conversations.mode(), QString("chat"));
        QCOMPARE(fixture.creates.first().value("repository_path").toString(), QString("D:/fixture/repository"));
        QCOMPARE(fixture.creates.first().value("request").toString(), QString("旧入口的新代码任务"));
        for(const auto &request:fixture.source.requests)
            if(request.method == "POST") QVERIFY(request.path == "/api/v2/missions" || request.path == "/api/v2/missions/preview");
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void convertedChatDraftCreatesMissionWithoutHistory()
    {
        Fixture fixture; Ui ui(fixture);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        auto *prompt=ui.find("conversationPrompt"); QVERIFY(prompt);
        prompt->setProperty("text", "选定的任务文本");
        QTRY_VERIFY(ui.find("convertDraftToAgentTaskButton")->isVisible());
        QVERIFY(ui.click(ui.find("convertDraftToAgentTaskButton")));
        auto *input=ui.find("missionRequestInput"); QVERIFY(input);
        QTRY_COMPARE(input->property("text").toString(), QString("选定的任务文本"));
        QCOMPARE(fixture.creates.size(), 0);
        auto *repository=ui.find("missionRepositoryInput"); QVERIFY(repository);
        repository->setProperty("text", "D:/fixture/repository");
        QVERIFY(ui.click(ui.find("missionPreviewButton"))); QTRY_VERIFY(ui.missions.creationReady());
        QVERIFY(ui.click(ui.find("missionCreateButton")));
        QTRY_COMPARE(fixture.creates.size(), 1);
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
        QVERIFY(ui.click(ui.find("missionCreateButton"))); QTRY_COMPARE(fixture.creates.size(), 1);
        QCOMPARE(fixture.creates.first().value("baseline_paths").toArray(), QJsonArray{"main.cpp"});
        QCOMPARE(fixture.creates.first().value("baseline_confirmed").toBool(), true);
        QCOMPARE(fixture.creates.first().value("preview_token").toString(), QString(64, 'c'));
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
