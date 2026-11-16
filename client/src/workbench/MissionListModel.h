#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

// Persisted Mission history, in the server's stable creation order.
class MissionListModel : public WorkbenchListModel {
    Q_OBJECT
public:
    enum Role { TaskIdRole = RecordRole + 1, StatusRole, RevisionRole, RepositoryRole,
                RequestRole, TaskTypeRole, CreatedAtRole, UpdatedAtRole };
    explicit MissionListModel(QObject *parent = nullptr);
    void upsert(const QJsonObject &mission, bool insert = true);
    void setPage(const QJsonArray &items, bool append);
    void removeMission(const QString &taskId);
};
