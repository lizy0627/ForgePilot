#pragma once
#include <QObject>
#include <QUrl>

class BackendConnection : public QObject {
    Q_OBJECT
    Q_PROPERTY(QUrl baseUrl READ baseUrl NOTIFY baseUrlChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)
public:
    using QObject::QObject;
    QUrl baseUrl() const { return m_baseUrl; }
    QString error() const { return m_error; }
    bool setBaseUrl(const QUrl &url);
    Q_INVOKABLE bool configureBackend(const QString &address);

signals:
    void baseUrlChanged();
    void errorChanged();

private:
    QUrl m_baseUrl{"http://127.0.0.1:8765"};
    QString m_error;
};
