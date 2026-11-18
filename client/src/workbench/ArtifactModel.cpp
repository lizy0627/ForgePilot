#include "ArtifactModel.h"
#include <QJsonArray>
#include <QSet>

ArtifactModel::ArtifactModel(QObject *parent)
    : WorkbenchListModel({{ArtifactIdRole,"artifactId"},{ArtifactTypeRole,"artifactType"},
        {ProducerRole,"producer"},{ProducerNodeRole,"producerNode"},{CreatedAtRole,"createdAt"},
        {ContentHashRole,"contentHash"},{ContentRole,"content"},{ParentsRole,"parentArtifactIds"}}, parent) {}

void ArtifactModel::setSnapshot(const QJsonObject &artifacts)
{
    runUpdate([this, artifacts](quint64) {
    if (!artifacts.value("items").isArray()) return;
    QList<QVariantMap> next;
    QSet<QString> ids;
    for (const auto &entry : artifacts.value("items").toArray()) {
        if (!entry.isObject()) return;
        const auto artifact = entry.toObject();
        const auto id = artifact.value("artifact_id").toString();
        if (id.isEmpty() || ids.contains(id)) return;
        ids.insert(id);
        auto row = artifact.toVariantMap();
        row.insert("artifactId", id);
        row.insert("artifactType", artifact.value("artifact_type").toString());
        row.insert("producerNode", artifact.value("producer_node").toString());
        row.insert("createdAt", artifact.value("created_at").toString());
        row.insert("contentHash", artifact.value("content_sha256").toString());
        row.insert("parentArtifactIds", artifact.value("parent_artifact_ids").toArray().toVariantList());
        next.append(row);
    }
    replaceRows(next, "artifactId");
    });
}
