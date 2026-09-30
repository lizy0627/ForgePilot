#pragma once
#include <QAbstractListModel>
#include <QFutureWatcher>
#include <QSet>
#include <QUrl>
#include <atomic>
#include <memory>

struct RepositoryEntry { QString name, path; int depth; bool directory; };
struct RepositoryScan { QList<RepositoryEntry> entries; QString error; bool truncated=false; };
class RepositoryModel : public QAbstractListModel {
    Q_OBJECT
    Q_PROPERTY(QString root READ root WRITE setRoot NOTIFY changed)
    Q_PROPERTY(QString selectedPath READ selectedPath NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(bool loading READ loading NOTIFY changed)
    Q_PROPERTY(bool truncated READ truncated NOTIFY changed)
    Q_PROPERTY(int count READ count NOTIFY changed)
public:
    explicit RepositoryModel(QObject *parent=nullptr) : QAbstractListModel(parent) {}
    ~RepositoryModel() override { if(m_cancel) m_cancel->store(true); }
    QString root() const { return m_root; }
    QString selectedPath() const { return m_selected; }
    QString error() const { return m_error; }
    bool loading() const { return m_loading; }
    bool truncated() const { return m_truncated; }
    int count() const { return int(m_visible.size()); }
    int rowCount(const QModelIndex &parent={}) const override { return parent.isValid()?0:count(); }
    QVariant data(const QModelIndex &index,int role) const override;
    QHash<int,QByteArray> roleNames() const override;
    void setRoot(const QString &path);
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void activate(int row);
    static RepositoryScan scan(const QString &root, const std::shared_ptr<std::atomic_bool> &cancel);
signals:
    void changed();
private:
    void rebuild();
    QList<RepositoryEntry> m_entries;
    QList<int> m_visible;
    QSet<QString> m_expanded;
    QString m_root,m_selected,m_error;
    bool m_loading=false,m_truncated=false;
    quint64 m_generation=0;
    std::shared_ptr<std::atomic_bool> m_cancel;
};
