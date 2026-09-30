#include "RepositoryModel.h"
#include <QDir>
#include <QDirIterator>
#include <QFileInfo>
#include <QtConcurrentRun>
#include <algorithm>
#include <functional>

static bool excluded(const QString &name)
{
    // Same display exclusions as the Runtime; this view lists names only and grants no access.
    static const QStringList patterns={".git",".hg",".svn",".forgepilot",".venv","*.venv","venv","env",
        "__pycache__",".pytest_cache",".mypy_cache",".ruff_cache",".cache",".uv-cache","build","build-*",
        "cmake-build-*","cmakefiles","out","bin","obj","dist","node_modules",".vs",".idea",".ssh",".aws",
        ".azure","logs",".env*","*secret*","*credential*","*.pem","*.key","*.p12","*.pfx","id_rsa*",
        "id_ed25519*",".netrc",".npmrc",".pypirc","config.local.*","cmakeuserpresets.json","local-env.cmd",
        "*.exe","*.dll","*.obj","*.pdb","*.lib","*.a","*.o","*.db","*.sqlite*"};
    return QDir::match(patterns,name.toLower());
}
RepositoryScan RepositoryModel::scan(const QString &path,const std::shared_ptr<std::atomic_bool> &cancel)
{
    RepositoryScan result;
    QFileInfo root(path);
    if(!root.isAbsolute() || !root.isDir() || root.isSymbolicLink() || root.isJunction()) {
        result.error="请选择存在的本地仓库绝对路径（不跟随链接目录）。"; return result;
    }
    if(!QFileInfo::exists(QDir(path).filePath(".git"))) {
        result.error="所选目录没有 .git 标记，请选择 Git 仓库根目录。"; return result;
    }
    const auto canonical=root.canonicalFilePath();
    int visited=0;
    std::function<void(QString,int)> walk=[&](const QString &folder,int depth) {
        if(cancel->load()) return;
        if(depth>24 || visited>=20000 || result.entries.size()>=5000) { result.truncated=true; return; }
        QList<QFileInfo> entries;
        QDirIterator iterator(folder,QDir::AllEntries|QDir::NoDotAndDotDot|QDir::Hidden|QDir::System);
        while(iterator.hasNext() && !cancel->load()) {
            iterator.next();
            if(++visited>20000) { result.truncated=true; break; }
            const auto info=iterator.fileInfo();
            if(excluded(info.fileName()) || info.isSymbolicLink() || info.isJunction()) continue;
            const auto actual=info.canonicalFilePath();
            if(!actual.startsWith(canonical+'/',Qt::CaseInsensitive)) continue;
            if(info.isDir() || info.isFile()) entries.append(info);
        }
        std::sort(entries.begin(),entries.end(),[](const QFileInfo&a,const QFileInfo&b) {
            return a.isDir()!=b.isDir()?a.isDir():QString::compare(a.fileName(),b.fileName(),Qt::CaseInsensitive)<0;
        });
        for(const auto &info:entries) {
            if(cancel->load()) return;
            if(result.entries.size()>=5000) { result.truncated=true; break; }
            result.entries.append({info.fileName(),info.absoluteFilePath(),depth,info.isDir()});
            if(info.isDir()) walk(info.absoluteFilePath(),depth+1);
        }
    };
    walk(canonical,0); return result;
}
QHash<int,QByteArray> RepositoryModel::roleNames() const
{ return {{Qt::UserRole+1,"name"},{Qt::UserRole+2,"path"},{Qt::UserRole+3,"depth"},{Qt::UserRole+4,"directory"},{Qt::UserRole+5,"expanded"}}; }
QVariant RepositoryModel::data(const QModelIndex &index,int role) const
{
    if(!index.isValid() || index.row()<0 || index.row()>=count()) return {};
    const auto &e=m_entries[m_visible[index.row()]];
    switch(role) {
    case Qt::UserRole+1:return e.name; case Qt::UserRole+2:return e.path; case Qt::UserRole+3:return e.depth;
    case Qt::UserRole+4:return e.directory; case Qt::UserRole+5:return m_expanded.contains(e.path); default:return {};
    }
}
void RepositoryModel::setRoot(const QString &path)
{ if(path!=m_root) { m_root=path; m_selected.clear(); m_expanded.clear(); refresh(); } }
void RepositoryModel::refresh()
{
    if(m_cancel) m_cancel->store(true);
    m_cancel=std::make_shared<std::atomic_bool>(false);
    const auto generation=++m_generation;
    beginResetModel(); m_entries.clear(); m_visible.clear(); endResetModel();
    m_error.clear(); m_truncated=false; m_loading=!m_root.isEmpty(); emit changed();
    if(!m_loading) return;
    auto *watcher=new QFutureWatcher<RepositoryScan>(this);
    connect(watcher,&QFutureWatcher<RepositoryScan>::finished,this,[=,this] {
        const auto result=watcher->result(); watcher->deleteLater();
        if(generation!=m_generation) return;
        m_entries=result.entries; m_error=result.error; m_truncated=result.truncated; m_loading=false;
        rebuild();
    });
    watcher->setFuture(QtConcurrent::run(&RepositoryModel::scan,m_root,m_cancel));
}
void RepositoryModel::rebuild()
{
    beginResetModel(); m_visible.clear(); int hiddenDepth=-1;
    for(int i=0;i<m_entries.size();++i) {
        const auto &e=m_entries[i];
        if(hiddenDepth>=0 && e.depth>hiddenDepth) continue;
        hiddenDepth=-1; m_visible.append(i);
        if(e.directory && !m_expanded.contains(e.path)) hiddenDepth=e.depth;
    }
    endResetModel(); emit changed();
}
void RepositoryModel::activate(int row)
{
    if(row<0 || row>=count()) return;
    const auto e=m_entries[m_visible[row]];
    m_selected=e.path;
    if(e.directory) { if(!m_expanded.remove(e.path)) m_expanded.insert(e.path); rebuild(); }
    else emit changed();
}
