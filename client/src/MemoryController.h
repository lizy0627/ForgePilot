#pragma once
#include <QObject>
#include <QVariantList>
#include <QVariantMap>
#include <QJsonObject>
class ConversationController;

class MemoryController:public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList items READ items NOTIFY changed)
    Q_PROPERTY(QVariantMap summary READ summary NOTIFY changed)
    Q_PROPERTY(bool loading READ loading NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
public:
    explicit MemoryController(ConversationController *conversations);
    QVariantList items() const { return m_items; }
    QVariantMap summary() const { return m_summary.toVariantMap(); }
    bool loading() const { return m_pending>0; }
    bool busy() const { return m_busy; }
    QString error() const { return m_error; }
    Q_INVOKABLE void open();
    Q_INVOKABLE void close();
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void save(const QString &id,const QString &content,const QString &scope,int revision,bool source);
    Q_INVOKABLE void remove(const QString &id);
    Q_INVOKABLE void saveSummary(const QString &content);
    Q_INVOKABLE void removeSummary();
    Q_INVOKABLE void enableSummary();
signals:
    void changed();
    void saved();
    void scopeChanged();
private:
    void synchronize();
    void mutate(const QByteArray &method,const QString &path,const QJsonObject &body);
    ConversationController *m_conversations;
    QVariantList m_items;
    QJsonObject m_summary;
    QString m_id,m_project,m_base,m_error;
    quint64 m_generation=0;
    int m_pending=0;
    bool m_open=false,m_busy=false;
};
