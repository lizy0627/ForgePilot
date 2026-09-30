#include "RecordListModel.h"
QVariant RecordListModel::data(const QModelIndex &index, int role) const
{ return index.isValid() && role == Qt::UserRole+1 ? QVariant(get(index.row())) : QVariant{}; }
QVariantMap RecordListModel::get(int row) const
{ return row >= 0 && row < count() ? m_rows[row] : QVariantMap{}; }
void RecordListModel::upsert(const QString &key, const QVariantMap &record)
{
    if (m_indices.contains(key)) {
        const int row=m_indices[key];
        for (auto it=record.begin(); it!=record.end(); ++it) m_rows[row].insert(it.key(),it.value());
        emit dataChanged(index(row),index(row),{Qt::UserRole+1});
    } else {
        const int row=count(); beginInsertRows({},row,row); m_rows.append(record); m_indices[key]=row;
        endInsertRows(); emit countChanged();
    }
}
void RecordListModel::clear()
{ beginResetModel(); m_rows.clear(); m_indices.clear(); endResetModel(); emit countChanged(); }
