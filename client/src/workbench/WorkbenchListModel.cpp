#include "WorkbenchListModel.h"
#include <QThread>
#include <QPointer>

WorkbenchListModel::WorkbenchListModel(QHash<int, QByteArray> roles, QObject *parent)
    : QAbstractListModel(parent), m_roles(std::move(roles))
{ m_roles.insert(RecordRole, "record"); }

QVariant WorkbenchListModel::data(const QModelIndex &item, int role) const
{
    if (!item.isValid() || item.model() != this || item.column() != 0 || item.row() < 0 || item.row() >= count()) return {};
    if (role == RecordRole) return m_rows[item.row()];
    const auto name = m_roles.constFind(role);
    return name == m_roles.cend() ? QVariant{} : m_rows[item.row()].value(QString::fromLatin1(*name));
}

QVariantMap WorkbenchListModel::get(int row) const
{ return row >= 0 && row < count() ? m_rows[row] : QVariantMap{}; }

void WorkbenchListModel::runUpdate(std::function<void(quint64)> update)
{
    Q_ASSERT(QThread::currentThread() == thread());
    ++m_epoch;
    if (m_updating) {
        m_pendingUpdates.append(std::move(update));
        return;
    }
    m_updating = true;
    QPointer<WorkbenchListModel> guard(this);
    for (;;) {
        const auto epoch = m_epoch;
        update(epoch);
        if (!guard) return;
        if (m_pendingUpdates.isEmpty()) {
            m_updating = false;
            return;
        }
        update = m_pendingUpdates.takeFirst();
    }
}

bool WorkbenchListModel::replaceRows(const QList<QVariantMap> &next, const QString &key)
{
    Q_ASSERT(QThread::currentThread() == thread());
    if (m_rows == next) return true;
    QPointer<WorkbenchListModel> guard(this);
    const auto epoch = m_epoch;
    bool sameKeys = m_rows.size() == next.size();
    for (qsizetype i = 0; sameKeys && i < next.size(); ++i)
        sameKeys = m_rows[i].value(key) == next[i].value(key);
    if (sameKeys) {
        for (int i = 0; i < count(); ++i) {
            if (m_rows[i] == next[i]) continue;
            m_rows[i] = next[i];
            emit dataChanged(index(i), index(i));
            if (!guard || !currentUpdate(epoch)) return false;
        }
        return true;
    }
    const int previousCount = count();
    beginResetModel();
    if (!guard) return false;
    m_rows = next;
    endResetModel();
    if (!guard) return false;
    if (count() != previousCount) emit countChanged();
    return guard && currentUpdate(epoch);
}

bool WorkbenchListModel::upsertRow(const QVariantMap &row, const QString &key)
{
    Q_ASSERT(QThread::currentThread() == thread());
    QPointer<WorkbenchListModel> guard(this);
    const auto epoch = m_epoch;
    for (int i = 0; i < count(); ++i) {
        if (m_rows[i].value(key) != row.value(key)) continue;
        if (m_rows[i] != row) {
            m_rows[i] = row;
            emit dataChanged(index(i), index(i));
        }
        return guard && currentUpdate(epoch);
    }
    const int position = count();
    beginInsertRows({}, position, position);
    if (!guard) return false;
    m_rows.append(row);
    endInsertRows();
    if (!guard) return false;
    emit countChanged();
    return guard && currentUpdate(epoch);
}

void WorkbenchListModel::clear()
{ runUpdate([this](quint64) { replaceRows({}, {}); }); }
