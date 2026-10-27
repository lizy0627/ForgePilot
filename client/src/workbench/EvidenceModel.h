#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

// Evidence IDs are scoped to their immutable bundle, never globally deduplicated.
class EvidenceModel : public WorkbenchListModel {
    Q_OBJECT
public:
    enum Role { EvidenceIdRole = RecordRole + 1, ArtifactIdRole, ClaimRole, PathRole, StartLineRole,
                EndLineRole, FileHashRole, SymbolRole, SourceRole, ConfidenceRole, ReasonRole };
    explicit EvidenceModel(QObject *parent = nullptr);
    void setSnapshot(const QJsonObject &artifacts);
};
