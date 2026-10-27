#include "MissionListModel.h"

MissionListModel::MissionListModel(QObject *parent)
    : WorkbenchListModel({{TaskIdRole,"taskId"},{StatusRole,"status"},{RevisionRole,"revision"},
        {RepositoryRole,"repositoryPath"},{RequestRole,"request"},{TaskTypeRole,"taskType"},
        {CreatedAtRole,"createdAt"},{UpdatedAtRole,"updatedAt"}}, parent) {}

void MissionListModel::upsert(const QJsonObject &mission)
{
    runUpdate([this, mission](quint64) {
    const auto id = mission.value("task_id").toString();
    const auto revision = mission.value("current_revision").toInteger();
    if (id.isEmpty() || revision < 1) return;
    for (const auto &existing : rows())
        if (existing.value("taskId") == id && existing.value("revision").toLongLong() > revision) return;
    auto row = mission.toVariantMap();
    row.insert("taskId", id);
    row.insert("revision", revision);
    row.insert("repositoryPath", mission.value("repository_path").toString());
    row.insert("taskType", mission.value("task_type").toString());
    row.insert("createdAt", mission.value("created_at").toString());
    row.insert("updatedAt", mission.value("updated_at").toString());
    upsertRow(row, "taskId");
    });
}
