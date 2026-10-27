#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

// The last ChangeSet is the cumulative backend-verified diff; earlier sets are provenance.
class DiffModel : public WorkbenchListModel {
    Q_OBJECT
    Q_PROPERTY(QString patch READ patch NOTIFY snapshotChanged)
    Q_PROPERTY(QVariantList changeSets READ changeSets NOTIFY snapshotChanged)
public:
    enum Role { PathRole = RecordRole + 1, ChangeTypeRole, UnifiedDiffRole, BeforeHashRole,
                AfterHashRole, PlanStepsRole, CriteriaRole, TestChangeRole, ArtifactIdRole };
    explicit DiffModel(QObject *parent = nullptr);
    void setSnapshot(const QJsonObject &diff);
    void clear() override;
    QString patch() const { return m_patch; }
    QVariantList changeSets() const { return m_changeSets; }
signals:
    void snapshotChanged();
private:
    QString m_patch;
    QVariantList m_changeSets;
};
