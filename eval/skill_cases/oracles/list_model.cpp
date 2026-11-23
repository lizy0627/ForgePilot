#include "subject.h"
#include <QCoreApplication>
#include <iostream>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QStringList expected{"one", "two", "three"};
    QStringList disk = expected;
    Labels model(disk);
    int calls = 0;
    int changedRow = -1;
    bool precise = true;
    QObject::connect(&model, &Labels::dataChanged,
        [&](const QModelIndex& from, const QModelIndex& to, const QList<int>& roles) {
            ++calls;
            changedRow = from.row();
            precise &= from.model() == &model && from == to
                && (roles.isEmpty() || roles.contains(Qt::DisplayRole));
        });

    for (int row : {2, 0, 1, 2}) {
        const auto newLabel = QString("updated-%1-%2").arg(row).arg(calls);
        const int previous = calls;
        if (!model.save(row, newLabel) || calls != previous + 1 || changedRow != row || !precise)
            return 1;
        expected[row] = newLabel;
        Labels reopened(disk);
        for (int i = 0; i < expected.size(); ++i) {
            if (model.data(model.index(i), Qt::DisplayRole) != expected[i] ||
                reopened.data(reopened.index(i), Qt::DisplayRole) != expected[i])
                return 2;
        }
        if (!model.save(row, newLabel) || calls != previous + 1)
            return 3;
    }
    const int before = calls;
    if (model.save(1, "   ") || model.save(-1, "invalid") || model.save(3, "invalid"))
        return 4;
    if (disk != expected || calls != before)
        return 5;
    for (int i = 0; i < expected.size(); ++i) {
        if (model.data(model.index(i), Qt::DisplayRole) != expected[i])
            return 6;
    }
    std::cout << FORGEPILOT_ORACLE_TOKEN << "\n";
    return 0;
}
