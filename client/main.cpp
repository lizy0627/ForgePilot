#include "TaskController.h"
#include "BackendConnection.h"
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
    BackendConnection connection;
    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption({"server", "本机后端 HTTP 地址", "url", connection.baseUrl().toString()});
    parser.addOption({"runtime-config", "本机启动器配置路径", "path"});
    parser.addOption({"configure", "打开运行配置向导"});
    parser.addOption({"self-test", "离线验证发布包中的 Qt/QML 客户端可加载"});
    parser.process(app);
    connection.configureBackend(parser.value("server"));
    TaskController controller(connection);
    MissionController missions;
    missions.setBaseUrl(connection.baseUrl());
    QObject::connect(&connection, &BackendConnection::baseUrlChanged, &missions,
                     [&] { missions.setBaseUrl(connection.baseUrl()); });
    ConversationController conversations(connection, &controller);
    McpController mcp(connection);
    RagController rag;
    rag.setBaseUrl(connection.baseUrl());
    conversations.setUseRag(rag.useForTasks());
    QObject::connect(&connection, &BackendConnection::baseUrlChanged, &rag,
                     [&] { rag.setBaseUrl(connection.baseUrl()); });
    QObject::connect(&rag, &RagController::useForTasksChanged, &conversations,
                     [&] { conversations.setUseRag(rag.useForTasks()); });
    RuntimeConfigController runtimeConfig;
    QObject::connect(&runtimeConfig, &RuntimeConfigController::restartAccepted, &app, &QCoreApplication::quit);
    if (!parser.isSet("self-test")) runtimeConfig.configureHelper(parser.value("runtime-config"));
    QQmlApplicationEngine engine;
    engine.setInitialProperties({
        {"backendConnection", QVariant::fromValue(&connection)},
        {"controller", QVariant::fromValue(&controller)},
        {"conversations", QVariant::fromValue(&conversations)},
        {"mcp", QVariant::fromValue(&mcp)},
        {"rag", QVariant::fromValue(&rag)},
        {"missions", QVariant::fromValue(&missions)},
        {"runtimeConfig", parser.isSet("self-test") ? QVariant() : QVariant::fromValue(&runtimeConfig)},
        {"showRuntimeConfiguration", parser.isSet("configure")}
    });
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app, [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.loadFromModule("ForgePilot", "Main");
    if (parser.isSet("self-test")) {
        QTimer::singleShot(5000, &app, [&] { app.exit(engine.rootObjects().isEmpty() ? 1 : 0); });
    }
    return app.exec();
}
