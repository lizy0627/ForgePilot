#include "subject.h"
#include <QCoreApplication>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QStringList saved{"red", "green", "yellow"};
    Labels model(saved);
    return model.save(1, "   ") ? 1 : 0;
}
