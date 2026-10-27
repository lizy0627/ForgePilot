#include "EvidenceModel.h"
#include <QJsonArray>
#include <QSet>

EvidenceModel::EvidenceModel(QObject *parent)
    : WorkbenchListModel({{EvidenceIdRole,"evidenceId"},{ArtifactIdRole,"artifactId"},
        {ClaimRole,"claim"},{PathRole,"path"},{StartLineRole,"startLine"},{EndLineRole,"endLine"},
        {FileHashRole,"fileHash"},{SymbolRole,"symbol"},{SourceRole,"source"},
        {ConfidenceRole,"confidence"},{ReasonRole,"reason"}}, parent) {}

void EvidenceModel::setSnapshot(const QJsonObject &artifacts)
{
    runUpdate([this, artifacts](quint64) {
    if (!artifacts.value("items").isArray()) return;
    QList<QVariantMap> next;
    QSet<QString> keys;
    for (const auto &entry : artifacts.value("items").toArray()) {
        const auto artifact = entry.toObject();
        if (artifact.value("artifact_type") != "EvidenceBundle") continue;
        const auto artifactId = artifact.value("artifact_id").toString();
        const auto content = artifact.value("content").toObject();
        if (artifactId.isEmpty() || !content.value("items").isArray()) return;
        for (const auto &item : content.value("items").toArray()) {
            if (!item.isObject()) return;
            const auto evidence = item.toObject();
            const auto evidenceId = evidence.value("evidence_id").toString();
            const auto key = artifactId + ':' + evidenceId;
            if (evidenceId.isEmpty() || keys.contains(key)) return;
            keys.insert(key);
            auto row = evidence.toVariantMap();
            row.insert("key", key);
            row.insert("artifactId", artifactId);
            row.insert("evidenceId", evidenceId);
            row.insert("startLine", evidence.value("start_line").toVariant());
            row.insert("endLine", evidence.value("end_line").toVariant());
            row.insert("fileHash", evidence.value("file_hash").toVariant());
            next.append(row);
        }
    }
    replaceRows(next, "key");
    });
}
