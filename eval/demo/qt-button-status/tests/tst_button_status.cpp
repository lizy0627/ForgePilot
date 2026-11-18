#include "MainWindow.h"
#include "StatusController.h"
#include <QLabel>
#include <QPushButton>
#include <QSignalSpy>
#include <QtTest>

class ButtonStatusTest : public QObject {
    Q_OBJECT
private slots:
    void clickUpdatesUiOnce() {
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
        QCOMPARE(changed.at(0).at(0).toString(), QStringLiteral("Active"));
        QTest::mouseClick(button, Qt::LeftButton);
        QCOMPARE(changed.count(), 1);
        QCOMPARE(label->text(), QStringLiteral("Active"));
    }
};
QTEST_MAIN(ButtonStatusTest)
#include "tst_button_status.moc"
