#include "RuntimeConfigController.h"
#include <QCoreApplication>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTemporaryDir>
#include <QThread>
#include <QtTest>

namespace {
int fixture()
{
    QFile input;
    if (!input.open(stdin, QIODevice::ReadOnly)) return 4;
    const auto request = QJsonDocument::fromJson(input.readAll()).object();
    const auto action = request.value("action").toString();
    const auto arguments = QCoreApplication::arguments();
    if (arguments.contains("pipe-only-secret") || arguments.join(' ').contains("embedding-private")) return 5;
    if (request.value("fields").toObject().value("fixture_mode") == "malformed") {
        QFile output; output.open(stdout, QIODevice::WriteOnly); output.write("not-json"); output.flush(); return 0;
    }
    if (action != "load" && request.value("config_revision") != "revision-1") return 6;
    QThread::msleep(action == "check" ? 250 : 80);
    const auto fields = request.value("fields").toObject();
    const bool bad = fields.value("fixture_mode") == "failure";
    const bool configured = !arguments.last().endsWith("new.json");
    QJsonObject response{{"ok", !bad}, {"configured", configured || action == "save" && !bad},
        {"has_api_key", configured || action == "save" && !bad}, {"has_embedding_key", false},
        {"restart_required", action == "save" && !bad}, {"restart_allowed", action == "save" && !bad},
        {"config_revision", "revision-1"}, {"active_count", 0},
        {"notice", bad ? "摘要模型认证失败" : action == "save" ? "已保存，下次受控重启后生效。" : "配置已读取。"},
        {"errors", bad ? QJsonArray{QJsonObject{{"purpose", "summary"}, {"code", "llm_auth"}, {"message", "认证失败"}}} : QJsonArray{}}};
    if (action == "load") response["fields"] = QJsonObject{{"base_url", "http://127.0.0.1:1/v1"},
        {"port", 8765}, {"task_model", "fixture-task"}, {"review_model", "fixture-review"}};
    if (action == "save" && !bad) {
        if (request.value("api_key") != "pipe-only-secret") return 7;
        response["fields"] = fields; response["saved_at"] = "2026-11-16T10:00:00Z";
    }
    QFile output;
    if (!output.open(stdout, QIODevice::WriteOnly)) return 8;
    output.write(QJsonDocument(response).toJson(QJsonDocument::Compact)); output.flush(); return 0;
}
void helper(RuntimeConfigController &controller, const QString &path)
{
    controller.setHelperCommand(QCoreApplication::applicationFilePath(), {"--config-helper-fixture"}, path);
}
}

class RuntimeConfigTests : public QObject {
    Q_OBJECT
private slots:
    void firstRunLoadsWithoutBackendAndSavesViaPrivatePipe()
    {
        QTemporaryDir directory; QVERIFY(directory.isValid());
        RuntimeConfigController controller; helper(controller, directory.filePath("new.json"));
        QSignalSpy finished(&controller, &RuntimeConfigController::operationFinished);
        QSignalSpy consumed(&controller, &RuntimeConfigController::secretsConsumed);
        controller.load(); QVERIFY(controller.busy());
        QTRY_COMPARE(finished.size(), 1);
        QVERIFY(controller.loaded()); QVERIFY(controller.wizardRequired()); QVERIFY(!controller.configured());
        QCOMPARE(controller.fields().value("task_model").toString(), "fixture-task");
        controller.save(controller.fields(), "pipe-only-secret", "embedding-private");
        QVERIFY(controller.busy()); QTRY_COMPARE(consumed.size(), 2);
        QTRY_COMPARE(finished.size(), 2);
        QCOMPARE(finished.last().at(0).toString(), "save"); QVERIFY(finished.last().at(1).toBool());
        QVERIFY(controller.configured()); QVERIFY(controller.hasApiKey()); QVERIFY(!controller.wizardRequired());
        QVERIFY(controller.restartRequired()); QVERIFY(controller.restartAllowed());
        QVERIFY(controller.message().contains("重启")); QVERIFY(!controller.savedAt().isEmpty());
        QVERIFY(!QFile::exists(directory.filePath("new.json")));
        QVERIFY(!controller.fields().contains("api_key")); QVERIFY(!controller.message().contains("pipe-only-secret"));
    }
    void connectionErrorsIdentifyPurposeAndEventLoopKeepsRunning()
    {
        QTemporaryDir directory; RuntimeConfigController controller;
        helper(controller, directory.filePath("existing.json"));
        QSignalSpy finished(&controller, &RuntimeConfigController::operationFinished);
        controller.load(); QTRY_COMPARE(finished.size(), 1);
        auto fields = controller.fields(); fields["fixture_mode"] = "failure";
        int ticks = 0; QTimer timer; connect(&timer, &QTimer::timeout, this, [&] { ++ticks; }); timer.start(15);
        controller.check(fields, "pipe-only-secret");
        QVERIFY(controller.busy()); controller.check(fields, "must-not-start-second-request");
        QTRY_COMPARE(finished.size(), 2);
        QVERIFY(ticks > 3); QVERIFY(!finished.last().at(1).toBool());
        QCOMPARE(controller.errors().first().toMap().value("purpose").toString(), "summary");
        QCOMPARE(controller.fields().value("task_model").toString(), "fixture-task");
        QVERIFY(controller.configured());
    }
    void malformedReplyAndCancellationAreVisibleWithoutLeakingOutput()
    {
        QTemporaryDir directory; RuntimeConfigController controller;
        helper(controller, directory.filePath("existing.json"));
        QSignalSpy finished(&controller, &RuntimeConfigController::operationFinished);
        controller.load(); QTRY_COMPARE(finished.size(), 1);
        auto fields = controller.fields(); fields["fixture_mode"] = "malformed";
        controller.save(fields, "pipe-only-secret"); QTRY_COMPARE(finished.size(), 2);
        QVERIFY(!finished.last().at(1).toBool()); QVERIFY(controller.message().contains("响应无效"));
        QVERIFY(controller.configured());
        controller.check(controller.fields(), "pipe-only-secret"); QVERIFY(controller.busy()); controller.cancel();
        QTRY_COMPARE(finished.size(), 3); QVERIFY(!finished.last().at(1).toBool());
        QVERIFY(!controller.busy()); QVERIFY(controller.message().contains("已取消"));
    }
    void missingHelperLeavesWizardAvailable()
    {
        QTemporaryDir directory; RuntimeConfigController controller;
        controller.setHelperCommand(directory.filePath("missing.exe"), {}, directory.filePath("config.json"));
        QSignalSpy finished(&controller, &RuntimeConfigController::operationFinished);
        controller.load(); QTRY_COMPARE(finished.size(), 1);
        QVERIFY(!controller.busy()); QVERIFY(controller.wizardRequired());
        QVERIFY(controller.message().contains("无法启动"));
    }
    void restartUsesFreshConfigurationRevision()
    {
        QTemporaryDir directory; RuntimeConfigController controller;
        helper(controller, directory.filePath("existing.json"));
        QSignalSpy finished(&controller, &RuntimeConfigController::operationFinished);
        QSignalSpy restart(&controller, &RuntimeConfigController::restartAccepted);
        controller.load(); QTRY_COMPARE(finished.size(), 1);
        controller.save(controller.fields(), "pipe-only-secret"); QTRY_COMPARE(finished.size(), 2);
        controller.restart(); QTRY_COMPARE(restart.size(), 1); QTRY_COMPARE(finished.size(), 3);
        QVERIFY(finished.last().at(1).toBool());
    }
};

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    if (QCoreApplication::arguments().contains("--config-helper-fixture")) return fixture();
    RuntimeConfigTests tests; return QTest::qExec(&tests, argc, argv);
}

#include "tst_runtime_config.moc"
