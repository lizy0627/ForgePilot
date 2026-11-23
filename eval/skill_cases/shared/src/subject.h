#pragma once

#include <QAbstractListModel>
#include <QStringList>

class Labels : public QAbstractListModel {
public:
    explicit Labels(QStringList& saved) : saved_(saved), rows_(saved) {}
    int rowCount(const QModelIndex& parent = {}) const override {
        return parent.isValid() ? 0 : rows_.size();
    }
    QVariant data(const QModelIndex& index, int role) const override;
    bool save(int row, const QString& label);

private:
    QStringList& saved_;
    QStringList rows_;
};
