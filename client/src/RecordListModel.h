#pragma once
#include <QAbstractListModel>
#include <QVariantMap>

// Stable rows for presentation; no task state or authorization is decided here.
class RecordListModel : public QAbstractListModel {
    Q_OBJECT
    Q_PROPERTY(int count READ count NOTIFY countChanged)
public:
    explicit RecordListModel(QObject *parent = nullptr) : QAbstractListModel(parent) {}
    int count() const { return int(m_rows.size()); }
    int rowCount(const QModelIndex &parent = {}) const override { return parent.isValid() ? 0 : count(); }
    QVariant data(const QModelIndex &index, int role) const override;
    QHash<int,QByteArray> roleNames() const override { return {{Qt::UserRole+1,"record"}}; }
    void upsert(const QString &key, const QVariantMap &record);
    void clear();
    Q_INVOKABLE QVariantMap get(int row) const;
signals:
    void countChanged();
private:
    QList<QVariantMap> m_rows;
    QHash<QString,int> m_indices;
};
