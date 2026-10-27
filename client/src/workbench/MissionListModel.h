#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

// Recently observed missions. API v2 currently has no collection GET endpoint.
class MissionListModel : public WorkbenchListModel {
    Q_OBJECT
public:
    enum Role { TaskIdRole = RecordRole + 1, StatusRole, RevisionRole, RepositoryRole,
                RequestRole, TaskTypeRole, CreatedAtRole, UpdatedAtRole };
    explicit MissionListModel(QObject *parent = nullptr);
    void upsert(const QJsonObject &mission);
};
