#pragma once
#include "WorkbenchListModel.h"
#include <QJsonObject>

class ArtifactModel : public WorkbenchListModel {
    Q_OBJECT
public:
    enum Role { ArtifactIdRole = RecordRole + 1, ArtifactTypeRole, ProducerRole, ProducerNodeRole,
                CreatedAtRole, ContentHashRole, ContentRole, ParentsRole };
    explicit ArtifactModel(QObject *parent = nullptr);
    void setSnapshot(const QJsonObject &artifacts);
};
