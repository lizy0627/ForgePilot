#include "AgentApiClient.h"
#include "workbench/MissionController.h"
#include <QFile>
#include <QJsonDocument>
#include <QtTest>

class MissionBackendTests : public QObject {
    Q_OBJECT
private slots:
    void productionHttpLifecycle() {
        const auto url = QUrl(qEnvironmentVariable("FORGEPILOT_INTEGRATION_URL"));
        const auto repository = qEnvironmentVariable("FORGEPILOT_INTEGRATION_REPOSITORY");
        QVERIFY(url.isValid()); QVERIFY(!repository.isEmpty());
        MissionController controller; controller.setBaseUrl(url);
        controller.createMission({{"repository_path", repository}, {"request", "Fix the worker"}, {"task_type", "bug_fix"}});
        QTRY_VERIFY_WITH_TIMEOUT(controller.approval()->available(), 15000);
        QVERIFY2(controller.error().isEmpty(), qPrintable(controller.error()));
        QCOMPARE(controller.status(), "awaiting_human"); QCOMPARE(controller.graph()->rowCount(), 3);
        QCOMPARE(controller.artifacts()->rowCount(), 2); QVERIFY(controller.synchronized());
        // Observe durable replay independently: the controller may legitimately
        // compensate every in-flight event with an authoritative HTTP snapshot.
        SseClient stream; QVERIFY(stream.setBaseUrl(url));
        QSignalSpy events(&stream, &SseClient::eventApplied);
        stream.connectToMission(controller.taskId());
        QTRY_VERIFY_WITH_TIMEOUT(!events.isEmpty(), 10000);
        const auto initial = QJsonObject::fromVariantMap(controller.snapshot());
        const auto review = initial.value("review").toObject();
        const QJsonObject approved{{"expected_revision", initial.value("current_revision")}, {"approval_type", "execution"},
            {"task_spec_hash", review.value("task_spec_hash")}, {"plan_hash", review.value("plan_hash")},
            {"scope_hash", review.value("scope_hash")}, {"decision", "approved"}};
        AgentApiClient api; QVERIFY(api.setBaseUrl(url)); QSignalSpy errors(&api, &AgentApiClient::requestError);
        auto wrongHash = approved; wrongHash["scope_hash"] = QString(64, '0'); api.approveMission(controller.taskId(), wrongHash);
        QTRY_COMPARE(errors.size(), 1); QCOMPARE(errors[0][3].toInt(), 409);
        QCOMPARE(errors[0][4].toString(), "mission_approval_conflict");
        QCOMPARE(controller.status(), "awaiting_human");
        controller.approval()->approve();
        QTRY_COMPARE_WITH_TIMEOUT(controller.status(), QString("succeeded"), 15000);
        QTRY_VERIFY_WITH_TIMEOUT(controller.synchronized(), 15000);
        QVERIFY2(controller.error().isEmpty(), qPrintable(controller.error()));
        QCOMPARE(controller.graph()->status(), "succeeded"); QCOMPARE(controller.graph()->rowCount(), 3);
        QCOMPARE(controller.graph()->revision(), controller.revision());
        QCOMPARE(controller.artifacts()->rowCount(), 2); QCOMPARE(controller.diff()->patch(), QString{});
        QCOMPARE(controller.validation()->rowCount(), 0); QVERIFY(controller.validation()->latest().isEmpty());
        QVERIFY(!controller.approval()->available()); QVERIFY(controller.revision() > initial.value("current_revision").toInteger());
        // EOF asks the coordinator for a fresh snapshot; this standalone stream
        // only verifies real durable replay, while controller/unit tests cover
        // snapshot adoption and terminal acknowledgement.
        QVERIFY(!events.isEmpty());
        stream.disconnect();
        QSignalSpy artifact(&controller, &MissionController::artifactLoaded);
        controller.loadArtifact(review.value("task_spec_artifact_id").toString());
        QTRY_COMPARE(artifact.size(), 1); QCOMPARE(artifact[0][0].toMap().value("artifact_type").toString(), "TaskSpec");
        api.approveMission(controller.taskId(), approved);
        QTRY_COMPARE(errors.size(), 2); QCOMPARE(errors[1][3].toInt(), 409);
        QCOMPARE(errors[1][4].toString(), "mission_revision_conflict");
        QFile evidence(qEnvironmentVariable("FORGEPILOT_INTEGRATION_OUTPUT") + "/mission-qt-snapshot.json");
        QVERIFY(evidence.open(QIODevice::WriteOnly));
        evidence.write(QJsonDocument(QJsonObject::fromVariantMap(controller.snapshot())).toJson());
        controller.clearSelection(); QCOMPARE(controller.graph()->rowCount(), 0);
    }
};
QTEST_GUILESS_MAIN(MissionBackendTests)
#include "tst_mission_backend.moc"
