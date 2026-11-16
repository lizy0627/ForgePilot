#pragma once

#include <QObject>
#include <QProcess>
#include <QJsonValue>
#include <QTimer>
#include <QVariantMap>

// Configuration remains a launcher responsibility. This controller only owns
// one bounded, asynchronous JSON exchange with its short-lived helper.
class RuntimeConfigController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantMap fields READ fields NOTIFY fieldsChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool loaded READ loaded NOTIFY changed)
    Q_PROPERTY(bool available READ available NOTIFY changed)
    Q_PROPERTY(bool configured READ configured NOTIFY changed)
    Q_PROPERTY(bool wizardRequired READ wizardRequired NOTIFY changed)
    Q_PROPERTY(bool hasApiKey READ hasApiKey NOTIFY changed)
    Q_PROPERTY(bool hasEmbeddingKey READ hasEmbeddingKey NOTIFY changed)
    Q_PROPERTY(bool restartRequired READ restartRequired NOTIFY changed)
    Q_PROPERTY(bool restartAllowed READ restartAllowed NOTIFY changed)
    Q_PROPERTY(int activeCount READ activeCount NOTIFY changed)
    Q_PROPERTY(QString message READ message NOTIFY changed)
    Q_PROPERTY(QString savedAt READ savedAt NOTIFY changed)
    Q_PROPERTY(QVariantList errors READ errors NOTIFY changed)
    Q_PROPERTY(QVariantList warnings READ warnings NOTIFY changed)
public:
    explicit RuntimeConfigController(QObject *parent = nullptr);
    ~RuntimeConfigController() override;
    void configureHelper(const QString &configPath = {});
    void setHelperCommand(const QString &program, const QStringList &arguments, const QString &configPath);
    QVariantMap fields() const { return m_fields; }
    bool busy() const { return !m_operation.isEmpty(); }
    bool loaded() const { return m_loaded; }
    bool available() const { return !m_program.isEmpty(); }
    bool configured() const { return m_configured; }
    bool wizardRequired() const { return m_loaded && !m_configured; }
    bool hasApiKey() const { return m_hasApiKey; }
    bool hasEmbeddingKey() const { return m_hasEmbeddingKey; }
    bool restartRequired() const { return m_restartRequired; }
    bool restartAllowed() const { return m_restartAllowed && !busy(); }
    int activeCount() const { return m_activeCount; }
    QString message() const { return m_message; }
    QString savedAt() const { return m_savedAt; }
    QVariantList errors() const { return m_errors; }
    QVariantList warnings() const { return m_warnings; }
    Q_INVOKABLE void load();
    Q_INVOKABLE void save(const QVariantMap &fields, const QString &apiKey = {}, const QString &embeddingKey = {});
    Q_INVOKABLE void check(const QVariantMap &fields, const QString &apiKey = {}, const QString &embeddingKey = {});
    Q_INVOKABLE void restart();
    Q_INVOKABLE void cancel();
signals:
    void changed();
    void fieldsChanged();
    void secretsConsumed();
    void operationFinished(const QString &action, bool ok);
    void restartAccepted();
private:
    void request(const QString &action, const QVariantMap &fields = {}, const QString &apiKey = {}, const QString &embeddingKey = {});
    void complete();
    void fail(const QString &message);
    QProcess m_process;
    QTimer m_timer;
    QString m_program, m_configPath, m_operation, m_message;
    QString m_savedAt;
    QJsonValue m_configRevision;
    QStringList m_arguments;
    QVariantMap m_fields;
    QVariantList m_errors, m_warnings;
    QByteArray m_output;
    bool m_loaded = false, m_configured = false, m_hasApiKey = false, m_hasEmbeddingKey = false;
    bool m_restartRequired = false, m_restartAllowed = false;
    int m_activeCount = -1;
};
