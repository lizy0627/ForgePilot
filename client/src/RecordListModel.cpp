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
bool RecordListModel::remove(const QString &key)
{
    const auto found=m_indices.constFind(key);
    if(found==m_indices.cend()) return false;
    const int row=*found; beginRemoveRows({},row,row); m_rows.removeAt(row); m_indices.remove(key);
    for(auto it=m_indices.begin();it!=m_indices.end();++it) if(it.value()>row) --it.value();
    endRemoveRows(); emit countChanged(); return true;
}
void RecordListModel::clear()
{ beginResetModel(); m_rows.clear(); m_indices.clear(); endResetModel(); emit countChanged(); }
