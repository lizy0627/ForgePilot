#pragma once
#include "AgentApiClient.h"
#include <QPointer>
#include <functional>

// Owns one explicit approval submission, never retries a user's decision.
class ApprovalController : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool available READ available NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QVariantMap review READ review NOTIFY changed)
public:
    explicit ApprovalController(AgentApiClient *api, QObject *parent = nullptr);
    ~ApprovalController() override;
    bool busy() const { return m_request != 0; }
    bool available() const;
    QString error() const { return m_error; }
    QVariantMap review() const { return m_mission.value("review").toObject().toVariantMap(); }
    void setMission(const QJsonObject &mission);
    void setEnabled(bool enabled);
    void clear();
    Q_INVOKABLE void approve();
    Q_INVOKABLE void reject();
signals:
    void changed();
    void snapshotReceived(const QJsonObject &mission);
    void refreshRequired();
private:
    void submit(const QString &decision);
    std::function<bool()> continuation();
    QPointer<AgentApiClient> m_api;
    QJsonObject m_mission;
    quint64 m_request = 0;
    quint64 m_generation = 0;
    bool m_enabled = false, m_stale = false;
    QString m_error;
};
