#include "TaskController.h"
#include "ConversationController.h"
#include "McpController.h"
#include "RagController.h"
#include "ClientUi.h"
#include "workbench/MissionController.h"
#include <QCommandLineParser>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>

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
    QObject::connect(&controller, &TaskController::repositoryPathChanged, &rag,
                     [&] { rag.setRepositoryPath(controller.repositoryPath()); });
    QObject::connect(&rag, &RagController::useForTasksChanged, &conversations,
                     [&] { conversations.setUseRag(rag.useForTasks()); });
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("missionController", &missions);
    engine.setInitialProperties({{"controller", QVariant::fromValue(&controller)},{"conversations",QVariant::fromValue(&conversations)},{"mcp",QVariant::fromValue(&mcp)},{"rag",QVariant::fromValue(&rag)}});
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app, [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.loadFromModule("ForgePilot", "Main");
    return app.exec();
}
