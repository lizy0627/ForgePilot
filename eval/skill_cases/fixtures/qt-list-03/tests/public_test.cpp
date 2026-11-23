#include "subject.h"
#include <QCoreApplication>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QStringList saved{"red", "green", "yellow"};
    Labels model(saved);
    bool redraw = false;
    QObject::connect(&model, &Labels::dataChanged,
        [&](const QModelIndex&, const QModelIndex&, const QList<int>& roles) {
            redraw = roles.isEmpty() || roles.contains(Qt::DisplayRole);
        });
    model.save(1, "lime");
    return redraw ? 0 : 1;
}
