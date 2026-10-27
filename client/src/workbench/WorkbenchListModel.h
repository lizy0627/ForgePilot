#pragma once
#include <QAbstractListModel>
#include <QVariantMap>
#include <functional>

// Read-only, GUI-thread projections. Only controllers replace their data.
class WorkbenchListModel : public QAbstractListModel {
    Q_OBJECT
    Q_PROPERTY(int count READ count NOTIFY countChanged)
public:
    enum { RecordRole = Qt::UserRole + 1 };
    explicit WorkbenchListModel(QHash<int, QByteArray> roles, QObject *parent = nullptr);
    int count() const { return int(m_rows.size()); }
    int rowCount(const QModelIndex &parent = {}) const override { return parent.isValid() ? 0 : count(); }
    QVariant data(const QModelIndex &index, int role) const override;
    QHash<int, QByteArray> roleNames() const override { return m_roles; }
    Q_INVOKABLE QVariantMap get(int row) const;
    virtual void clear();
signals:
    void countChanged();
protected:
    // Signals may synchronously replace the mission or destroy the model. Serialize
    // reentrant writes in caller order and stop superseded writes.
    void runUpdate(std::function<void(quint64)> update);
    bool currentUpdate(quint64 epoch) const { return epoch == m_epoch; }
    bool replaceRows(const QList<QVariantMap> &rows, const QString &key);
    bool upsertRow(const QVariantMap &row, const QString &key);
    const QList<QVariantMap> &rows() const { return m_rows; }
private:
    QHash<int, QByteArray> m_roles;
    QList<QVariantMap> m_rows;
    QList<std::function<void(quint64)>> m_pendingUpdates;
    quint64 m_epoch = 0;
    bool m_updating = false;
};
