#pragma once
#include "JsonHttp.h"
#include <QObject>
#include <QSet>
#include <QJsonArray>
#include <QVariantList>
#include <QVariantMap>
#include <optional>
class TaskController;

// Settings-only client. Draft properties contain no credentials; patches live only
// for the editor lifetime. Page and editor generations independently fence replies.
class McpController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList items READ items NOTIFY changed)
    Q_PROPERTY(bool loading READ loading NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool disabled READ disabled NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QString notice READ notice NOTIFY changed)
    Q_PROPERTY(bool editorOpen READ editorOpen NOTIFY editorChanged)
    Q_PROPERTY(bool editorBusy READ editorBusy NOTIFY editorChanged)
    Q_PROPERTY(QString state READ state NOTIFY editorChanged)
    Q_PROPERTY(QString editorError READ editorError NOTIFY editorChanged)
    Q_PROPERTY(QString editorNotice READ editorNotice NOTIFY editorChanged)
    Q_PROPERTY(QVariantMap draft READ draft NOTIFY editorChanged)
    Q_PROPERTY(QVariantList tools READ tools NOTIFY editorChanged)
    Q_PROPERTY(QString serverId READ serverId NOTIFY editorChanged)
    Q_PROPERTY(int kind READ kind NOTIFY editorChanged)
    Q_PROPERTY(bool canSave READ canSave NOTIFY editorChanged)
    Q_PROPERTY(bool uncertain READ uncertain NOTIFY editorChanged)
    Q_PROPERTY(bool canCloseEditor READ canCloseEditor NOTIFY editorChanged)
    Q_PROPERTY(bool pendingSave READ pendingSave NOTIFY editorChanged)
    Q_PROPERTY(bool existing READ existing NOTIFY editorChanged)
    Q_PROPERTY(QVariantMap credentialState READ credentialState NOTIFY editorChanged)
public:
    explicit McpController(TaskController *tasks);
    QVariantList items() const { return m_items; }
    bool loading() const { return m_loading; }
    bool busy() const { return m_busy; }
    bool disabled() const { return m_disabled; }
    QString error() const { return m_error; }
    QString notice() const { return m_notice; }
    bool editorOpen() const { return m_editorOpen; }
    bool editorBusy() const { return m_editorLoading || m_resolving || m_state=="probing" || m_state=="saving"; }
    QString state() const { return m_state; }
    QString editorError() const { return m_editorError; }
    QString editorNotice() const { return m_editorNotice; }
    QVariantMap draft() const { return m_draft.toVariantMap(); }
    QVariantList tools() const;
    QString serverId() const { return m_id; }
    int kind() const;
    bool canSave() const;
    bool uncertain() const { return m_uncertain; }
    bool canCloseEditor() const { return m_state!="saving" && !m_resolving; }
    bool pendingSave() const { return m_pending.has_value(); }
    bool existing() const { return m_revision>0; }
    QVariantMap credentialState() const;
    Q_INVOKABLE void open();
    Q_INVOKABLE void close();
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void edit(const QString &id = {});
    Q_INVOKABLE void closeEditor();
    Q_INVOKABLE void setKind(int kind);
    Q_INVOKABLE void update(const QString &field, const QVariant &value);
    Q_INVOKABLE void updateList(const QString &field, const QVariantList &values) { update(field, values); }
    Q_INVOKABLE void setSecret(const QString &key, const QString &value);
    Q_INVOKABLE void clearSecret(const QString &key);
    Q_INVOKABLE bool hasSecret(const QString &key) const;
    Q_INVOKABLE void selectTool(const QString &name, bool selected);
    Q_INVOKABLE void probe();
    Q_INVOKABLE void save();
    Q_INVOKABLE void resolveSave();
    Q_INVOKABLE void test(const QString &id, qint64 revision);
    Q_INVOKABLE void setEnabled(const QString &id, qint64 revision, bool enabled);
    Q_INVOKABLE void remove(const QString &id, qint64 revision);
    Q_INVOKABLE QString localPath(const QUrl &url) const { return url.toLocalFile(); }
    Q_INVOKABLE QString defaultDirectory(const QString &command) const;
    Q_INVOKABLE QString toolReason(const QString &reason) const;
signals:
    void changed();
    void editorChanged();
    void clearSensitiveInputs();
    void saved();
private:
    using Response = JsonHttp::Response;
    void request(bool editor, const QByteArray &method, const QString &path,
                 const QJsonObject &body, std::function<void(const Response &)> callback);
    void abort(bool editor);
    void invalidate();
    void loadDetail(const QJsonObject &body);
    void failEditor(const Response &response);
    void mutate(const QByteArray &method, const QString &path, const QJsonObject &body, const QString &notice);
    QString validation() const;
    static QString failure(const Response &response);
    static bool ok(const Response &response);
    struct PendingSave { QUrl backend; QString serverId; qint64 baseRevision; };
    std::optional<PendingSave> m_pending;
    bool m_resolving = false, m_hasDiscovered = false;
    TaskController *m_tasks;
    QNetworkAccessManager m_network;
    QSet<QNetworkReply *> m_pageReplies, m_editorReplies;
    quint64 m_generation = 0, m_editorGeneration = 0;
    bool m_open = false, m_loading = false, m_busy = false, m_disabled = true;
    bool m_editorOpen = false, m_editorLoading = false, m_uncertain = false, m_recoveryConflict = false;
    QVariantList m_items;
    QJsonObject m_preset, m_draft, m_original, m_secrets;
    QJsonArray m_tools;
    QString m_id, m_hash, m_state = "editing", m_error, m_notice, m_editorError, m_editorNotice;
    qint64 m_revision = 0;
};
