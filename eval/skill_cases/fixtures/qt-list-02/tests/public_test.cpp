#include "subject.h"
#include <QCoreApplication>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QStringList saved{"red", "green", "yellow"};
    Labels model(saved);
    int first = -1;
    QObject::connect(&model, &Labels::dataChanged, [&](const QModelIndex& index) { first = index.row(); });
    model.save(2, "blue");
    return first == 2 ? 0 : 1;
}
