#pragma once
#include "AgentApiClient.h"
#include <QPointer>
#include <functional>

// Only previews and confirms backend delivery. No local Git or filesystem work.
class DeliveryController : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool canPreview READ canPreview NOTIFY changed)
    Q_PROPERTY(bool canConfirm READ canConfirm NOTIFY changed)
    Q_PROPERTY(QVariantMap preview READ preview NOTIFY changed)
    Q_PROPERTY(QVariantMap result READ result NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
public:
    explicit DeliveryController(AgentApiClient *api, QObject *parent = nullptr);
    ~DeliveryController() override;
    bool busy() const { return m_request != 0; }
    bool canPreview() const;
    bool canConfirm() const;
    QVariantMap preview() const { return m_preview.toVariantMap(); }
    QVariantMap result() const { return m_result.toVariantMap(); }
    QString error() const { return m_error; }
    void setMission(const QJsonObject &mission);
    void setEnabled(bool enabled);
    void invalidatePreview();
    void invalidatePreviewBefore(qint64 revision);
    void clear();
    Q_INVOKABLE void prepare(const QString &mode = "patch", const QString &targetBranch = {});
    Q_INVOKABLE void confirm();
signals:
    void changed();
    void snapshotReceived(const QJsonObject &mission);
    void refreshRequired();
private:
    std::function<bool()> continuation();
    QPointer<AgentApiClient> m_api;
    QJsonObject m_mission, m_preview, m_result;
    QString m_mode, m_target, m_error;
    qint64 m_previewRevision = 0;
    quint64 m_request = 0;
    quint64 m_generation = 0;
    bool m_enabled = false, m_confirming = false;
};
