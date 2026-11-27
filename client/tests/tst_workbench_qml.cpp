#include "ClientUi.h"
#include "ConversationController.h"
#include "McpController.h"
#include "TaskController.h"
#include "workbench/MissionController.h"
#include "HttpFixture.h"
#include <QFile>
#include <QGuiApplication>
#include <QClipboard>
#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QElapsedTimer>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickItem>
#include <QQuickWindow>
#include <QRectF>
#include <QScreen>
#include <QtTest>
#include <algorithm>
#include <functional>
#include <memory>

namespace {
const QString MissionA = "10000000-0000-4000-a000-000000000001";
const QString MissionB = "10000000-0000-4000-a000-000000000009";

QRectF sceneBounds(QQuickItem *item)
{
    return {item->mapToScene(QPointF(0, 0)), QSizeF(item->width(), item->height())};
}

bool insideWindow(QQuickItem *item, QQuickWindow *window)
{
    if (!item || !item->isVisible() || item->width() <= 0 || item->height() <= 0) return false;
    const auto rect = sceneBounds(item);
    return rect.left() >= -1 && rect.top() >= -1
        && rect.right() <= window->width() + 1 && rect.bottom() <= window->height() + 1;
}

QJsonObject fromFile(const QString &name)
{
    QFile file(QString(FORGEPILOT_SOURCE_DIR) + '/' + name);
    if (!file.open(QIODevice::ReadOnly)) qFatal("Cannot open workbench fixture");
    const auto parsed = QJsonDocument::fromJson(file.readAll());
    if (!parsed.isObject()) qFatal("Workbench fixture is not an object");
    return parsed.object();
}

QJsonObject mission(const QString &id, const QString &status, int revision = 8)
{
    auto body = fromFile("protocol/examples.json").value("mission_query_response").toObject();
    body["task_id"] = id;
    body["request"] = id == MissionA ? "Stop background worker" : "Repair interrupted task";
    body["status"] = status;
    body["current_revision"] = revision;
    body["last_event_id"] = id + ':' + QString::number(revision);
    body["recovery_required"] = status == "interrupted";
    if (status == "awaiting_human") {
        auto review = body.value("review").toObject();
        const auto items = fromFile("client/tests/fixtures/mission_artifacts.json").value("items").toArray();
        for (const auto &entry : items) {
            const auto artifact = entry.toObject();
            if (artifact.value("artifact_type") == "TaskSpec")
                review["task_spec_hash"] = artifact.value("content_sha256");
            if (artifact.value("artifact_type") == "ExecutionPlan")
                review["plan_hash"] = artifact.value("content_sha256");
        }
        review["task_id"] = id;
        review["revision"] = revision;
        review["task_spec_artifact_id"] = "10000000-0000-4000-a000-000000000002";
        review["plan_artifact_id"] = "10000000-0000-4000-a000-000000000003";
        body["review"] = review;
    } else body["review"] = QJsonValue::Null;
    return body;
}

QJsonObject graph(const QJsonObject &m)
{
    const auto status = m.value("status");
    const auto nodeId = m.value("test_graph_node_id").toString("execution_approval");
    const QJsonObject node{{"node_id", nodeId}, {"node_type", "human_gate"},
        {"status", status}, {"attempt", 1}, {"result", QJsonValue::Null}};
    const QJsonObject definition{{"node_id", nodeId}, {"node_type", "human_gate"},
        {"handler", nodeId}, {"dependencies", QJsonArray{}},
        {"agent_id", QJsonValue::Null}, {"agent_definition_hash", QJsonValue::Null}, {"settings", QJsonObject{}}};
    return {{"task_id", m.value("task_id")}, {"graph_id", m.value("graph_id")},
        {"graph_version", 1}, {"definition_hash", QString(64, 'a')}, {"status", status},
        {"current_revision", m.value("current_revision")}, {"nodes", QJsonArray{node}},
        {"definition", QJsonObject{{"graph_id", m.value("graph_id")}, {"version", 1},
            {"nodes", QJsonArray{definition}}, {"edges", QJsonArray{}}}}};
}

struct Fixture {
    Source source;
    QHash<QString, QJsonObject> snapshots{{MissionA, mission(MissionA, "awaiting_human")},
                                           {MissionB, mission(MissionB, "interrupted")}};
    QJsonObject artifacts = fromFile("client/tests/fixtures/mission_artifacts.json");
    QList<Request> streams;
    std::function<bool(const Request &)> intercept;
    bool includeResults = false;
    int approvalPosts = 0;
    Fixture()
    {
        source.handle = [this](const Request &r) {
            if (intercept && intercept(r)) return;
            if (r.path.startsWith("/api/v1/conversations?")) {
                Source::json(r, {{"items", QJsonArray{}}, {"total", 0}, {"next_cursor", QJsonValue::Null}});
                return;
            }
            if (r.method == "GET" && r.path.startsWith("/api/v2/missions?")) {
                Source::json(r, {{"items", QJsonArray{snapshots.value(MissionA), snapshots.value(MissionB)}},
                    {"total", 2}, {"next_cursor", QJsonValue::Null}});
                return;
            }
            const auto parts = r.path.split('/');
            if (parts.size() < 5 || parts[1] != "api" || parts[2] != "v2" || parts[3] != "missions") {
                Source::json(r, {{"items", QJsonArray{}}, {"total", 0}, {"next_cursor", QJsonValue::Null}});
                return;
            }
            const QString id = QString::fromUtf8(parts[4]);
            auto m = snapshots.value(id);
            if (m.isEmpty()) { Source::json(r, {{"error", QJsonObject{{"code", "unknown_mission"}, {"message", "Unknown"}, {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}}, 404); return; }
            if (r.path.endsWith("/events")) { streams.append(r); Source::stream(r, ": ready\n\n", false); return; }
            if (r.path.endsWith("/graph")) { Source::json(r, graph(m)); return; }
            if (r.path.endsWith("/artifacts")) {
                Source::json(r, id == MissionA ? artifacts : QJsonObject{{"task_id", id}, {"items", QJsonArray{}}}); return;
            }
            if (r.path.endsWith("/diff")) {
                QJsonArray changes;
                if (id == MissionA && includeResults)
                    for (const auto &v : artifacts.value("items").toArray())
                        if (v.toObject().value("artifact_type") == "ChangeSet") changes.append(v);
                Source::json(r, {{"task_id", id}, {"patch", ""}, {"change_sets", changes}}); return;
            }
            if (r.path.endsWith("/validation")) {
                QJsonArray reports;
                if (id == MissionA && includeResults)
                    for (const auto &v : artifacts.value("items").toArray())
                        if (v.toObject().value("artifact_type") == "ValidationReport") reports.append(v);
                Source::json(r, {{"task_id", id}, {"items", reports},
                    {"latest", reports.isEmpty() ? QJsonValue::Null : reports.last()}}); return;
            }
            if (r.path.endsWith("/approvals") && r.method == "POST") {
                ++approvalPosts;
                m = mission(id, r.body.value("decision") == "approved" ? "running" : "cancelled", 9);
                snapshots[id] = m; Source::json(r, m, 202); return;
            }
            if (r.method == "GET") { Source::json(r, m); return; }
            Source::json(r, m, 202);
        };
    }
};

struct Ui {
    BackendConnection connection;
    TaskController tasks{connection};
    ConversationController conversations{connection,&tasks};
    MissionController missions;
    McpController mcp{connection};
    QQmlApplicationEngine engine;
    QQuickWindow *window = nullptr;
    QStringList warnings;
    explicit Ui(Fixture &f, int initialWidth = 1200, int initialHeight = 760, bool withMcp = false)
    {
        tasks.setBaseUrl(f.source.url()); missions.setBaseUrl(f.source.url());
        QObject::connect(&engine, &QQmlEngine::warnings, &engine, [this](const QList<QQmlError> &errors) {
            for (const auto &error : errors) warnings.append(error.toString());
        });
        QVariantMap initial{{"backendConnection", QVariant::fromValue(&connection)}, {"controller", QVariant::fromValue(&tasks)},
            {"conversations", QVariant::fromValue(&conversations)},
            {"missions", QVariant::fromValue(&missions)}, {"width", initialWidth}, {"height", initialHeight}};
        if (withMcp) initial.insert("mcp", QVariant::fromValue(&mcp));
        engine.setInitialProperties(initial);
    }
    bool load()
    {
        const auto mainPath = QString(FORGEPILOT_SOURCE_DIR) + "/client/Main.qml";
        QFile mainFile(mainPath);
        if (!mainFile.open(QIODevice::ReadOnly)) return false;
        // Retain the legacy panel regressions; tst_workflow_qml exercises the
        // production Main.qml with the new graph-first workbench.
        engine.loadData(mainFile.readAll().replace("Workbench.WorkflowPage", "Workbench.WorkbenchPage"),
                        QUrl::fromLocalFile(mainPath));
        if (engine.rootObjects().isEmpty()) return false;
        window = qobject_cast<QQuickWindow *>(engine.rootObjects().first());
        return window && QTest::qWaitForWindowExposed(window);
    }
    QQuickItem *find(const QString &name) const
    {
        if (!window) return nullptr;
        std::function<QQuickItem *(QQuickItem *)> visit = [&](QQuickItem *item) -> QQuickItem * {
            if (item->objectName() == name) return item;
            for (auto *child : item->childItems()) if (auto *found = visit(child)) return found;
            return nullptr;
        };
        return visit(window->contentItem());
    }
    QQuickItem *taskRow(const QString &id) const
    {
        if (!window) return nullptr;
        std::function<QQuickItem *(QQuickItem *)> visit = [&](QQuickItem *item) -> QQuickItem * {
            if (item->objectName() == "missionTaskRow" && item->property("taskId").toString() == id) return item;
            for (auto *child : item->childItems()) if (auto *found = visit(child)) return found;
            return nullptr;
        };
        return visit(window->contentItem());
    }
    QQuickItem *textItem(QQuickItem *root, const QString &needle) const
    {
        if (!root) return nullptr;
        std::function<QQuickItem *(QQuickItem *)> visit = [&](QQuickItem *item) -> QQuickItem * {
            if (item->property("text").toString().contains(needle)) return item;
            for (auto *child : item->childItems()) if (auto *found = visit(child)) return found;
            return nullptr;
        };
        return visit(root);
    }
    void reveal(QQuickItem *item) const
    {
        for(auto *parent=item ? item->parentItem() : nullptr; parent; parent=parent->parentItem()) {
            if(!parent->property("contentY").isValid() || !parent->property("contentHeight").isValid()) continue;
            const auto top=item->mapToItem(parent,QPointF(0,0)).y();
            const auto bottom=top+item->height();
            auto offset=parent->property("contentY").toReal();
            if(top<0) offset+=top;
            else if(bottom>parent->height()) offset+=bottom-parent->height();
            const auto maximum=qMax<qreal>(0,parent->property("contentHeight").toReal()-parent->height());
            parent->setProperty("contentY",qBound<qreal>(0,offset,maximum));
            QCoreApplication::processEvents();
        }
    }
    bool click(const QString &name) const
    {
        auto *item = find(name);
        if (!item || !item->isVisible() || !item->isEnabled()) return false;
        window->update(); QTest::qWait(30);
        QTest::mouseClick(window, Qt::LeftButton, Qt::NoModifier,
            item->mapToScene(QPointF(item->width()/2, item->height()/2)).toPoint());
        return true;
    }
};
}

class WorkbenchQmlTests : public QObject {
    Q_OBJECT
private slots:
    void initTestCase()
    {
        configureClientUi(*qobject_cast<QGuiApplication *>(QCoreApplication::instance()));
    }
    void chatDraftsStayWithTheirBackend()
    {
        Fixture first, second;
        Ui ui(first);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        auto *prompt=ui.find("conversationPrompt"); QVERIFY(prompt);
        ui.conversations.selectConversation(A);
        prompt->setProperty("text","draft on first");
        ui.connection.setBaseUrl(second.source.url());
        ui.conversations.selectConversation(A);
        QCOMPARE(prompt->property("text").toString(),QString());
        prompt->setProperty("text","draft on second");
        ui.connection.setBaseUrl(first.source.url());
        ui.conversations.selectConversation(A);
        QCOMPARE(prompt->property("text").toString(),QString("draft on first"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void responsiveLayout_data()
    {
        QTest::addColumn<int>("windowWidth");
        QTest::addColumn<int>("windowHeight");
        QTest::newRow("minimum-860x640") << 860 << 640;
        QTest::newRow("desktop-1280x720") << 1280 << 720;
        QTest::newRow("desktop-1600x900") << 1600 << 900;
    }
    void responsiveLayout()
    {
        QFETCH(int, windowWidth);
        QFETCH(int, windowHeight);
        Fixture f; Ui ui(f, windowWidth, windowHeight);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QTRY_COMPARE(ui.window->width(), windowWidth);
        QTRY_COMPARE(ui.window->height(), windowHeight);
        QCOMPARE(ui.window->minimumWidth(), 860);
        QCOMPARE(ui.window->minimumHeight(), 640);
        qInfo().noquote() << "RESPONSIVE" << windowWidth << 'x' << windowHeight
            << "devicePixelRatio=" << ui.window->screen()->devicePixelRatio();
        const auto captureDir = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (!captureDir.isEmpty()) QVERIFY(QDir().mkpath(captureDir));
        auto capture = [&](const QString &page) {
            if (captureDir.isEmpty()) return true;
            QTest::qWait(40);
            return ui.window->grabWindow().save(captureDir + '/' + page + '-'
                + QString::number(windowWidth) + 'x' + QString::number(windowHeight) + ".png");
        };

        auto requireInside = [&](const QString &name) {
            auto *item = ui.find(name);
            if (!insideWindow(item, ui.window)) {
                qWarning().noquote() << name << "outside" << windowWidth << 'x' << windowHeight
                    << (item ? sceneBounds(item) : QRectF());
                return false;
            }
            return true;
        };
        QTRY_VERIFY(requireInside("freeChatModeButton"));
        QTRY_VERIFY(requireInside("agentWorkbenchModeButton"));
        QTRY_VERIFY(requireInside("sidebarToggle"));
        QTRY_VERIFY(requireInside("conversationPrompt"));
        QTRY_VERIFY(requireInside("sendMessageButton"));
        auto noOverlap = [&](const QString &first, const QString &second) {
            auto *a = ui.find(first);
            auto *b = ui.find(second);
            return a && b && (sceneBounds(a).intersected(sceneBounds(b)).width() <= 1
                || sceneBounds(a).intersected(sceneBounds(b)).height() <= 1);
        };
        QVERIFY(noOverlap("freeChatModeButton", "agentWorkbenchModeButton"));
        QVERIFY(noOverlap("conversationPrompt", "sendMessageButton"));
        QVERIFY(capture("chat"));
        auto *sidebar = ui.find("conversationSidebar"); QVERIFY(sidebar);
        if (windowWidth < 1180) {
            QTRY_VERIFY(!sidebar->isVisible());
            QTRY_VERIFY(requireInside("compactHistoryButton"));
            QTRY_VERIFY(requireInside("compactSettingsButton"));
            QTRY_VERIFY(requireInside("compactMemoryButton"));
            QVERIFY(noOverlap("compactHistoryButton", "compactSettingsButton"));
            QVERIFY(noOverlap("compactSettingsButton", "compactMemoryButton"));
            QVERIFY(ui.click("compactHistoryButton"));
            QTRY_VERIFY(requireInside("compactNewTaskButton"));
            QVERIFY(capture("history"));
            QTest::keyClick(ui.window, Qt::Key_Escape);
            QTRY_VERIFY([&] { auto *item = ui.find("compactNewTaskButton"); return !item || !item->isVisible(); }());
        } else {
            QTRY_VERIFY(sidebar->isVisible());
            QTRY_VERIFY(requireInside("newTaskButton"));
            QTRY_VERIFY(requireInside("settingsButton"));
        }

        QVERIFY(ui.click(windowWidth < 1180 ? "compactMemoryButton" : "memoryManagerButton"));
        QTRY_VERIFY(requireInside("memoryList"));
        QTRY_VERIFY(requireInside("summaryMemoryTab"));
        QTRY_VERIFY(requireInside("closeMemoryButton"));
        auto *addMemory = ui.find("addMemoryButton"); QVERIFY(addMemory);
        QTRY_VERIFY(addMemory->isEnabled());
        QVERIFY(ui.click("addMemoryButton"));
        QTRY_VERIFY(requireInside("memoryContentInput"));
        QTRY_VERIFY(requireInside("saveMemoryButton"));
        QVERIFY(capture("memory"));
        QTest::keyClick(ui.window, Qt::Key_Escape);
        QTRY_VERIFY([&] { auto *item = ui.find("memoryContentInput"); return !item || !item->isVisible(); }());
        QVERIFY(ui.click("closeMemoryButton"));
        QTRY_VERIFY([&] { auto *item = ui.find("memoryList"); return !item || !item->isVisible(); }());

        QVERIFY(ui.click(windowWidth < 1180 ? "compactSettingsButton" : "settingsButton"));
        QTRY_VERIFY(requireInside("settingsPanel"));
        QTRY_VERIFY(requireInside("closeSettingsButton"));
        QVERIFY(capture("settings"));
        QVERIFY(ui.click("closeSettingsButton"));
        QTRY_VERIFY([&] { auto *item = ui.find("settingsPanel"); return !item || !item->isVisible(); }());

        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY(requireInside("workbenchCenterScroll"));
        auto *taskCenter = ui.find("missionTaskCenter"); QVERIFY(taskCenter);
        if (windowWidth < 1080) {
            QTRY_VERIFY(!taskCenter->isVisible());
            QTRY_VERIFY(requireInside("workbenchCompactBar"));
            QTRY_VERIFY(requireInside("workbenchTaskCenterToggle"));
            QVERIFY(ui.click("workbenchTaskCenterToggle"));
            QTRY_VERIFY(requireInside("missionTaskCenter"));
            QTRY_VERIFY(requireInside("missionRequestInput"));
            QVERIFY(noOverlap("missionTaskCenter", "workbenchCenterScroll"));
        } else {
            QTRY_VERIFY(requireInside("missionTaskCenter"));
            QVERIFY(noOverlap("missionTaskCenter", "workbenchCenterScroll"));
        }
        QVERIFY(capture("workbench"));

        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        if (!details->isVisible()) QVERIFY(ui.click("workbenchDetailsToggle"));
        QTRY_VERIFY(requireInside("workbenchDetails"));
        QTRY_VERIFY(requireInside("workbenchTabStripScroll"));
        QTRY_VERIFY(requireInside("workbenchDetailScroll"));
        auto *tabStrip = ui.find("workbenchTabStripScroll"); QVERIFY(tabStrip);
        if (tabStrip->property("contentWidth").toReal() > tabStrip->width() + 1) {
            auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
            tabs->setProperty("currentIndex", 7);
            QTRY_VERIFY(tabStrip->property("contentX").toReal() > 0);
        }
        QVERIFY(noOverlap("missionTaskCenter", "workbenchDetails"));
        QVERIFY(capture("details"));
        if (windowWidth < 1300) {
            QTRY_VERIFY(requireInside("workbenchReturnButton"));
            QVERIFY(ui.click("workbenchReturnButton"));
            QTRY_VERIFY(requireInside("workbenchCenterScroll"));
        }
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void populatedDetailPages_data()
    {
        QTest::addColumn<int>("windowWidth");
        QTest::addColumn<int>("windowHeight");
        QTest::newRow("minimum-860x640") << 860 << 640;
        QTest::newRow("desktop-1280x720") << 1280 << 720;
        QTest::newRow("desktop-1600x900") << 1600 << 900;
    }
    void populatedDetailPages()
    {
        QFETCH(int, windowWidth);
        QFETCH(int, windowHeight);
        Fixture f; Ui ui(f, windowWidth, windowHeight);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        auto *center = ui.find("workbenchCenterScroll"); QVERIFY(center);
        if (windowWidth < 1300 && details->isVisible()) {
            QVERIFY(ui.click("workbenchReturnButton"));
            QTRY_VERIFY(!details->isVisible());
        }
        QTRY_VERIFY(insideWindow(center, ui.window));
        auto *graph = ui.find("taskGraphView"); QVERIFY(graph);
        QVERIFY(graph->isVisible());
        const auto captureDir = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (!captureDir.isEmpty()) {
            QVERIFY(QDir().mkpath(captureDir));
            QVERIFY(ui.window->grabWindow().save(captureDir + "/task-graph-"
                + QString::number(windowWidth) + 'x' + QString::number(windowHeight) + ".png"));
        }
        if (!details->isVisible()) QVERIFY(ui.click("workbenchDetailsToggle"));
        QTRY_VERIFY(insideWindow(details, ui.window));
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        auto *tabScroll = ui.find("workbenchTabStripScroll"); QVERIFY(tabScroll);
        auto *detailScroll = ui.find("workbenchDetailScroll"); QVERIFY(detailScroll);
        const QStringList pages = {"task", "evidence", "plan", "approval", "diff",
                                   "validation", "review", "delivery"};
        const QStringList panels = {"taskSpecPanel", "evidencePanel", "executionPlanPanel",
                                    "approvalPanel", "missionDiffPanel", "validationPanel",
                                    "reviewPanel", "deliveryPanel"};
        auto fullyInDetailViewport = [&](QQuickItem *item) {
            const auto rect = sceneBounds(item);
            const auto viewport = sceneBounds(detailScroll);
            return item->isVisible() && rect.left() >= viewport.left() - 1
                && rect.right() <= viewport.right() + 1
                && rect.top() >= viewport.top() - 1
                && rect.bottom() <= viewport.bottom() + 1;
        };
        for (int index = 0; index < pages.size(); ++index) {
            tabs->setProperty("currentIndex", index);
            QTRY_COMPARE(tabs->property("currentIndex").toInt(), index);
            QQuickItem *panel = nullptr;
            QTRY_VERIFY((panel = ui.find(panels.at(index))) && panel->isVisible());
            QTRY_VERIFY(insideWindow(tabScroll, ui.window));
            QTRY_VERIFY(insideWindow(detailScroll, ui.window));
            if (!captureDir.isEmpty()) {
                QTest::qWait(30);
                QVERIFY(ui.window->grabWindow().save(captureDir + '/' + pages.at(index) + '-'
                    + QString::number(windowWidth) + 'x' + QString::number(windowHeight) + ".png"));
            }
            if (index == 3 || index == 7) {
                auto *flickable = qobject_cast<QQuickItem *>(
                    detailScroll->property("contentItem").value<QObject *>());
                QVERIFY(flickable);
                const auto maxY = std::max(0.0,
                    flickable->property("contentHeight").toReal() - flickable->height());
                QVERIFY(flickable->setProperty("contentY", maxY));
                if (index == 3) {
                    auto *approve = ui.find("missionApproveButton"); QVERIFY(approve);
                    auto *reject = ui.find("missionRejectButton"); QVERIFY(reject);
                    QTRY_VERIFY(fullyInDetailViewport(approve));
                    QTRY_VERIFY(fullyInDetailViewport(reject));
                    QVERIFY(approve->isEnabled() && reject->isEnabled());
                } else {
                    auto *preview = ui.find("missionDeliveryPreviewButton"); QVERIFY(preview);
                    QTRY_VERIFY(fullyInDetailViewport(preview));
                }
            }
        }
        tabs->setProperty("currentIndex", 3);
        auto *approve = ui.find("missionApproveButton"); QVERIFY(approve);
        auto *flickable = qobject_cast<QQuickItem *>(
            detailScroll->property("contentItem").value<QObject *>());
        QVERIFY(flickable);
        QVERIFY(flickable->setProperty("contentY", std::max(0.0,
            flickable->property("contentHeight").toReal() - flickable->height())));
        QTRY_VERIFY(fullyInDetailViewport(approve));
        QVERIFY(ui.click("missionApproveButton"));
        QTRY_COMPARE(f.approvalPosts, 1);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void longWorkbenchProseWraps_data()
    {
        QTest::addColumn<int>("tab");
        QTest::addColumn<QString>("panel");
        QTest::addColumn<QString>("artifactType");
        QTest::newRow("task") << 0 << QString("taskSpecPanel") << QString("TaskSpec");
        QTest::newRow("evidence") << 1 << QString("evidencePanel") << QString("EvidenceBundle");
        QTest::newRow("plan") << 2 << QString("executionPlanPanel") << QString("ExecutionPlan");
        QTest::newRow("review") << 6 << QString("reviewPanel") << QString("ReviewReport");
    }
    void longWorkbenchProseWraps()
    {
        QFETCH(int, tab);
        QFETCH(QString, panel);
        QFETCH(QString, artifactType);
        Fixture f;
        const auto token = QString("GeneratedSymbol") + QString(160, 'W') + ".cpp";
        auto artifacts = f.artifacts.value("items").toArray();
        for (int i = 0; i < artifacts.size(); ++i) {
            auto artifact = artifacts.at(i).toObject();
            if (artifact.value("artifact_type") != artifactType) continue;
            auto content = artifact.value("content").toObject();
            if (artifactType == "EvidenceBundle") {
                auto items = content.value("items").toArray();
                auto evidence = items.first().toObject();
                evidence["claim"] = token;
                items[0] = evidence;
                content["items"] = items;
            } else {
                content[artifactType == "TaskSpec" ? "objective" : "summary"] = token;
            }
            artifact["content"] = content;
            artifacts[i] = artifact;
        }
        f.artifacts["items"] = artifacts;
        Ui ui(f, 860, 640);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        if (!details->isVisible()) QVERIFY(ui.click("workbenchDetailsToggle"));
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", tab);
        QQuickItem *label = nullptr;
        QTRY_VERIFY((label = ui.textItem(ui.find(panel), token)) && label->isVisible());
        QTRY_VERIFY(label->width() > 0 && label->property("contentWidth").toReal() > 0);
        QTRY_VERIFY(label->property("lineCount").toInt() > 1);
        const auto captureDir = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (!captureDir.isEmpty()) {
            QVERIFY(QDir().mkpath(captureDir));
            QVERIFY(ui.window->grabWindow().save(captureDir + "/long-"
                + QTest::currentDataTag() + "-860x640.png"));
        }
        QVERIFY2(label->property("contentWidth").toReal() <= label->width() + 1,
                 "The entire unbroken identifier must fit within the text viewport");
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void narrowingWindowReflowsChatAndWorkbench()
    {
        Fixture f; Ui ui(f, 1600, 900);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        ui.window->resize(860, 640);
        QTRY_COMPARE(ui.window->width(), 860);
        QTRY_COMPARE(ui.window->height(), 640);
        auto within = [&](const QString &name) { return insideWindow(ui.find(name), ui.window); };
        auto *sidebar = ui.find("conversationSidebar"); QVERIFY(sidebar);
        QTRY_VERIFY(!sidebar->isVisible());
        QTRY_VERIFY(within("compactHistoryButton"));
        QTRY_VERIFY(within("conversationPrompt"));
        QTRY_VERIFY(within("sendMessageButton"));
        ui.window->resize(1600, 900);
        QTRY_COMPARE(ui.window->width(), 1600);
        QTRY_COMPARE(ui.window->height(), 900);
        QTRY_VERIFY(sidebar->isVisible());
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        if (details->isVisible()) {
            QVERIFY(ui.click("workbenchDetailsToggle"));
            QTRY_VERIFY(!details->isVisible());
        }
        QVERIFY(ui.click("workbenchDetailsToggle"));
        QTRY_VERIFY(details->isVisible());
        ui.window->resize(860, 640);
        QTRY_COMPARE(ui.window->width(), 860);
        QTRY_COMPARE(ui.window->height(), 640);
        QTest::qWait(100);
        qInfo() << "NARROW DETAILS" << details->isVisible() << sceneBounds(details)
            << "workbench" << sceneBounds(ui.find("agentWorkbenchPage"));
        QTRY_VERIFY(within("workbenchDetails"));
        QTRY_VERIFY(within("workbenchReturnButton"));
        QVERIFY(ui.click("workbenchReturnButton"));
        QTRY_VERIFY(within("workbenchCenterScroll"));
        QTRY_VERIFY(within("workbenchTaskCenterToggle"));
        QVERIFY(ui.click("workbenchTaskCenterToggle"));
        QTRY_VERIFY(within("missionTaskCenter"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void mcpEditorFooterFitsMinimumWindow()
    {
        Fixture f;
        f.intercept = [](const Request &request) {
            if (request.path == "/api/v1/mcp/servers") {
                Source::json(request, {{"items", QJsonArray{}}, {"disabled", false}});
                return true;
            }
            if (request.path == "/api/v1/mcp/presets") {
                Source::json(request, {{"items", QJsonArray{}}});
                return true;
            }
            return false;
        };
        Ui ui(f, 860, 640, true);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("compactSettingsButton"));
        QVERIFY(ui.click("mcpSettingsTab"));
        auto *add = ui.find("addMcpButton"); QVERIFY(add);
        QTRY_VERIFY(add->isEnabled());
        QVERIFY(ui.click("addMcpButton"));
        auto within = [&](const QString &name) { return insideWindow(ui.find(name), ui.window); };
        QTRY_VERIFY(within("mcpEditorScroll"));
        QTRY_VERIFY(within("closeMcpEditor"));
        QTRY_VERIFY(within("probeMcpButton"));
        QTRY_VERIFY(within("saveMcpButton"));
        QTRY_VERIFY(within("mcpName"));
        const auto captureDir = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (!captureDir.isEmpty()) {
            QVERIFY(QDir().mkpath(captureDir));
            QVERIFY(ui.window->grabWindow().save(captureDir + "/mcp-editor-860x640.png"));
        }
        QVERIFY(ui.click("closeMcpEditor"));
        QTRY_VERIFY([&] { auto *item = ui.find("mcpEditorScroll"); return !item || !item->isVisible(); }());
        QVERIFY(ui.click("closeSettingsButton"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void switchesTopLevelModesWithoutChangingConversationMode()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        const auto captureDir = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (!captureDir.isEmpty()) {
            QVERIFY(QDir().mkpath(captureDir));
            QVERIFY(ui.window->grabWindow().save(captureDir + "/free-chat.png"));
        }
        auto *prompt = ui.find("conversationPrompt"); QVERIFY(prompt);
        prompt->setProperty("text", "Draft stays in free chat");
        QVERIFY(ui.click("settingsButton"));
        QTest::qWait(100);
        if (!captureDir.isEmpty()) QVERIFY(ui.window->grabWindow().save(captureDir + "/settings.png"));
        QTest::keyClick(ui.window, Qt::Key_Escape);
        auto *workbench = ui.find("agentWorkbenchPage"); QVERIFY(workbench);
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY(workbench->isVisible());
        auto *detailTabs = ui.find("workbenchDetailTabs"); QVERIFY(detailTabs);
        QVERIFY2(detailTabs->width() >= 8 * 60, "Workbench detail tabs must retain readable widths");
        if (!captureDir.isEmpty()) QVERIFY(ui.window->grabWindow().save(captureDir + "/agent-workbench.png"));
        QCOMPARE(ui.conversations.mode(), QString("chat"));
        QCOMPARE(prompt->property("text").toString(), QString("Draft stays in free chat"));
        QVERIFY(ui.click("freeChatModeButton"));
        QTRY_VERIFY(!workbench->isVisible());
        QVERIFY(ui.find("sendMessageButton"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void longChatTitleCanBeReadAndCopied()
    {
        Fixture f;
        const QString longTitle = QStringLiteral("对话标题与完整路径 D:/Development/ForgePilot/深层目录/")
            + QStringLiteral("包含中文与无空格的长文件名_").repeated(14);
        f.intercept = [longTitle](const Request &r) {
            if (r.path.startsWith("/api/v1/conversations?")) {
                Source::json(r, {{"items", QJsonArray{QJsonObject{{"conversation_id", MissionA}, {"title", longTitle}}}},
                    {"total", 1}, {"next_cursor", QJsonValue::Null}});
                return true;
            }
            if (r.path == "/api/v1/conversations/" + MissionA) {
                Source::json(r, {{"conversation_id", MissionA}, {"title", longTitle},
                    {"repository_path", ""}, {"messages", QJsonArray{}},
                    {"active_request", QJsonValue::Null}, {"last_request", QJsonValue::Null}});
                return true;
            }
            return false;
        };
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QTRY_COMPARE(ui.conversations.title(), longTitle);
        auto *details = ui.find("conversationTitleDetailsButton"); QVERIFY(details);
        QTRY_VERIFY(details->isVisible());
        QVERIFY(ui.click("conversationTitleDetailsButton"));
        auto *fullText = ui.find("conversationTitleFullText"); QVERIFY(fullText);
        QTRY_COMPARE(fullText->property("text").toString(), longTitle);
        QVERIFY(ui.click("conversationTitleCopyButton"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longTitle);
        QTest::keyClick(ui.window, Qt::Key_Escape);
        QVERIFY(ui.click("conversationMore_" + MissionA));
        auto *historyTitleAction = ui.find("viewConversationTitle_" + MissionA); QVERIFY(historyTitleAction);
        QVERIFY(QMetaObject::invokeMethod(historyTitleAction, "triggered"));
        auto *historyFullText = ui.find("conversationTitleDetail"); QVERIFY(historyFullText);
        QTRY_COMPARE(historyFullText->property("text").toString(), longTitle);
        auto *historyCopy = ui.find("conversationTitleDetailCopy"); QVERIFY(historyCopy);
        QVERIFY(QMetaObject::invokeMethod(historyCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longTitle);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void longSettingsErrorCanBeReadAndCopied()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("settingsButton"));
        const QString longError = QStringLiteral("索引失败：D:/Development/ForgePilot/")
            + QStringLiteral("nested_path_with_no_breaks/").repeated(20)
            + QStringLiteral("，请核对完整错误。");
        const QVariantMap rag{{"status", "failed"}, {"statusErrorCode", "rag_index_failed"},
            {"reachable", true}, {"error", longError}, {"available", false},
            {"enabled", false}, {"running", false}, {"busy", false},
            {"fileCount", 0}, {"progress", 0.0}, {"totalFiles", 0},
            {"useForTasks", false}};
        ui.window->setProperty("rag", rag);
        auto *error = ui.find("codeIndexErrorDetail"); QVERIFY(error);
        QTRY_COMPARE(error->property("text").toString(), longError);
        auto *copy = ui.find("codeIndexErrorDetailCopy"); QVERIFY(copy);
        QVERIFY(QMetaObject::invokeMethod(copy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longError);
        auto *status = ui.find("codeIndexStatusFull"); QVERIFY(status);
        QVERIFY(!status->property("text").toString().isEmpty());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void longWorkbenchHeaderCanExpandScrollAndCopy()
    {
        Fixture f;
        const QString longRequest = QStringLiteral("逐项检查构建、验证与交付路径，保留全部上下文。")
            + QStringLiteral("详细任务说明与连续中文内容").repeated(80);
        const QString longPath = QStringLiteral("D:/Development/ForgePilot/")
            + QStringLiteral("very_long_nested_directory/").repeated(16) + "source.cpp";
        auto snapshot = f.snapshots.value(MissionA);
        snapshot["request"] = longRequest;
        snapshot["repository_path"] = longPath;
        f.snapshots[MissionA] = snapshot;
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workbenchReturnButton"));
        auto *titleViewer = ui.find("missionHeaderTitleFullText"); QVERIFY(titleViewer);
        QTRY_VERIFY(titleViewer->isVisible());
        auto *titleToggle = ui.find("missionHeaderTitleFullTextToggle"); QVERIFY(titleToggle);
        QVERIFY(QMetaObject::invokeMethod(titleToggle, "clicked"));
        QTRY_VERIFY(titleViewer->property("expanded").toBool());
        auto *titleLoader = ui.find("missionHeaderTitleFullTextLoader"); QVERIFY(titleLoader);
        QTRY_VERIFY(titleLoader->property("item").value<QObject *>());
        auto *loaded = qobject_cast<QQuickItem *>(titleLoader->property("item").value<QObject *>());
        QVERIFY(loaded);
        auto *fullText = ui.textItem(loaded, longRequest); QVERIFY(fullText);
        QCOMPARE(fullText->property("text").toString(), longRequest);
        QTRY_VERIFY(loaded->property("contentHeight").toReal() > loaded->height());
        auto *titleCopy = ui.find("missionHeaderTitleFullTextCopy"); QVERIFY(titleCopy);
        QVERIFY(QMetaObject::invokeMethod(titleCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longRequest);
        auto *pathViewer = ui.find("missionRepositoryPathFullText"); QVERIFY(pathViewer);
        QVERIFY(pathViewer->isVisible());
        auto *pathCopy = ui.find("missionRepositoryPathFullTextCopy"); QVERIFY(pathCopy);
        QVERIFY(QMetaObject::invokeMethod(pathCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longPath);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void longTaskCenterInputsRemainReadableAndCopyable()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        ui.window->setWidth(900);
        ui.window->setHeight(680);
        QTRY_COMPARE(ui.window->width(), 900);
        const QString longContext = QStringLiteral("来自自由聊天的所选上下文：")
            + QStringLiteral("逐行说明需求和边界；").repeated(80);
        const QString longRequest = QStringLiteral("请检查这条任务：")
            + QStringLiteral("包含连续中文和长路径的完整正文").repeated(100);
        const QString longPath = QStringLiteral("D:/Development/ForgePilot/")
            + QStringLiteral("nested_repository_directory/").repeated(15);
        QVERIFY(QMetaObject::invokeMethod(ui.window, "openTaskDraft", Q_ARG(QVariant, longRequest),
            Q_ARG(QVariant, QString()), Q_ARG(QVariant, longContext)));
        auto *taskCenterToggle = ui.find("workbenchTaskCenterToggle");
        QVERIFY(taskCenterToggle);
        auto *taskCenter = ui.find("missionTaskCenter");
        QVERIFY(taskCenter);
        if (!taskCenter->isVisible()) QVERIFY(ui.click("workbenchTaskCenterToggle"));
        QTRY_VERIFY(taskCenter->isVisible());
        auto *request = ui.find("missionRequestInput"); QVERIFY(request);
        QTRY_COMPARE(request->property("text").toString(), longRequest);
        auto *requestScroll = ui.find("missionRequestScroll"); QVERIFY(requestScroll);
        QTRY_VERIFY(requestScroll->property("contentHeight").toReal() > requestScroll->height());
        auto *context = ui.find("missionContextFull"); QVERIFY(context);
        QCOMPARE(context->property("text").toString(), longContext);
        auto *contextCopy = ui.find("missionContextFullCopy"); QVERIFY(contextCopy);
        QVERIFY(QMetaObject::invokeMethod(contextCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longContext);
        auto *repository = ui.find("missionRepositoryInput"); QVERIFY(repository);
        repository->setProperty("text", longPath);
        auto *path = ui.find("missionRepositoryInputFull"); QVERIFY(path);
        QTRY_COMPARE(path->property("text").toString(), longPath);
        auto *pathCopy = ui.find("missionRepositoryInputFullCopy"); QVERIFY(pathCopy);
        QVERIFY(QMetaObject::invokeMethod(pathCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longPath);
        auto *missionHeader = ui.find("missionHeader"); QVERIFY(missionHeader);
        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        QVERIFY(ui.click("workbenchDetailsToggle"));
        QTRY_VERIFY(details->isVisible());
        QTRY_VERIFY(details->mapToScene(QPointF(0, 0)).x()
            >= taskCenter->mapToScene(QPointF(taskCenter->width(), 0)).x() - 1);
        const auto taskRight = taskCenter->mapToScene(QPointF(taskCenter->width(), 0)).x();
        const auto detailsLeft = details->mapToScene(QPointF(0, 0)).x();
        QVERIFY(!missionHeader->isVisible());
        QVERIFY2(taskRight <= detailsLeft + 1, "Task center must not overlap compact details");
        QVERIFY2(detailsLeft + details->width() <= ui.window->width() + 1, "Details must stay inside the narrow window");
        QVERIFY(ui.click("workbenchReturnButton"));
        QTRY_VERIFY(missionHeader->isVisible());
        const auto centerRight = missionHeader->mapToScene(QPointF(missionHeader->width(), 0)).x();
        QVERIFY2(centerRight <= ui.window->width() + 1, "Wrapped mission content must stay inside the narrow window");
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void longGraphNodeAndDeliveryPathHaveFullTextActions()
    {
        Fixture f;
        const QString longNode = QStringLiteral("node_") + QStringLiteral("very_long_agent_step_").repeated(12);
        const QString longPath = QStringLiteral("D:/Development/ForgePilot/")
            + QStringLiteral("nested_delivery_output/").repeated(14) + "result.patch";
        auto snapshot = f.snapshots.value(MissionA);
        snapshot["test_graph_node_id"] = longNode;
        f.snapshots[MissionA] = snapshot;
        auto items = f.artifacts.value("items").toArray();
        for (int i = 0; i < items.size(); ++i) {
            auto artifact = items[i].toObject();
            if (artifact.value("artifact_type") != "DeliveryBundle") continue;
            auto content = artifact.value("content").toObject();
            content["changed_files"] = QJsonArray{longPath};
            artifact["content"] = content;
            items[i] = artifact;
        }
        f.artifacts["items"] = items;
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.click("workbenchReturnButton"));
        auto *nodeFull = ui.find("missionGraphNodeFull"); QVERIFY(nodeFull);
        QCOMPARE(nodeFull->property("text").toString(), longNode);
        auto *nodeCopy = ui.find("missionGraphNodeFullCopy"); QVERIFY(nodeCopy);
        QVERIFY(QMetaObject::invokeMethod(nodeCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longNode);
        QVERIFY(ui.click("workbenchDetailsToggle"));
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", 7);
        auto *delivery = ui.find("missionDeliveryBundleDetails"); QVERIFY(delivery);
        QTRY_VERIFY(delivery->property("text").toString().contains(longPath));
        auto *deliveryCopy = ui.find("missionDeliveryBundleDetailsCopy"); QVERIFY(deliveryCopy);
        QVERIFY(QMetaObject::invokeMethod(deliveryCopy, "clicked"));
        QTRY_VERIFY(QGuiApplication::clipboard()->text().contains(longPath));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void modeSwitchLatencyBaseline()
    {
        Fixture f; Ui ui(f);
        auto startupClock = std::make_shared<QElapsedTimer>();
        auto firstFrameMs = std::make_shared<qint64>(-1);
        QObject::connect(&ui.engine, &QQmlApplicationEngine::objectCreated, &ui.engine,
            [startupClock, firstFrameMs](QObject *root, const QUrl &) {
                auto *window = qobject_cast<QQuickWindow *>(root);
                if (window) QObject::connect(window, &QQuickWindow::frameSwapped, window,
                    [startupClock, firstFrameMs] { if (*firstFrameMs < 0) *firstFrameMs = startupClock->elapsed(); });
            });
        startupClock->start();
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        if (*firstFrameMs < 0) QTest::qWait(100);
        if (*firstFrameMs >= 0)
            qInfo().noquote() << "BENCH Qt QML fixture load to first frame (offscreen) ms=" << *firstFrameMs;
        else
            qInfo().noquote() << "BENCH Qt QML fixture first frame unavailable on offscreen platform";
        auto *workbench = ui.find("agentWorkbenchPage");
        auto *workbenchButton = ui.find("agentWorkbenchModeButton");
        auto *chatButton = ui.find("freeChatModeButton");
        QVERIFY(workbench); QVERIFY(workbenchButton); QVERIFY(chatButton);
        QList<qint64> microseconds;
        for (int i = 0; i < 7; ++i) {
            QElapsedTimer clock;
            clock.start();
            QVERIFY(QMetaObject::invokeMethod(workbenchButton, "clicked"));
            QCoreApplication::processEvents();
            QTRY_VERIFY(workbench->isVisible());
            microseconds.append(clock.nsecsElapsed() / 1000);
            QVERIFY(QMetaObject::invokeMethod(chatButton, "clicked"));
            QCoreApplication::processEvents();
            QTRY_VERIFY(!workbench->isVisible());
        }
        std::sort(microseconds.begin(), microseconds.end());
        qInfo().noquote() << "BENCH UI mode switch state latency (offscreen, warm) p50_ms="
            << microseconds[3] / 1000.0 << "p95_ms=" << microseconds[6] / 1000.0;
    }
    void showsFrozenApprovalAndSubmitsOnce()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_VERIFY(ui.missions.approval()->available());
        auto *details = ui.find("missionApprovalDetails"); QVERIFY(details);
        const auto summary = details->property("text").toString();
        QVERIFY2(summary.contains("Stop background worker"), qPrintable(summary));
        QVERIFY2(summary.contains("src/**"), qPrintable(summary));
        QVERIFY2(summary.contains(".git/**"), qPrintable(summary));
        QVERIFY2(summary.contains("8") && summary.contains(ui.missions.approval()->review().value("task_spec_hash").toString()),
                 "Approval must show revision and frozen hashes");
        auto *approvalFull = ui.find("missionApprovalFullDetails"); QVERIFY(approvalFull);
        QCOMPARE(approvalFull->property("text").toString(), summary);
        auto *approvalCopy = ui.find("missionApprovalFullDetailsCopy"); QVERIFY(approvalCopy);
        QVERIFY(QMetaObject::invokeMethod(approvalCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), summary);
        auto *approvalToggle = ui.find("missionApprovalFullDetailsToggle"); QVERIFY(approvalToggle);
        QVERIFY(QMetaObject::invokeMethod(approvalToggle, "clicked"));
        QTRY_VERIFY(!approvalFull->property("expanded").toBool());
        auto *approve = ui.find("missionApproveButton"); QVERIFY(approve);
        QVERIFY(approve->isEnabled());
        // The click must flow through QML; a second click cannot submit again.
        QVERIFY(ui.click("missionApproveButton"));
        QTRY_COMPARE(f.approvalPosts, 1);
        QTRY_COMPARE(ui.missions.status(), QString("running"));
        QVERIFY(!approve->isEnabled());
        QCOMPARE(f.approvalPosts, 1);
    }
    void approvalNoticeTracksPackageChangesInsteadOfExecutionProgress()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *panel = ui.find("approvalPanel"); QVERIFY(panel);
        QTRY_VERIFY(panel->property("changeNotice").toString().isEmpty());

        auto next = f.snapshots.value(MissionA);
        auto review = next.value("review").toObject();
        next["status"] = "running";
        next["current_revision"] = 9;
        next["last_event_id"] = MissionA + ":9";
        review["revision"] = 9;
        next["review"] = review;
        f.snapshots[MissionA] = next;
        ui.missions.refresh();
        QTRY_COMPARE(ui.missions.revision(), 9);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(panel->property("changeNotice").toString().isEmpty());

        next["status"] = "awaiting_human";
        next["current_revision"] = 10;
        next["last_event_id"] = MissionA + ":10";
        review["revision"] = 10;
        review["plan_hash"] = QString(64, 'd');
        next["review"] = review;
        f.snapshots[MissionA] = next;
        ui.missions.refresh();
        QTRY_COMPARE(ui.missions.revision(), 10);
        QTRY_VERIFY(panel->property("changeNotice").toString().contains("Plan Hash"));
        QVERIFY(!ui.find("missionApproveButton")->isEnabled());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void deliveryShowsExportedArtifactPath_data()
    {
        QTest::addColumn<QString>("mode");
        QTest::newRow("patch") << QString("patch");
        QTest::newRow("bundle") << QString("bundle");
    }
    void deliveryShowsExportedArtifactPath()
    {
        QFETCH(QString, mode);
        const QString exportedPath = "D:/demo/export." + mode;
        Fixture f;
        f.snapshots[MissionA] = mission(MissionA, "succeeded");
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            QJsonObject result;
            if (!r.body.contains("token")) {
                result = {{"task_id", MissionA}, {"token", QString(64, 'f')}};
            } else {
                f.snapshots[MissionA] = mission(MissionA, "succeeded", 9);
                result = {{"mode", mode}, {"branch", "forgepilot/task-demo"},
                    {"commit", QString(40, 'a')}, {mode, QJsonObject{{"path", exportedPath},
                        {"sha256", QString(64, 'b')}, {"size", 367}}}};
            }
            Source::json(r, {{"mission", f.snapshots.value(MissionA)}, {"delivery", result}});
            return true;
        };
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", 7);
        auto *panel = ui.find("deliveryPanel"); QVERIFY(panel);
        ui.missions.delivery()->prepare(mode);
        QTRY_VERIFY(ui.missions.delivery()->canConfirm());
        ui.missions.delivery()->confirm();
        QTRY_COMPARE(ui.missions.delivery()->result().value("mode").toString(), mode);
        QTRY_VERIFY(panel->property("resultDetails").toString().contains(exportedPath));
        QVERIFY(panel->property("resultDetails").toString().contains("forgepilot/task-demo"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void activeMissionMustBeCancelledBeforeAbandon_data()
    {
        QTest::addColumn<QString>("status");
        QTest::newRow("waiting") << QString("awaiting_human");
        QTest::newRow("executing") << QString("running");
    }
    void activeMissionMustBeCancelledBeforeAbandon()
    {
        QFETCH(QString, status);
        Fixture f;
        f.snapshots[MissionA] = mission(MissionA, status);
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", 7);
        auto *mode = ui.find("missionDeliveryMode"); QVERIFY(mode);
        mode->setProperty("currentIndex", 4);
        auto *preview = ui.find("missionDeliveryPreviewButton"); QVERIFY(preview);
        QVERIFY(!preview->isEnabled());
        QVERIFY(preview->property("text").toString().contains("先取消任务"));
        ui.missions.delivery()->prepare("abandon");
        QCOMPARE(ui.missions.delivery()->error(), preview->property("text").toString());
        QVERIFY(!ui.missions.delivery()->busy());
    }
    void deliveryPreviewSurvivesTabSwitchAndConfirmsOnce_data()
    {
        QTest::addColumn<QString>("mode");
        QTest::addColumn<int>("modeIndex");
        QTest::addColumn<QString>("targetBranch");
        QTest::newRow("abandon") << QString("abandon") << 4 << QString();
        QTest::newRow("squash") << QString("squash") << 3 << QString("release/delivery");
    }
    void deliveryPreviewSurvivesTabSwitchAndConfirmsOnce()
    {
        QFETCH(QString, mode);
        QFETCH(int, modeIndex);
        QFETCH(QString, targetBranch);
        const QString token(64, 'f');
        Fixture f;
        f.snapshots[MissionA] = mission(MissionA, "succeeded");
        QList<QJsonObject> deliveryPosts;
        Request confirmation;
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            deliveryPosts.append(r.body);
            if (r.body.contains("token")) confirmation = r;
            else {
                f.snapshots[MissionA] = mission(MissionA, "succeeded", 9);
                Source::json(r, {{"mission", f.snapshots.value(MissionA)},
                    {"delivery", QJsonObject{{"task_id", MissionA}, {"token", token},
                        {"target_branch", targetBranch.isEmpty() ? QJsonValue::Null : QJsonValue(targetBranch)}}}});
            }
            return true;
        };
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        if (!details->isVisible()) QVERIFY(ui.click("workbenchDetailsToggle"));
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", 7);
        auto *modeInput = ui.find("missionDeliveryMode"); QVERIFY(modeInput);
        auto *targetInput = ui.find("missionDeliveryTarget"); QVERIFY(targetInput);
        QVERIFY(modeInput->setProperty("currentIndex", modeIndex));
        QVERIFY(targetInput->setProperty("text", targetBranch));
        auto *previewButton = ui.find("missionDeliveryPreviewButton"); QVERIFY(previewButton);
        QVERIFY(previewButton->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(previewButton, "clicked"));
        QTRY_VERIFY(ui.missions.delivery()->canConfirm());
        QCOMPARE(deliveryPosts.size(), 1);
        QCOMPARE(deliveryPosts.first().value("mode").toString(), mode);
        QCOMPARE(deliveryPosts.first().value("target_branch").toString(), targetBranch);
        QCOMPARE(deliveryPosts.first().value("expected_revision").toInteger(), 8);

        QPointer<QQuickItem> oldPanel = ui.find("deliveryPanel"); QVERIFY(oldPanel);
        tabs->setProperty("currentIndex", 4);
        QTRY_VERIFY(oldPanel.isNull());
        QVERIFY(ui.find("missionDiffPanel"));
        tabs->setProperty("currentIndex", 7);
        QTRY_VERIFY(ui.find("deliveryPanel"));
        modeInput = ui.find("missionDeliveryMode"); QVERIFY(modeInput);
        targetInput = ui.find("missionDeliveryTarget"); QVERIFY(targetInput);
        QCOMPARE(modeInput->property("currentIndex").toInt(), modeIndex);
        QCOMPARE(targetInput->property("text").toString(), targetBranch);
        QVERIFY(!modeInput->isEnabled());
        QVERIFY(!targetInput->isEnabled());
        auto *warning = ui.find("missionDeliveryWarning"); QVERIFY(warning);
        QVERIFY(warning->isVisible());
        const auto warningText = warning->property("text").toString();
        QVERIFY(warningText.contains("危险"));
        if (mode == "abandon") QVERIFY(warningText.contains("丢弃任务的隔离工作区"));
        else {
            QVERIFY(warningText.contains("创建合并提交"));
            QVERIFY(warningText.contains(targetBranch));
        }
        auto *confirmButton = ui.find("missionDeliveryConfirmButton"); QVERIFY(confirmButton);
        QVERIFY(confirmButton->isEnabled());
        QVERIFY(confirmButton->property("danger").toBool());
        QVERIFY(QMetaObject::invokeMethod(confirmButton, "clicked"));
        QTRY_VERIFY(ui.find("missionDeliveryDialogConfirmButton"));
        auto *accept = ui.find("missionDeliveryDialogConfirmButton"); QVERIFY(accept);
        auto *confirmText = ui.find("missionDeliveryConfirmText"); QVERIFY(confirmText);
        QTRY_VERIFY(accept->isVisible());
        QVERIFY(accept->isEnabled());
        QVERIFY(accept->property("danger").toBool());
        QVERIFY(confirmText->property("text").toString().contains("方式：" + mode));
        if (mode == "abandon")
            QVERIFY(confirmText->property("text").toString().contains("隔离工作区将被丢弃"));
        else {
            QVERIFY(confirmText->property("text").toString().contains("目标分支引用会改变"));
            QVERIFY(confirmText->property("text").toString().contains(targetBranch));
        }
        QCOMPARE(deliveryPosts.size(), 1);
        QVERIFY(ui.click("missionDeliveryDialogConfirmButton"));
        ui.missions.delivery()->confirm();
        QTRY_VERIFY(confirmation.socket);
        QCOMPARE(deliveryPosts.size(), 2);
        QCOMPARE(confirmation.body.value("mode").toString(), mode);
        QCOMPARE(confirmation.body.value("target_branch").toString(), targetBranch);
        QCOMPARE(confirmation.body.value("token").toString(), token);
        QCOMPARE(confirmation.body.value("expected_revision").toInteger(), 9);
        QVERIFY(ui.missions.delivery()->busy());
        QVERIFY(!ui.missions.delivery()->canConfirm());
        QVERIFY(!confirmButton->isEnabled());
        QVERIFY(!accept->isEnabled());

        f.snapshots[MissionA] = mission(MissionA, "succeeded", 10);
        Source::json(confirmation, {{"mission", f.snapshots.value(MissionA)},
            {"delivery", QJsonObject{{"mode", mode}, {"target_branch", targetBranch.isEmpty() ? QJsonValue::Null : QJsonValue(targetBranch)}}}});
        QTRY_COMPARE(ui.missions.delivery()->result().value("mode").toString(), mode);
        QTRY_VERIFY(ui.missions.synchronized());
        ui.missions.delivery()->confirm();
        QTest::qWait(50);
        QCOMPARE(deliveryPosts.size(), 2);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void deliveryRevisionChangeClearsPreviewAndBlocksOpenConfirmation_data()
    {
        QTest::addColumn<QString>("mode");
        QTest::newRow("abandon") << QString("abandon");
        QTest::newRow("squash") << QString("squash");
    }
    void deliveryRevisionChangeClearsPreviewAndBlocksOpenConfirmation()
    {
        QFETCH(QString, mode);
        Fixture f;
        f.snapshots[MissionA] = mission(MissionA, "succeeded");
        int deliveryPosts = 0;
        f.intercept = [&](const Request &r) {
            if (!r.path.endsWith("/delivery")) return false;
            ++deliveryPosts;
            Source::json(r, {{"mission", f.snapshots.value(MissionA)},
                {"delivery", QJsonObject{{"task_id", MissionA}, {"token", QString(64, 'f')}}}});
            return true;
        };
        Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *details = ui.find("workbenchDetails"); QVERIFY(details);
        if (!details->isVisible()) QVERIFY(ui.click("workbenchDetailsToggle"));
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", 7);
        ui.missions.delivery()->prepare(mode, mode == "squash" ? "release/delivery" : "");
        QTRY_VERIFY(ui.missions.delivery()->canConfirm());
        auto *confirmButton = ui.find("missionDeliveryConfirmButton"); QVERIFY(confirmButton);
        QVERIFY(QMetaObject::invokeMethod(confirmButton, "clicked"));
        QTRY_VERIFY(ui.find("missionDeliveryDialogConfirmButton"));
        auto *accept = ui.find("missionDeliveryDialogConfirmButton"); QVERIFY(accept);
        QTRY_VERIFY(accept->isVisible());
        QVERIFY(accept->isEnabled());

        f.snapshots[MissionA] = mission(MissionA, "succeeded", 9);
        ui.missions.refresh();
        QTRY_COMPARE(ui.missions.revision(), 9);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(ui.missions.delivery()->preview().isEmpty());
        QVERIFY(ui.missions.delivery()->property("previewMode").toString().isEmpty());
        QVERIFY(ui.missions.delivery()->property("previewTarget").toString().isEmpty());
        QVERIFY(!ui.missions.delivery()->canConfirm());
        QVERIFY(!confirmButton->isVisible());
        QVERIFY(!confirmButton->isEnabled());
        QVERIFY(!accept->isEnabled());
        QTRY_VERIFY(!accept->isVisible());
        auto *warning = ui.find("missionDeliveryWarning"); QVERIFY(warning);
        QVERIFY(!warning->isVisible());
        ui.missions.delivery()->confirm();
        QTest::qWait(50);
        QCOMPARE(deliveryPosts, 1);
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void rejectionNeedsDialogConfirmationAndUsesFrozenRevision()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_VERIFY(ui.missions.approval()->available());
        const auto frozenReview = ui.missions.approval()->review();

        QVERIFY(ui.click("missionRejectButton"));
        QCOMPARE(f.approvalPosts, 0);
        QTest::keyClick(ui.window, Qt::Key_Escape);
        QVERIFY(ui.click("missionRejectButton"));
        QCOMPARE(f.approvalPosts, 0);
        auto *confirm = ui.textItem(ui.window->contentItem(), "确认拒绝");
        QVERIFY(confirm);
        QVERIFY(confirm->isVisible());
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            confirm->mapToScene(QPointF(confirm->width()/2, confirm->height()/2)).toPoint());
        QTRY_COMPARE(f.approvalPosts, 1);
        QTRY_COMPARE(ui.missions.status(), QString("cancelled"));
        const auto approvals = std::find_if(f.source.requests.cbegin(), f.source.requests.cend(), [](const Request &r) {
            return r.method == "POST" && r.path.endsWith("/approvals");
        });
        QVERIFY(approvals != f.source.requests.cend());
        QCOMPARE(approvals->body.value("decision").toString(), QString("rejected"));
        QCOMPARE(approvals->body.value("expected_revision").toInteger(), 8);
        QCOMPARE(approvals->body.value("task_spec_hash").toString(), frozenReview.value("task_spec_hash").toString());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void keepsInterruptedTaskRecoverableAfterSwitch()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        ui.missions.selectMission(MissionB);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(ui.missions.status(), QString("interrupted"));
        QCOMPARE(ui.missions.missions()->count(), 2);
        QVERIFY(ui.find("missionTaskList"));
        auto *first = ui.taskRow(MissionA); QVERIFY(first);
        ui.reveal(first);
        ui.window->update(); QTest::qWait(30);
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            first->mapToScene(QPointF(first->width()/2, first->height()/2)).toPoint());
        QTRY_COMPARE(ui.missions.taskId(), MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(ui.missions.status(), QString("awaiting_human"));
        auto *second = ui.taskRow(MissionB); QVERIFY(second);
        ui.reveal(second);
        ui.window->update(); QTest::qWait(30);
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            second->mapToScene(QPointF(second->width()/2, second->height()/2)).toPoint());
        QTRY_COMPARE(ui.missions.taskId(), MissionB);
        QTRY_VERIFY(ui.missions.synchronized());
        auto *recovery = ui.find("missionRecoveryButton"); QVERIFY(recovery);
        QVERIFY(recovery->isVisible());
        QVERIFY(ui.find("missionRefreshButton"));
        // The approval from the previous task must not remain actionable.
        if (auto *approve = ui.find("missionApproveButton")) QVERIFY(!approve->isEnabled());
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void boundsDiffAndValidationText()
    {
        Fixture f; Ui ui(f, 860, 640);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        auto items = f.artifacts.value("items").toArray();
        QJsonObject changeSet, report;
        for (const auto &entry : items) {
            const auto artifact = entry.toObject();
            if (artifact.value("artifact_type") == "ChangeSet") changeSet = artifact;
            if (artifact.value("artifact_type") == "ValidationReport") report = artifact;
        }
        QVERIFY(!changeSet.isEmpty()); QVERIFY(!report.isEmpty());
        const auto longPatch = (QString("+diff line ") + QString(160, 'x') + '\n').repeated(3000);
        const auto longPath = QString("src/") + QString("deeply_nested_directory/").repeated(14) + "implementation.cpp";
        auto changeContent = changeSet.value("content").toObject();
        auto files = changeContent.value("files").toArray();
        QVERIFY(!files.isEmpty());
        auto file = files.first().toObject(); file["unified_diff"] = longPatch; file["path"] = longPath; files[0] = file;
        changeContent["files"] = files; changeSet["content"] = changeContent;
        ui.missions.diff()->setSnapshot({{"task_id", MissionA}, {"patch", ""}, {"change_sets", QJsonArray{changeSet}}});
        const auto longOutput = (QString("log line ") + QString(160, 'y') + '\n').repeated(3000);
        const auto longCommand = QString("cmake --build ") + QString("very_deep_build_directory/").repeated(12);
        auto reportContent = report.value("content").toObject();
        auto commands = reportContent.value("commands").toArray();
        QVERIFY(!commands.isEmpty());
        auto command = commands.first().toObject(); command["output_summary"] = longOutput; command["command_summary"] = longCommand; commands[0] = command;
        reportContent["commands"] = commands; report["content"] = reportContent;
        ui.missions.validation()->setSnapshot({{"task_id", MissionA}, {"items", QJsonArray{report}}, {"latest", report}});
        auto *tabs = ui.find("workbenchDetailTabs"); QVERIFY(tabs);
        tabs->setProperty("currentIndex", 4);
        QTRY_VERIFY(ui.find("missionDiffFiles"));
        QQuickItem *diffRow = nullptr;
        QTRY_VERIFY((diffRow = ui.textItem(ui.find("missionDiffFiles"), file.value("path").toString())));
        QVERIFY(diffRow->isEnabled());
        ui.window->update(); QTest::qWait(30);
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            diffRow->mapToScene(QPointF(diffRow->width()/2, diffRow->height()/2)).toPoint());
        auto *diffText = ui.find("missionDiffText"); QVERIFY(diffText);
        QTRY_COMPARE(diffText->property("text").toString().size(), 12000);
        QCOMPARE(diffText->property("text").toString(), longPatch.left(12000));
        auto *diffScroll = ui.find("missionDiffScroll"); QVERIFY(diffScroll);
        QTRY_VERIFY(diffScroll->property("contentHeight").toReal() > diffScroll->height());
        QTRY_VERIFY(diffScroll->property("contentWidth").toReal() > diffScroll->width());
        auto *diffFlickable = qobject_cast<QQuickItem *>(
            diffScroll->property("contentItem").value<QObject *>());
        QVERIFY(diffFlickable);
        QVERIFY(diffFlickable->setProperty("contentY", 100));
        QVERIFY(diffFlickable->setProperty("contentX", 100));
        QTRY_VERIFY(diffFlickable->property("contentY").toReal() > 0);
        QTRY_VERIFY(diffFlickable->property("contentX").toReal() > 0);
        auto *diffPath = ui.find("missionDiffPathFullText"); QVERIFY(diffPath);
        QVERIFY(diffPath->property("text").toString().contains(longPath));
        auto *diffPathCopy = ui.find("missionDiffPathFullTextCopy"); QVERIFY(diffPathCopy);
        QVERIFY(QMetaObject::invokeMethod(diffPathCopy, "clicked"));
        QTRY_VERIFY(QGuiApplication::clipboard()->text().contains(longPath));
        auto *diffCopy = ui.find("missionDiffCopyButton"); QVERIFY(diffCopy);
        QVERIFY(QMetaObject::invokeMethod(diffCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longPatch.left(12000));
        auto *more = ui.find("missionDiffMore"); QVERIFY(more); QVERIFY(more->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(more, "clicked"));
        QTRY_COMPARE(diffText->property("text").toString(), longPatch.mid(12000, 12000));
        const auto captureDir = qEnvironmentVariable("FORGEPILOT_UI_OUTPUT");
        if (!captureDir.isEmpty()) {
            QVERIFY(QDir().mkpath(captureDir));
            QVERIFY(ui.window->grabWindow().save(captureDir + "/long-diff-860x640.png"));
        }
        tabs->setProperty("currentIndex", 5);
        QTRY_VERIFY(ui.find("missionValidationCommands"));
        QQuickItem *validationRow = nullptr;
        QTRY_VERIFY((validationRow = ui.textItem(ui.find("missionValidationCommands"), command.value("command_summary").toString())));
        ui.window->update(); QTest::qWait(30);
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            validationRow->mapToScene(QPointF(validationRow->width()/2, validationRow->height()/2)).toPoint());
        auto *output = ui.find("missionValidationOutput"); QVERIFY(output);
        QTRY_COMPARE(output->property("text").toString(), longOutput.left(12000));
        auto *validationScroll = ui.find("missionValidationScroll"); QVERIFY(validationScroll);
        QTRY_VERIFY(validationScroll->property("contentHeight").toReal() > validationScroll->height());
        QTRY_VERIFY(validationScroll->property("contentWidth").toReal() > validationScroll->width());
        auto *validationFlickable = qobject_cast<QQuickItem *>(
            validationScroll->property("contentItem").value<QObject *>());
        QVERIFY(validationFlickable);
        QVERIFY(validationFlickable->setProperty("contentY", 100));
        QVERIFY(validationFlickable->setProperty("contentX", 100));
        QTRY_VERIFY(validationFlickable->property("contentY").toReal() > 0);
        QTRY_VERIFY(validationFlickable->property("contentX").toReal() > 0);
        auto *commandFull = ui.find("missionValidationCommandFull"); QVERIFY(commandFull);
        QCOMPARE(commandFull->property("text").toString(), longCommand);
        auto *commandCopy = ui.find("missionValidationCommandFullCopy"); QVERIFY(commandCopy);
        QVERIFY(QMetaObject::invokeMethod(commandCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longCommand);
        auto *logCopy = ui.find("missionValidationCopyButton"); QVERIFY(logCopy);
        QVERIFY(QMetaObject::invokeMethod(logCopy, "clicked"));
        QTRY_COMPARE(QGuiApplication::clipboard()->text(), longOutput.left(12000));
        if (!captureDir.isEmpty())
            QVERIFY(ui.window->grabWindow().save(captureDir + "/long-validation-860x640.png"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
    void reconnectsWithoutLosingUiAndReportsBackendExit()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        ui.missions.selectMission(MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QTRY_COMPARE(f.streams.size(), 1);
        Request held;
        bool holdGraph = true;
        f.intercept = [&](const Request &r) {
            if (holdGraph && r.path.endsWith("/graph")) { held = r; holdGraph = false; return true; }
            return false;
        };
        f.streams.last().socket->disconnectFromHost();
        QTRY_VERIFY(held.socket);
        QVERIFY(!ui.missions.synchronized());
        if (auto *approve = ui.find("missionApproveButton")) QVERIFY(!approve->isEnabled());
        f.intercept = {};
        Source::json(held, graph(f.snapshots.value(MissionA)));
        QTRY_VERIFY_WITH_TIMEOUT(f.streams.size() >= 2, 5000);
        QTRY_VERIFY(ui.missions.synchronized());
        QVERIFY(f.streams.last().headers.toLower().contains("last-event-id: " + MissionA.toUtf8() + ":8"));
        // Stop accepting connections and close the live stream as a backend-exit simulation.
        f.source.close();
        f.streams.last().socket->disconnectFromHost();
        QTRY_VERIFY_WITH_TIMEOUT(!ui.missions.error().isEmpty(), 5000);
        QVERIFY(ui.find("missionRefreshButton"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
    }
};
QTEST_MAIN(WorkbenchQmlTests)
#include "tst_workbench_qml.moc"
