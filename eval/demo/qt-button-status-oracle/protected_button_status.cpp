#include "MainWindow.h"
#include "StatusController.h"
#include <QLabel>
#include <QPushButton>
#include <QSignalSpy>
#include <QtTest>

class ProtectedButtonStatusTest : public QObject {
    Q_OBJECT
private slots:
    void clickUpdatesDisplayedStatusExactlyOnce() {
        MainWindow window;
        auto *button = window.findChild<QPushButton *>("activateButton");
        auto *label = window.findChild<QLabel *>("statusLabel");
        auto *controller = window.findChild<StatusController *>("controller");
        QVERIFY(button && label && controller);
        QSignalSpy changed(controller, &StatusController::statusChanged);
        QVERIFY(changed.isValid());
        QCOMPARE(label->text(), QStringLiteral("Ready"));
        window.show();
        QTest::mouseClick(button, Qt::LeftButton);
        QCOMPARE(controller->status(), QStringLiteral("Active"));
        QCOMPARE(label->text(), QStringLiteral("Active"));
        QCOMPARE(changed.count(), 1);
        QCOMPARE(changed.takeFirst().at(0).toString(), QStringLiteral("Active"));
        QTest::mouseClick(button, Qt::LeftButton);
        QCOMPARE(changed.count(), 0);
        QCOMPARE(label->text(), QStringLiteral("Active"));
    }
};
QTEST_MAIN(ProtectedButtonStatusTest)
#include "protected_button_status.moc"
