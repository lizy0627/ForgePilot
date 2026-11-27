#include <QtTest>
#include <QAbstractItemModelTester>
#include <QJsonArray>
#include <QJsonDocument>
#include <QFile>
#include <QPointer>
#include "workbench/MissionListModel.h"
#include "workbench/GraphModel.h"
#include "workbench/ArtifactModel.h"
#include "workbench/EvidenceModel.h"
#include "workbench/DiffModel.h"
#include "workbench/ValidationModel.h"
#include "workbench/MissionProtocol.h"

namespace {
const QString taskId = "11111111-1111-4111-8111-111111111111";
const QString artifactId = "22222222-2222-4222-8222-222222222222";
const QString timestamp = "2026-10-27T01:00:00+00:00";
QJsonObject mission(qint64 revision, QString status = "running")
{
    return {{"task_id",taskId},{"status",status},{"current_revision",revision},
        {"repository_path","D:/Projects/Example"},{"request","Repair worker shutdown"},
        {"task_type","bug_fix"},{"created_at",timestamp},{"updated_at",timestamp}};
}
QJsonObject graph(qint64 revision, const QString &state = "running", const QString &role = "analyst")
{
    const QJsonObject definition{{"node_id","investigate"},{"node_type","agent"},
        {"handler","analysis"},{"dependencies",QJsonArray{"draft_task"}},
        {"agent_id","root-cause-analyst.v1"},{"settings",QJsonObject{{"recovery_role",role}}}};
    const QJsonObject result{{"status",state},{"decision_summary","Located worker lifetime"},
        {"metrics",QJsonObject{{"duration_ms",10322},{"model_calls",2},{"tool_calls",4},{"total_tokens",700}}},
        {"output_artifact_ids",QJsonArray{artifactId}}};
    const QJsonObject node{{"node_id","investigate"},{"node_type","agent"},
        {"status",state},{"attempt",1},{"result",state == "running" ? QJsonValue{} : QJsonValue(result)}};
    return {{"task_id",taskId},{"graph_id","mission"},{"graph_version",1},
        {"definition_hash",QString(64,'a')},{"status",state},{"current_revision",revision},
        {"nodes",QJsonArray{node}},{"definition",QJsonObject{{"graph_id","mission"},{"version",1},
        {"nodes",QJsonArray{definition}},{"edges",QJsonArray{}}}}};
}
QJsonObject artifact(const QString &type, const QJsonObject &content, const QString &id = artifactId)
{
    return {{"artifact_id",id},{"artifact_type",type},{"schema_version","1.0"},
        {"task_id",taskId},{"producer","root-cause-analyst.v1"},{"producer_node","investigate"},
        {"created_at",timestamp},{"parent_artifact_ids",QJsonArray{}},
        {"content_sha256",QString(64,'b')},{"content",content}};
}
QJsonObject bundle(const QString &id = artifactId)
{
    return artifact("EvidenceBundle", {{"summary","Lifetime evidence"},{"items",QJsonArray{
        QJsonObject{{"evidence_id","EV-1"},{"claim","Worker survives the window"},
            {"path","src/window.cpp"},{"start_line",12},{"end_line",14},
            {"file_hash",QString(64,'a')},{"symbol","Window::~Window"},{"source","local_repository"},
            {"confidence",0.95},{"reason","Destructor does not stop the worker"}}}}}, id);
}
QJsonObject change(const QString &path, const QString &id = artifactId)
{
    return artifact("ChangeSet", {{"execution_plan_artifact_id",artifactId},
        {"summary","Stop the worker"},{"requires_additional_approval",false},{"files",QJsonArray{
        QJsonObject{{"path",path},{"change_type","modified"},{"unified_diff","@@\n-stop\n+stopAndWait\n"},
            {"before_sha256",QString(64,'a')},{"after_sha256",QString(64,'b')},
            {"plan_step_ids",QJsonArray{"STEP-1"}},{"acceptance_criteria_ids",QJsonArray{"AC-1"}},
            {"test_change","none"}}}}}, id);
}
}

class WorkbenchModelTests : public QObject {
    Q_OBJECT
private slots:
    void missionRowsPreserveRevisionAndNotify();
    void graphRolesAndUpdates();
    void parallelAnalystsAndJoinExposeOnlyActiveAgents();
    void graphRecoveryActions();
    void advertisedAndLegacyRetryActions_data();
    void advertisedAndLegacyRetryActions();
    void graphApprovalActions();
    void malformedAndLateSnapshotsAreAtomic();
    void evidenceKeepsBundleIdentity();
    void diffUsesCumulativeChangeSet();
    void validationCommandsAndEmptyReport();
    void backendArtifactsMatchModels();
    void signalsMayDestroyModels();
    void reentrantMissionSwitchSerializesModelChanges();
};

void WorkbenchModelTests::missionRowsPreserveRevisionAndNotify()
{
    MissionListModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    QSignalSpy inserted(&model, &QAbstractItemModel::rowsInserted);
    QSignalSpy changed(&model, &QAbstractItemModel::dataChanged);
    QSignalSpy reset(&model, &QAbstractItemModel::modelReset);
    model.upsert(mission(5));
    QCOMPARE(model.count(), 1);
    QCOMPARE(inserted.size(), 1);
    model.upsert(mission(4, "pending"));
    QCOMPARE(model.get(0).value("revision").toLongLong(), 5);
    QCOMPARE(changed.size(), 0);
    model.upsert(mission(6, "awaiting_human"));
    QCOMPARE(changed.size(), 1);
    QCOMPARE(reset.size(), 0);
    QCOMPARE(model.data(model.index(0), MissionListModel::StatusRole).toString(), "awaiting_human");
    QCOMPARE(model.rowCount(model.index(0)), 0);
    QVERIFY(!model.data(model.index(0), Qt::UserRole + 999).isValid());
    model.clear();
    QCOMPARE(model.count(), 0);
    QCOMPARE(reset.size(), 1);
}

void WorkbenchModelTests::graphRolesAndUpdates()
{
    GraphModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    QSignalSpy reset(&model, &QAbstractItemModel::modelReset);
    QSignalSpy changed(&model, &QAbstractItemModel::dataChanged);
    model.setSnapshot(graph(2));
    QCOMPARE(model.currentAgents(), QStringList{"root-cause-analyst.v1"});
    QCOMPARE(model.get(0).value("dependencies").toList(), QVariantList{"draft_task"});
    QCOMPARE(model.get(0).value("currentAgent").toString(), "root-cause-analyst.v1");
    model.setSnapshot(graph(3, "succeeded"));
    QCOMPARE(reset.size(), 1);
    QCOMPARE(changed.size(), 1);
    QCOMPARE(model.revision(), 3);
    QCOMPARE(model.status(), "succeeded");
    QVERIFY(model.currentAgents().isEmpty());
    QCOMPARE(model.get(0).value("metrics").toMap().value("total_tokens").toInt(), 700);
    QCOMPARE(model.get(0).value("metrics").toMap().value("duration_ms").toInt(), 10322);
    QVERIFY(model.get(0).value("actions").toStringList().isEmpty());
    model.setSnapshot(graph(3, "succeeded"));
    QCOMPARE(changed.size(), 1);
    model.clear();
    QCOMPARE(model.count(), 0);
    QCOMPARE(model.revision(), 0);
    QVERIFY(model.taskId().isEmpty());
}

void WorkbenchModelTests::parallelAnalystsAndJoinExposeOnlyActiveAgents()
{
    GraphModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    auto snapshot = graph(20);
    auto definition = snapshot.value("definition").toObject();
    auto analystA = definition.value("nodes").toArray().first().toObject();
    analystA["node_id"] = "root_cause";
    analystA["agent_id"] = "root-cause-analyst.v1";
    auto analystB = analystA;
    analystB["node_id"] = "impact";
    analystB["agent_id"] = "impact-analyst.v1";
    auto join = analystA;
    join["node_id"] = "join";
    join["node_type"] = "join";
    join["handler"] = "join";
    join["agent_id"] = QJsonValue::Null;
    join["dependencies"] = QJsonArray{"root_cause", "impact"};
    definition["nodes"] = QJsonArray{analystA, analystB, join};
    snapshot["definition"] = definition;
    auto first = snapshot.value("nodes").toArray().first().toObject();
    first["node_id"] = "root_cause";
    auto second = first;
    second["node_id"] = "impact";
    auto joinState = first;
    joinState["node_id"] = "join";
    joinState["node_type"] = "join";
    joinState["status"] = "pending";
    snapshot["nodes"] = QJsonArray{first, second, joinState};
    model.setSnapshot(snapshot);
    QCOMPARE(model.count(), 3);
    QCOMPARE(model.currentAgents(), (QStringList{"root-cause-analyst.v1", "impact-analyst.v1"}));
    QCOMPARE(model.get(1).value("currentAgent").toString(), "impact-analyst.v1");
    QCOMPARE(model.get(2).value("dependencies").toList(), (QVariantList{"root_cause", "impact"}));
    QVERIFY(model.get(2).value("currentAgent").toString().isEmpty());

    snapshot["current_revision"] = 21;
    first["status"] = "succeeded";
    second["status"] = "succeeded";
    joinState["status"] = "running";
    snapshot["nodes"] = QJsonArray{first, second, joinState};
    model.setSnapshot(snapshot);
    QCOMPARE(model.revision(), 21);
    QVERIFY(model.currentAgents().isEmpty());
    QVERIFY(model.get(0).value("currentAgent").toString().isEmpty());
    QCOMPARE(model.get(2).value("status").toString(), "running");
}

void WorkbenchModelTests::graphRecoveryActions()
{
    GraphModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    model.setSnapshot(graph(5, "retryable"));
    QCOMPARE(model.get(0).value("actions").toStringList(), QStringList{"retry"});
    QVERIFY(model.allowsRetry({}));
    model.setRecoveryRequired(true);
    QCOMPARE(model.get(0).value("actions").toStringList(), QStringList{"resume"});
    QVERIFY(!model.allowsRetry({}));
    model.setRecoveryRequired(false);
    QCOMPARE(model.get(0).value("actions").toStringList(), QStringList{"retry"});
    model.setSnapshot(graph(6, "retryable", "writer"));
    QCOMPARE(model.get(0).value("actions").toStringList(), QStringList{"resume"});
    model.setSnapshot(graph(7, "failed"));
    QVERIFY(model.get(0).value("actions").toStringList().isEmpty());
    auto interrupted = graph(8, "retryable");
    interrupted["status"] = "interrupted";
    model.setSnapshot(interrupted);
    QVERIFY(!model.recoveryRequired());
    QCOMPARE(model.get(0).value("actions").toStringList(), QStringList{"resume"});
}

void WorkbenchModelTests::advertisedAndLegacyRetryActions_data()
{
    QTest::addColumn<QString>("id");
    QTest::addColumn<QString>("handler");
    QTest::addColumn<QString>("type");
    QTest::addColumn<QString>("role");
    QTest::addColumn<QJsonValue>("advertised");
    QTest::addColumn<QStringList>("expected");
    const QJsonValue missing(QJsonValue::Undefined);
    QTest::newRow("legacy-generalist-implementer") << QString("implementer") << QString("implement")
        << QString("agent") << QString{} << missing << QStringList{"resume"};
    QTest::newRow("legacy-generalist-repair") << QString("repair_1") << QString("repair.v1")
        << QString("agent") << QString{} << missing << QStringList{"resume"};
    QTest::newRow("legacy-deterministic-delivery") << QString("delivery_bundle") << QString("delivery")
        << QString("delivery") << QString("deterministic") << missing << QStringList{"retry"};
    QTest::newRow("explicitly-no-actions") << QString("investigate") << QString("analysis")
        << QString("agent") << QString{} << QJsonValue(QJsonArray{}) << QStringList{};
    QTest::newRow("advertised-resume") << QString("investigate") << QString("analysis")
        << QString("agent") << QString{} << QJsonValue(QJsonArray{"resume"}) << QStringList{"resume"};
}

void WorkbenchModelTests::advertisedAndLegacyRetryActions()
{
    QFETCH(QString, id); QFETCH(QString, handler); QFETCH(QString, type); QFETCH(QString, role);
    QFETCH(QJsonValue, advertised); QFETCH(QStringList, expected);
    auto snapshot = graph(5, "retryable");
    auto definition = snapshot["definition"].toObject();
    auto declared = definition["nodes"].toArray().first().toObject();
    declared["node_id"] = id; declared["node_type"] = type; declared["handler"] = handler;
    declared["agent_id"] = type == "agent" ? QJsonValue("generalist.v1") : QJsonValue(QJsonValue::Null);
    declared["settings"] = role.isEmpty() ? QJsonObject{} : QJsonObject{{"recovery_role", role}};
    definition["nodes"] = QJsonArray{declared};
    snapshot["definition"] = definition;
    auto node = snapshot["nodes"].toArray().first().toObject();
    node["node_id"] = id; node["node_type"] = type; node.insert("retry_actions", advertised);
    snapshot["nodes"] = QJsonArray{node};
    GraphModel model;
    model.setSnapshot(snapshot);
    QCOMPARE(model.get(0).value("actions").toStringList(), expected);
    QCOMPARE(model.hasResumeAction(), expected.contains("resume"));
    QCOMPARE(model.allowsRetry(id), advertised.isUndefined() || expected.contains("retry"));
}

void WorkbenchModelTests::graphApprovalActions()
{
    GraphModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    auto value = graph(9, "awaiting_human");
    auto definition = value.value("definition").toObject();
    auto nodeDefinition = definition.value("nodes").toArray().first().toObject();
    nodeDefinition.insert("handler", "execution_approval");
    nodeDefinition.insert("node_type", "human_gate");
    definition.insert("nodes", QJsonArray{nodeDefinition});
    value.insert("definition", definition);
    auto node = value.value("nodes").toArray().first().toObject();
    node.insert("node_type", "human_gate");
    value.insert("nodes", QJsonArray{node});
    model.setSnapshot(value);
    QCOMPARE(model.get(0).value("actions").toStringList(), (QStringList{"approve","reject"}));
    model.setRecoveryRequired(true);
    QVERIFY(model.get(0).value("actions").toStringList().isEmpty());
}

void WorkbenchModelTests::malformedAndLateSnapshotsAreAtomic()
{
    GraphModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    model.setSnapshot(graph(10, "succeeded"));
    model.setSnapshot(graph(9, "running"));
    QCOMPARE(model.revision(), 10);
    auto malformed = graph(11);
    const auto node = malformed.value("nodes").toArray().first();
    malformed.insert("nodes", QJsonArray{node,node});
    model.setSnapshot(malformed);
    QCOMPARE(model.revision(), 10);
    QCOMPARE(model.count(), 1);
    auto other = graph(1, "pending");
    other.insert("task_id", "33333333-3333-4333-8333-333333333333");
    model.clear();
    model.setSnapshot(other);
    QCOMPARE(model.revision(), 1);
    QCOMPARE(model.status(), "pending");
}

void WorkbenchModelTests::evidenceKeepsBundleIdentity()
{
    ArtifactModel artifacts;
    EvidenceModel evidence;
    QAbstractItemModelTester artifactTester(&artifacts, QAbstractItemModelTester::FailureReportingMode::QtTest);
    QAbstractItemModelTester evidenceTester(&evidence, QAbstractItemModelTester::FailureReportingMode::QtTest);
    const QJsonObject envelope{{"task_id",taskId},{"items",QJsonArray{bundle(),bundle("33333333-3333-4333-8333-333333333333")}}};
    artifacts.setSnapshot(envelope); evidence.setSnapshot(envelope);
    QCOMPARE(artifacts.count(), 2);
    QCOMPARE(evidence.count(), 2);
    QCOMPARE(evidence.get(0).value("evidenceId").toString(), "EV-1");
    QCOMPARE(evidence.get(1).value("evidenceId").toString(), "EV-1");
    QVERIFY(evidence.get(0).value("key") != evidence.get(1).value("key"));
    QCOMPARE(evidence.get(0).value("startLine").toInt(), 12);
    QCOMPARE(evidence.get(0).value("confidence").toDouble(), 0.95);
    QCOMPARE(artifacts.get(0).value("contentHash").toString(), QString(64,'b'));
    artifacts.clear(); evidence.clear();
    QCOMPARE(artifacts.count(), 0);
    QCOMPARE(evidence.count(), 0);
}

void WorkbenchModelTests::diffUsesCumulativeChangeSet()
{
    DiffModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    model.setSnapshot({{"task_id",taskId},{"patch","latest cumulative patch"},
        {"change_sets",QJsonArray{change("old.cpp"),change("current.cpp","33333333-3333-4333-8333-333333333333")}}});
    QCOMPARE(model.count(), 1);
    QCOMPARE(model.get(0).value("path").toString(), "current.cpp");
    QCOMPARE(model.changeSets().size(), 2);
    QCOMPARE(model.patch(), "latest cumulative patch");
    model.clear();
    QCOMPARE(model.count(), 0);
    QVERIFY(model.patch().isEmpty());
    QVERIFY(model.changeSets().isEmpty());
}

void WorkbenchModelTests::validationCommandsAndEmptyReport()
{
    ValidationModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    const auto report = artifact("ValidationReport", {{"status","passed"},{"tests_passed",7},
        {"tests_failed",0},{"commands",QJsonArray{QJsonObject{{"kind","test"},{"command_summary","ctest"},
        {"started_at",timestamp},{"finished_at",timestamp},{"exit_code",0},{"output_summary","7 tests passed"}}}}});
    model.setSnapshot({{"task_id",taskId},{"items",QJsonArray{report}},{"latest",report}});
    QCOMPARE(model.status(), "passed");
    QCOMPARE(model.count(), 1);
    QCOMPARE(model.get(0).value("command").toString(), "ctest");
    QCOMPARE(model.get(0).value("exitCode").toInt(), 0);
    QCOMPARE(model.reports().size(), 1);
    model.setSnapshot({{"task_id",taskId},{"items",QJsonArray{}},{"latest",QJsonValue{}}});
    QCOMPARE(model.count(), 0);
    QVERIFY(model.latest().isEmpty());
    QVERIFY(model.status().isEmpty());
    model.clear();
}

void WorkbenchModelTests::backendArtifactsMatchModels()
{
    // Generated from test_artifact_protocol.py::all_contents with create_artifact;
    // all seven envelopes passed the backend's strict Pydantic models and hashes.
    QFile fixture(QStringLiteral(FORGEPILOT_SOURCE_DIR "/client/tests/fixtures/mission_artifacts.json"));
    QVERIFY(fixture.open(QIODevice::ReadOnly));
    const auto envelope = QJsonDocument::fromJson(fixture.readAll()).object();
    QVERIFY(MissionProtocol::validArtifacts(envelope));
    ArtifactModel artifacts;
    EvidenceModel evidence;
    DiffModel diff;
    ValidationModel validation;
    QAbstractItemModelTester artifactTester(&artifacts, QAbstractItemModelTester::FailureReportingMode::QtTest);
    QAbstractItemModelTester evidenceTester(&evidence, QAbstractItemModelTester::FailureReportingMode::QtTest);
    QAbstractItemModelTester diffTester(&diff, QAbstractItemModelTester::FailureReportingMode::QtTest);
    QAbstractItemModelTester validationTester(&validation, QAbstractItemModelTester::FailureReportingMode::QtTest);
    artifacts.setSnapshot(envelope);
    evidence.setSnapshot(envelope);
    QCOMPARE(artifacts.count(), 7);
    QCOMPARE(evidence.count(), 1);
    for (const auto &entry : envelope.value("items").toArray()) {
        const auto value = entry.toObject();
        QVERIFY(MissionProtocol::validArtifact(value));
        if (value.value("artifact_type").toString() == "ChangeSet") {
            const auto patch = value.value("content").toObject().value("files").toArray().first().toObject().value("unified_diff");
            const QJsonObject response{{"task_id",envelope.value("task_id")},{"change_sets",QJsonArray{value}},{"patch",patch}};
            QVERIFY(MissionProtocol::validDiff(response));
            diff.setSnapshot(response);
        }
        if (value.value("artifact_type").toString() == "ValidationReport") {
            const QJsonObject response{{"task_id",envelope.value("task_id")},{"items",QJsonArray{value}},{"latest",value}};
            QVERIFY(MissionProtocol::validValidation(response));
            validation.setSnapshot(response);
        }
    }
    QCOMPARE(diff.count(), 1);
    QCOMPARE(validation.count(), 1);
    QCOMPARE(validation.status(), "passed");
}

void WorkbenchModelTests::signalsMayDestroyModels()
{
    QPointer<MissionListModel> missions = new MissionListModel;
    missions->upsert(mission(1));
    connect(missions, &QAbstractItemModel::dataChanged, this, [&] { delete missions.data(); });
    missions->upsert(mission(2));
    QVERIFY(missions.isNull());

    QPointer<GraphModel> graphModel = new GraphModel;
    connect(graphModel, &QAbstractItemModel::modelReset, this, [&] { delete graphModel.data(); });
    graphModel->setSnapshot(graph(1));
    QVERIFY(graphModel.isNull());

    auto *parent = new QObject;
    QPointer<MissionListModel> child = new MissionListModel(parent);
    connect(child, &QAbstractItemModel::rowsInserted, this, [parent] { delete parent; });
    child->upsert(mission(1));
    QVERIFY(child.isNull());

    QPointer<GraphModel> notificationModel = new GraphModel;
    connect(notificationModel, &GraphModel::snapshotChanged, this, [&] { delete notificationModel.data(); });
    notificationModel->setSnapshot(graph(1));
    QVERIFY(notificationModel.isNull());
}

void WorkbenchModelTests::reentrantMissionSwitchSerializesModelChanges()
{
    GraphModel model;
    QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
    auto other = graph(2, "retryable");
    other.insert("task_id", "33333333-3333-4333-8333-333333333333");
    bool switched = false;
    connect(&model, &QAbstractItemModel::modelReset, this, [&] {
        if (switched) return;
        switched = true;
        model.clear();
        model.setRecoveryRequired(true);
        model.setSnapshot(other);
    });
    model.setSnapshot(graph(20, "running"));
    QCOMPARE(model.taskId(), other.value("task_id").toString());
    QCOMPARE(model.revision(), 2);
    QCOMPARE(model.count(), 1);
    QVERIFY(model.recoveryRequired());
    QCOMPARE(model.get(0).value("actions").toStringList(), QStringList{"resume"});

    // A direct observer can clear again without nesting reset transactions.
    bool cleared = false;
    connect(&model, &QAbstractItemModel::dataChanged, this, [&] {
        if (!cleared) { cleared = true; model.clear(); }
    });
    other.insert("current_revision", 3);
    other.insert("status", "failed");
    auto node = other.value("nodes").toArray().first().toObject();
    node.insert("status", "failed");
    other.insert("nodes", QJsonArray{node});
    model.setSnapshot(other);
    QVERIFY(cleared);
    QCOMPARE(model.count(), 0);
    QVERIFY(model.taskId().isEmpty());
    QCOMPARE(model.revision(), 0);
}

QTEST_GUILESS_MAIN(WorkbenchModelTests)
#include "tst_workbench_models.moc"
