#include "TaskController.h"
#include "ConversationController.h"
#include "McpController.h"
#include "ClientUi.h"
#include <QCommandLineParser>
#include <QGuiApplication>
#include <QQmlApplicationEngine>

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
    ConversationController conversations(&controller);
    McpController mcp(&controller);
    QQmlApplicationEngine engine;
    engine.setInitialProperties({{"controller", QVariant::fromValue(&controller)},{"conversations",QVariant::fromValue(&conversations)},{"mcp",QVariant::fromValue(&mcp)}});
    QObject::connect(&engine, &QQmlApplicationEngine::objectCreationFailed, &app, [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.loadFromModule("ForgePilot", "Main");
    return app.exec();
}
