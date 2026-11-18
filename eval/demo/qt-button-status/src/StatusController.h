#pragma once
#include <QObject>
#include <QString>

class StatusController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString status READ status NOTIFY statusChanged)
public:
    explicit StatusController(QObject *parent = nullptr) : QObject(parent) {}
    QString status() const { return active_ ? QStringLiteral("Active") : QStringLiteral("Ready"); }
public slots:
    void activate();
signals:
    void statusChanged(const QString &status);
private:
    bool active_ = false;
};
