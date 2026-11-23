#include "RuntimeConfigController.h"

#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPointer>

namespace {
constexpr qsizetype MaxHelperBytes = 512 * 1024;
QString configurationPath(const QString &root)
{
    const auto identity = QCryptographicHash::hash(QDir::toNativeSeparators(root).toLower().toUtf8(),
                                                  QCryptographicHash::Sha256).toHex().left(16);
    return QDir(qEnvironmentVariable("LOCALAPPDATA")).filePath(
        "ForgePilot/launcher/" + QString::fromLatin1(identity) + "/config.json");
}
}

RuntimeConfigController::RuntimeConfigController(QObject *parent) : QObject(parent)
{
    m_timer.setSingleShot(true);
    connect(&m_timer, &QTimer::timeout, this, [this] {
        fail("配置操作超时，请重新读取配置确认结果后重试。");
    });
    connect(&m_process, &QProcess::readyReadStandardOutput, this, [this] {
        const auto data = m_process.readAllStandardOutput();
        if (!busy()) return;
        if (m_output.size() + data.size() > MaxHelperBytes) {
            fail("配置辅助程序响应过大，请检查安装后重试。");
            return;
        }
        m_output += data;
    });
    // Never forward raw stderr: even a failed connection may contain credentials.
    connect(&m_process, &QProcess::readyReadStandardError, this, [this] { m_process.readAllStandardError(); });
    connect(&m_process, &QProcess::errorOccurred, this, [this](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart && busy()) fail("配置辅助程序无法启动，请检查开发环境或发布包。");
    });
    connect(&m_process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
        [this](int, QProcess::ExitStatus status) {
            if (!busy()) return;
            if (status != QProcess::NormalExit) fail("配置辅助程序已中断；请重新读取当前配置。");
            else complete();
        });
#ifdef Q_OS_WIN
    m_process.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *arguments) {
        arguments->flags |= 0x08000000; // CREATE_NO_WINDOW
    });
#endif
}

RuntimeConfigController::~RuntimeConfigController()
{
    m_process.disconnect(this);
    if (m_process.state() != QProcess::NotRunning) { m_process.kill(); m_process.waitForFinished(1000); }
}

void RuntimeConfigController::setHelperCommand(const QString &program, const QStringList &arguments, const QString &configPath)
{
    if (busy()) return;
    m_program = program; m_arguments = arguments; m_configPath = configPath;
    emit changed();
}

void RuntimeConfigController::configureHelper(const QString &configPath)
{
    QDir directory(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 6; ++depth) {
        const auto root = directory.absolutePath();
        const auto frozen = directory.filePath("bin/ForgePilotAgent.exe");
        if (QFileInfo::exists(frozen)) {
            setHelperCommand(frozen, {}, configPath.isEmpty() ? configurationPath(root) : configPath);
            return;
        }
        const auto script = directory.filePath("agent/desktop.py");
        const auto python = directory.filePath("agent.venv/Scripts/python.exe");
        if (QFileInfo::exists(script) && QFileInfo::exists(python)) {
            setHelperCommand(python, {"-X", "utf8", "-I", script},
                             configPath.isEmpty() ? configurationPath(root) : configPath);
            return;
        }
        if (!directory.cdUp()) break;
    }
    m_loaded = true; m_configured = false; m_message = "未找到配置辅助入口，请检查开发环境或重新解压发布包。";
    emit changed();
}

void RuntimeConfigController::load() { request("load"); }
void RuntimeConfigController::save(const QVariantMap &fields, const QString &key, const QString &embeddingKey)
{ request("save", fields, key, embeddingKey); }
void RuntimeConfigController::check(const QVariantMap &fields, const QString &key, const QString &embeddingKey)
{ request("check", fields, key, embeddingKey); }
void RuntimeConfigController::restart() { request("restart"); }

void RuntimeConfigController::cancel()
{
    if (busy()) fail("操作已取消。若保存已开始，请重新读取配置确认最终结果。");
}

void RuntimeConfigController::request(const QString &action, const QVariantMap &fields,
                                      const QString &apiKey, const QString &embeddingKey)
{
    if (busy()) return;
    if (m_process.state() != QProcess::NotRunning) {
        m_message = "正在结束上一次配置操作，请稍后重试。"; emit changed(); emit secretsConsumed(); return;
    }
    if (!available()) configureHelper(m_configPath);
    if (!available()) { m_loaded = true; emit changed(); emit secretsConsumed(); return; }
    QJsonObject body{{"action", action}};
    if (action != "load") body["config_revision"] = m_configRevision;
    if (action == "save" || action == "check") {
        body["fields"] = QJsonObject::fromVariantMap(fields);
        body["api_key"] = apiKey;
        body["embedding_api_key"] = embeddingKey;
    }
    QByteArray input = QJsonDocument(body).toJson(QJsonDocument::Compact) + '\n';
    if (input.size() > MaxHelperBytes) { m_message = "配置内容超过上限，请缩短路径或模型名称。"; emit changed(); emit secretsConsumed(); return; }
    m_operation = action; m_output.clear(); m_message.clear(); m_errors.clear(); emit changed();
    auto arguments = m_arguments;
    arguments << "config-helper" << "--config" << m_configPath;
    m_process.setProgram(m_program); m_process.setArguments(arguments);
    // Passwords exist only in the private stdin pipe, never in the command line.
    m_process.start(QIODevice::ReadWrite);
    if (!busy()) { input.fill('\0'); emit secretsConsumed(); return; }
    m_process.write(input); m_process.closeWriteChannel(); input.fill('\0'); input.clear();
    emit secretsConsumed();
    // A cold start can take 85 seconds, followed by up to 43 seconds of stop cleanup.
    m_timer.start(action == "check" || action == "restart" ? 180000 : 30000);
}

void RuntimeConfigController::fail(const QString &message)
{
    const auto action = m_operation;
    m_operation.clear(); m_output.clear(); m_timer.stop();
    if (m_process.state() != QProcess::NotRunning) m_process.kill();
    if (action == "load") { m_loaded = true; m_configured = false; }
    m_message = message; emit changed();
    emit operationFinished(action, false);
}

void RuntimeConfigController::complete()
{
    m_timer.stop();
    const auto action = m_operation;
    m_output += m_process.readAllStandardOutput();
    QJsonParseError error;
    const auto document = QJsonDocument::fromJson(m_output, &error);
    if (m_output.size() > MaxHelperBytes || error.error != QJsonParseError::NoError || !document.isObject()
        || !document.object().value("ok").isBool()) {
        fail("配置辅助程序响应无效，请重新读取当前配置。");
        return;
    }
    const auto response = document.object(); m_output.clear(); m_operation.clear();
    if (response.contains("config_revision")) m_configRevision = response.value("config_revision");
    if (response.value("saved_at").isString()) m_savedAt = response.value("saved_at").toString();
    if (response.value("fields").isObject()) {
        m_fields = response.value("fields").toObject().toVariantMap();
        m_fields.remove("api_key"); m_fields.remove("embedding_api_key");
        emit fieldsChanged();
    }
    if (response.value("configured").isBool()) m_configured = response.value("configured").toBool();
    if (response.value("has_api_key").isBool()) m_hasApiKey = response.value("has_api_key").toBool();
    if (response.value("has_embedding_key").isBool()) m_hasEmbeddingKey = response.value("has_embedding_key").toBool();
    if (response.value("restart_required").isBool()) m_restartRequired = response.value("restart_required").toBool();
    if (response.value("restart_allowed").isBool()) m_restartAllowed = response.value("restart_allowed").toBool();
    if (response.value("active_count").isDouble()) m_activeCount = response.value("active_count").toInt(-1);
    m_message = response.value("notice").toString(); m_errors = response.value("errors").toArray().toVariantList();
    m_warnings = response.value("warnings").toArray().toVariantList();
    if (action == "load" || action == "save") m_loaded = true;
    const auto ok = response.value("ok").toBool();
    QPointer<RuntimeConfigController> guard(this);
    emit changed();
    if (!guard) return;
    emit operationFinished(action, ok);
    if (guard && action == "restart" && ok) emit restartAccepted();
}
