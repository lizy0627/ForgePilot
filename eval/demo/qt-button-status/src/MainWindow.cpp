#include "MainWindow.h"
#include "StatusController.h"
#include <QLabel>
#include <QPushButton>
#include <QVBoxLayout>

MainWindow::MainWindow(QWidget *parent) : QWidget(parent)
{
    setWindowTitle(QStringLiteral("ForgePilot Demo - Button Status"));
    resize(460, 200);
    auto *controller = new StatusController(this);
    controller->setObjectName(QStringLiteral("controller"));
    auto *label = new QLabel(controller->status(), this);
    label->setObjectName(QStringLiteral("statusLabel"));
    label->setStyleSheet(QStringLiteral("font-size:28px; color:#174d3c;"));
    auto *button = new QPushButton(QStringLiteral("Activate"), this);
    button->setObjectName(QStringLiteral("activateButton"));
    auto *layout = new QVBoxLayout(this);
    layout->addWidget(new QLabel(QStringLiteral("Click Activate: Ready should become Active"), this));
    layout->addWidget(label);
    layout->addWidget(button);
    connect(button, &QPushButton::clicked, controller, &StatusController::activate);
    connect(controller, &StatusController::statusChanged, label, &QLabel::setText);
}
