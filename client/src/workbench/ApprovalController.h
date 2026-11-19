#pragma once
#include "AgentApiClient.h"
#include <QPointer>
#include <functional>

struct MissionSnapshot;

// Owns one explicit approval submission, never retries a user's decision.
class ApprovalController : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool available READ available NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QVariantMap review READ review NOTIFY changed)
    Q_PROPERTY(QVariantMap scopeExpansion READ scopeExpansion NOTIFY changed)
    Q_PROPERTY(bool scopeAvailable READ scopeAvailable NOTIFY changed)
public:
    explicit ApprovalController(AgentApiClient *api, QObject *parent = nullptr);
    ~ApprovalController() override;
    bool busy() const { return m_request != 0; }
    bool available() const;
    QString error() const { return m_error; }
    QVariantMap review() const { return m_mission.value("review").toObject().toVariantMap(); }
    QVariantMap scopeExpansion() const { return m_scopeExpansion.toVariantMap(); }
    bool scopeAvailable() const;
    void setSnapshot(const MissionSnapshot &snapshot);
    void setEnabled(bool enabled);
    void clear();
    Q_INVOKABLE void approve();
    Q_INVOKABLE void reject();
    Q_INVOKABLE void decideScopeExpansion(const QVariantMap &reviewed, bool approved);
signals:
    void changed();
    void snapshotReceived(const QJsonObject &mission);
    void refreshRequired();
    void scopeExpansionApproved(const QString &taskId);
private:
    bool canDecide() const;
    void submitExecution(bool approved);
    void submit(const QJsonObject &review, const QString &type, bool approved);
    std::function<bool()> continuation();
    QPointer<AgentApiClient> m_api;
    QJsonObject m_mission, m_scopeExpansion;
    quint64 m_request = 0;
    quint64 m_generation = 0;
    bool m_enabled = false, m_stale = false;
    bool m_executionPending = false, m_approvingScope = false;
    QString m_error;
};
