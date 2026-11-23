#include "subject.h"

QVariant Labels::data(const QModelIndex& index, int role) const {
    if (!index.isValid() || role != Qt::DisplayRole || index.row() >= rows_.size()) {
        return {};
    }
    return rows_.at(index.row());
}

bool Labels::save(int row, const QString& label) {
    if (row < 0 || row >= rows_.size() || label.trimmed().isEmpty()) {
        return false;
    }
    if (rows_.at(row) == label) {
        return true;
    }
    saved_[row] = label;
    rows_[row] = label;
    emit dataChanged(index(row), index(row), {Qt::DisplayRole});
    return true;
}
