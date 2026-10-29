#include "ClientUi.h"
#include "ConversationController.h"
#include "TaskController.h"
#include "workbench/MissionController.h"
#include "HttpFixture.h"
#include <QFile>
#include <QGuiApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickItem>
#include <QQuickWindow>
#include <QtTest>
#include <functional>

namespace {
const QString MissionA = "10000000-0000-4000-a000-000000000001";
const QString MissionB = "10000000-0000-4000-a000-000000000009";

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
    const QJsonObject node{{"node_id", "execution_approval"}, {"node_type", "human_gate"},
        {"status", status}, {"attempt", 1}, {"result", QJsonValue::Null}};
    const QJsonObject definition{{"node_id", "execution_approval"}, {"node_type", "human_gate"},
        {"handler", "execution_approval"}, {"dependencies", QJsonArray{}},
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
    TaskController tasks;
    ConversationController conversations{&tasks};
    MissionController missions;
    QQmlApplicationEngine engine;
    QQuickWindow *window = nullptr;
    QStringList warnings;
    explicit Ui(Fixture &f)
    {
        tasks.setBaseUrl(f.source.url()); missions.setBaseUrl(f.source.url());
        QObject::connect(&engine, &QQmlEngine::warnings, &engine, [this](const QList<QQmlError> &errors) {
            for (const auto &error : errors) warnings.append(error.toString());
        });
        engine.setInitialProperties({{"controller", QVariant::fromValue(&tasks)},
            {"conversations", QVariant::fromValue(&conversations)},
            {"missions", QVariant::fromValue(&missions)}, {"width", 1200}, {"height", 760}});
    }
    bool load()
    {
        engine.load(QUrl::fromLocalFile(QString(FORGEPILOT_SOURCE_DIR) + "/client/Main.qml"));
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
    void switchesTopLevelModesWithoutChangingConversationMode()
    {
        Fixture f; Ui ui(f);
        QVERIFY2(ui.load(), qPrintable(ui.warnings.join('\n')));
        auto *prompt = ui.find("conversationPrompt"); QVERIFY(prompt);
        prompt->setProperty("text", "Draft stays in free chat");
        auto *workbench = ui.find("agentWorkbenchPage"); QVERIFY(workbench);
        QVERIFY(ui.click("agentWorkbenchModeButton"));
        QTRY_VERIFY(workbench->isVisible());
        QCOMPARE(ui.conversations.mode(), QString("chat"));
        QCOMPARE(prompt->property("text").toString(), QString("Draft stays in free chat"));
        QVERIFY(ui.click("freeChatModeButton"));
        QTRY_VERIFY(!workbench->isVisible());
        QVERIFY(ui.find("sendMessageButton"));
        QVERIFY2(ui.warnings.isEmpty(), qPrintable(ui.warnings.join('\n')));
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
        auto *approve = ui.find("missionApproveButton"); QVERIFY(approve);
        QVERIFY(approve->isEnabled());
        // The click must flow through QML; a second click cannot submit again.
        QVERIFY(ui.click("missionApproveButton"));
        QTRY_COMPARE(f.approvalPosts, 1);
        QTRY_COMPARE(ui.missions.status(), QString("running"));
        QVERIFY(!approve->isEnabled());
        QCOMPARE(f.approvalPosts, 1);
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
        ui.window->update(); QTest::qWait(30);
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            first->mapToScene(QPointF(first->width()/2, first->height()/2)).toPoint());
        QTRY_COMPARE(ui.missions.taskId(), MissionA);
        QTRY_VERIFY(ui.missions.synchronized());
        QCOMPARE(ui.missions.status(), QString("awaiting_human"));
        auto *second = ui.taskRow(MissionB); QVERIFY(second);
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
        Fixture f; Ui ui(f);
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
        const auto longPatch = QString("+diff line\n").repeated(3000);
        auto changeContent = changeSet.value("content").toObject();
        auto files = changeContent.value("files").toArray();
        QVERIFY(!files.isEmpty());
        auto file = files.first().toObject(); file["unified_diff"] = longPatch; files[0] = file;
        changeContent["files"] = files; changeSet["content"] = changeContent;
        ui.missions.diff()->setSnapshot({{"task_id", MissionA}, {"patch", ""}, {"change_sets", QJsonArray{changeSet}}});
        const auto longOutput = QString("log line\n").repeated(3000);
        auto reportContent = report.value("content").toObject();
        auto commands = reportContent.value("commands").toArray();
        QVERIFY(!commands.isEmpty());
        auto command = commands.first().toObject(); command["output_summary"] = longOutput; commands[0] = command;
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
        auto *more = ui.find("missionDiffMore"); QVERIFY(more); QVERIFY(more->isEnabled());
        QVERIFY(QMetaObject::invokeMethod(more, "clicked"));
        QTRY_COMPARE(diffText->property("text").toString(), longPatch.mid(12000, 12000));
        tabs->setProperty("currentIndex", 5);
        QTRY_VERIFY(ui.find("missionValidationCommands"));
        QQuickItem *validationRow = nullptr;
        QTRY_VERIFY((validationRow = ui.textItem(ui.find("missionValidationCommands"), command.value("command_summary").toString())));
        ui.window->update(); QTest::qWait(30);
        QTest::mouseClick(ui.window, Qt::LeftButton, Qt::NoModifier,
            validationRow->mapToScene(QPointF(validationRow->width()/2, validationRow->height()/2)).toPoint());
        auto *output = ui.find("missionValidationOutput"); QVERIFY(output);
        QTRY_COMPARE(output->property("text").toString(), longOutput.left(12000));
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
