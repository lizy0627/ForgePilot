#include "TaskController.h"
#include "ConversationController.h"
#include "McpController.h"
#include "RagController.h"
#include "RuntimeConfigController.h"
#include "ClientUi.h"
#include "workbench/MissionController.h"
#include <QCommandLineParser>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QTimer>

static_assert(_MSVC_LANG >= 202002L, "C++20 is required");
static_assert(sizeof(void*) == 8, "x64 is required");

int main(int argc, char* argv[])
{
    QGuiApplication app(argc, argv);
    app.setApplicationName("ForgePilot");
    configureClientUi(app);
    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption({"server", "本机后端 HTTP 地址", "url", "http://127.0.0.1:8765"});
    parser.addOption({"runtime-config", "本机启动器配置路径", "path"});
    parser.addOption({"configure", "打开运行配置向导"});
    parser.addOption({"self-test", "离线验证发布包中的 Qt/QML 客户端可加载"});
    parser.process(app);
    TaskController controller;
    controller.setBaseUrl(QUrl(parser.value("server")));
    MissionController missions;
    missions.setBaseUrl(controller.baseUrl());
    QObject::connect(&controller, &TaskController::baseUrlChanged, &missions,
                     [&] { missions.setBaseUrl(controller.baseUrl()); });
    ConversationController conversations(&controller);
    McpController mcp(&controller);
    RagController rag;
    rag.setBaseUrl(controller.baseUrl());
    conversations.setUseRag(rag.useForTasks());
    QObject::connect(&controller, &TaskController::baseUrlChanged, &rag,
                     [&] { rag.setBaseUrl(controller.baseUrl()); });
    QObject::connect(&rag, &RagController::useForTasksChanged, &conversations,
                     [&] { conversations.setUseRag(rag.useForTasks()); });
    RuntimeConfigController runtimeConfig;
    QObject::connect(&runtimeConfig, &RuntimeConfigController::restartAccepted, &app, &QCoreApplication::quit);
    if (!parser.isSet("self-test")) runtimeConfig.configureHelper(parser.value("runtime-config"));
    QQmlApplicationEngine engine;
    engine.setInitialProperties({{"runtimeConfig", parser.isSet("self-test") ? QVariant() : QVariant::fromValue(&runtimeConfig)}, {"showRuntimeConfiguration", parser.isSet("configure")}, {"controller", QVariant::fromValue(&controller)},{"conversations",QVariant::fromValue(&conversations)},{"mcp",QVariant::fromValue(&mcp)},{"rag",QVariant::fromValue(&rag)},{"missions",QVariant::fromValue(&missions)}});
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app, [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.loadFromModule("ForgePilot", "Main");
    if (parser.isSet("self-test")) {
        QTimer::singleShot(5000, &app, [&] { app.exit(engine.rootObjects().isEmpty() ? 1 : 0); });
    }
    return app.exec();
}
