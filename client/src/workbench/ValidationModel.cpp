#include "ValidationModel.h"
#include <QJsonArray>

ValidationModel::ValidationModel(QObject *parent)
    : WorkbenchListModel({{KindRole,"kind"},{CommandRole,"command"},{StartedAtRole,"startedAt"},
        {FinishedAtRole,"finishedAt"},{ExitCodeRole,"exitCode"},{OutputRole,"output"}}, parent) {}

void ValidationModel::setSnapshot(const QJsonObject &validation)
{
    runUpdate([this, validation](quint64) {
    if (!validation.value("items").isArray() || !(validation.value("latest").isNull() || validation.value("latest").isObject())) return;
    const auto latest = validation.value("latest").toObject();
    const auto commands = latest.value("content").toObject().value("commands");
    if (!latest.isEmpty() && !commands.isArray()) return;
    QList<QVariantMap> next;
    int position = 0;
    for (const auto &entry : commands.toArray()) {
        if (!entry.isObject()) return;
        const auto command = entry.toObject();
        auto row = command.toVariantMap();
        row.insert("key", latest.value("artifact_id").toString() + ':' + QString::number(position++));
        row.insert("command", command.value("command_summary").toString());
        row.insert("startedAt", command.value("started_at").toString());
        row.insert("finishedAt", command.value("finished_at").toString());
        row.insert("exitCode", command.value("exit_code").toInteger());
        row.insert("output", command.value("output_summary").toString());
        next.append(row);
    }
    const auto reports = validation.value("items").toArray().toVariantList();
    const bool changed = m_latest != latest.toVariantMap() || m_reports != reports;
    m_latest = latest.toVariantMap(); m_reports = reports;
    if (!replaceRows(next, "key")) return;
    if (changed) emit snapshotChanged();
    });
}

void ValidationModel::clear()
{
    runUpdate([this](quint64) {
    if (m_latest.isEmpty() && m_reports.isEmpty() && count() == 0) return;
    m_latest.clear(); m_reports.clear();
    if (!replaceRows({}, {})) return;
    emit snapshotChanged();
    });
}
