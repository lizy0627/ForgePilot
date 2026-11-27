#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

class GraphModel : public WorkbenchListModel {
    Q_OBJECT
    Q_PROPERTY(QString taskId READ taskId NOTIFY snapshotChanged)
    Q_PROPERTY(qint64 revision READ revision NOTIFY snapshotChanged)
    Q_PROPERTY(QString status READ status NOTIFY snapshotChanged)
    Q_PROPERTY(QStringList currentAgents READ currentAgents NOTIFY snapshotChanged)
    Q_PROPERTY(bool hasResumeAction READ hasResumeAction NOTIFY snapshotChanged)
    Q_PROPERTY(bool recoveryRequired READ recoveryRequired WRITE setRecoveryRequired NOTIFY snapshotChanged)
public:
    enum Role { NodeIdRole = RecordRole + 1, NodeTypeRole, StatusRole, AttemptRole,
                DependenciesRole, AgentIdRole, CurrentAgentRole, MetricsRole, ActionsRole, ResultRole };
    explicit GraphModel(QObject *parent = nullptr);
    void setSnapshot(const QJsonObject &graph);
    void clear() override;
    QString taskId() const { return m_taskId; }
    qint64 revision() const { return m_revision; }
    QString status() const { return m_status; }
    QStringList currentAgents() const { return m_currentAgents; }
    bool hasResumeAction() const { return m_hasResumeAction; }
    bool allowsRetry(const QString &nodeId) const;
    bool recoveryRequired() const { return m_recoveryRequired; }
    void setRecoveryRequired(bool required);
signals:
    void snapshotChanged();
private:
    bool setSnapshotNow(const QJsonObject &graph);
    QJsonObject m_snapshot;
    QString m_taskId, m_status;
    QStringList m_currentAgents;
    qint64 m_revision = 0;
    bool m_recoveryRequired = false;
    bool m_hasResumeAction = false;
};
