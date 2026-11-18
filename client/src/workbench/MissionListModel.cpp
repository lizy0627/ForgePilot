#include "MissionListModel.h"
#include <QJsonArray>

namespace {
QVariantMap missionRow(const QJsonObject &mission) {
    auto row = mission.toVariantMap();
    row.insert("taskId", mission.value("task_id").toString());
    row.insert("revision", mission.value("current_revision").toInteger());
    row.insert("repositoryPath", mission.value("repository_path").toString());
    row.insert("taskType", mission.value("task_type").toString());
    row.insert("createdAt", mission.value("created_at").toString());
    row.insert("updatedAt", mission.value("updated_at").toString());
    return row;
}
}

MissionListModel::MissionListModel(QObject *parent)
    : WorkbenchListModel({{TaskIdRole,"taskId"},{StatusRole,"status"},{RevisionRole,"revision"},
        {RepositoryRole,"repositoryPath"},{RequestRole,"request"},{TaskTypeRole,"taskType"},
        {CreatedAtRole,"createdAt"},{UpdatedAtRole,"updatedAt"}}, parent) {}

void MissionListModel::upsert(const QJsonObject &mission, bool insert)
{
    runUpdate([this, mission, insert](quint64) {
    const auto id = mission.value("task_id").toString();
    const auto revision = mission.value("current_revision").toInteger();
    if (id.isEmpty() || revision < 1) return;
    bool found = false;
    for (const auto &existing : rows())
        if (existing.value("taskId") == id) {
            if (existing.value("revision").toLongLong() > revision) return;
            found = true; break;
        }
    if (insert || found) upsertRow(missionRow(mission), "taskId");
    });
}

void MissionListModel::setPage(const QJsonArray &items, bool append)
{
    runUpdate([this, items, append](quint64) {
        auto next = append ? rows() : QList<QVariantMap>{};
        for (const auto &item : items) {
            auto row = missionRow(item.toObject());
            // A detail read may already have observed a newer revision than this page.
            for (const auto &existing : rows())
                if (existing.value("taskId") == row.value("taskId")
                    && existing.value("revision").toLongLong() > row.value("revision").toLongLong()) row = existing;
            bool found = false;
            for (auto &existing : next)
                if (existing.value("taskId") == row.value("taskId")) { existing = row; found = true; break; }
            if (!found) next.append(row);
        }
        replaceRows(next, "taskId");
    });
}

void MissionListModel::removeMission(const QString &taskId)
{
    runUpdate([this, taskId](quint64) {
        auto next = rows();
        next.removeIf([&taskId](const QVariantMap &row) { return row.value("taskId") == taskId; });
        replaceRows(next, "taskId");
    });
}
