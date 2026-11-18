#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

// Command rows for the latest report, with immutable report history retained.
class ValidationModel : public WorkbenchListModel {
    Q_OBJECT
    Q_PROPERTY(QVariantMap latest READ latest NOTIFY snapshotChanged)
    Q_PROPERTY(QVariantList reports READ reports NOTIFY snapshotChanged)
    Q_PROPERTY(QString status READ status NOTIFY snapshotChanged)
public:
    enum Role { KindRole = RecordRole + 1, CommandRole, StartedAtRole, FinishedAtRole, ExitCodeRole, OutputRole };
    explicit ValidationModel(QObject *parent = nullptr);
    void setSnapshot(const QJsonObject &validation);
    void clear() override;
    QVariantMap latest() const { return m_latest; }
    QVariantList reports() const { return m_reports; }
    QString status() const { return m_latest.value("content").toMap().value("status").toString(); }
signals:
    void snapshotChanged();
private:
    QVariantMap m_latest;
    QVariantList m_reports;
};
