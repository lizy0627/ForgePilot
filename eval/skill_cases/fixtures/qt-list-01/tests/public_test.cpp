#include "subject.h"
#include <QCoreApplication>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QStringList saved{"red", "green", "yellow"};
    Labels model(saved);
    model.save(1, "lime");
    return model.data(model.index(1), Qt::DisplayRole) == "lime" ? 0 : 1;
}
