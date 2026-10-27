#include "DiffModel.h"
#include <QJsonArray>
#include <QSet>

DiffModel::DiffModel(QObject *parent)
    : WorkbenchListModel({{PathRole,"path"},{ChangeTypeRole,"changeType"},{UnifiedDiffRole,"unifiedDiff"},
        {BeforeHashRole,"beforeHash"},{AfterHashRole,"afterHash"},{PlanStepsRole,"planStepIds"},
        {CriteriaRole,"acceptanceCriteriaIds"},{TestChangeRole,"testChange"},{ArtifactIdRole,"artifactId"}}, parent) {}

void DiffModel::setSnapshot(const QJsonObject &diff)
{
    runUpdate([this, diff](quint64) {
    if (!diff.value("patch").isString() || !diff.value("change_sets").isArray()) return;
    const auto sets = diff.value("change_sets").toArray();
    const auto latest = sets.isEmpty() ? QJsonObject{} : sets.last().toObject();
    const auto files = latest.value("content").toObject().value("files");
    if (!sets.isEmpty() && !files.isArray()) return;
    QList<QVariantMap> next;
    QSet<QString> paths;
    for (const auto &entry : files.toArray()) {
        const auto file = entry.toObject();
        const auto path = file.value("path").toString();
        if (path.isEmpty() || paths.contains(path)) return;
        paths.insert(path);
        auto row = file.toVariantMap();
        row.insert("changeType", file.value("change_type").toString());
        row.insert("unifiedDiff", file.value("unified_diff").toString());
        row.insert("beforeHash", file.value("before_sha256").toVariant());
        row.insert("afterHash", file.value("after_sha256").toVariant());
        row.insert("planStepIds", file.value("plan_step_ids").toArray().toVariantList());
        row.insert("acceptanceCriteriaIds", file.value("acceptance_criteria_ids").toArray().toVariantList());
        row.insert("testChange", file.value("test_change").toString());
        row.insert("artifactId", latest.value("artifact_id").toString());
        next.append(row);
    }
    const bool changed = m_patch != diff.value("patch").toString() || m_changeSets != sets.toVariantList();
    m_patch = diff.value("patch").toString();
    m_changeSets = sets.toVariantList();
    if (!replaceRows(next, "path")) return;
    if (changed) emit snapshotChanged();
    });
}

void DiffModel::clear()
{
    runUpdate([this](quint64) {
    if (m_patch.isEmpty() && m_changeSets.isEmpty() && count() == 0) return;
    m_patch.clear(); m_changeSets.clear();
    if (!replaceRows({}, {})) return;
    emit snapshotChanged();
    });
}
