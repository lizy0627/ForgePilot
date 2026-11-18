#include "MemoryController.h"
#include "ConversationController.h"
#include "Protocol.h"
#include <QJsonArray>
#include <QUrl>
#include <utility>

MemoryController::MemoryController(ConversationController *c, Request request)
    : QObject(c), m_conversations(c), m_request(std::move(request))
{
    connect(c,&ConversationController::changed,this,&MemoryController::synchronize);
    connect(c,&ConversationController::conversationDeleted,this,[this](const QString &){
        ++m_generation; m_pending=0; m_items.clear(); m_summary={}; m_error.clear(); emit changed(); emit scopeChanged();
        if(m_open) refresh();
    });
    synchronize();
}
void MemoryController::synchronize()
{
    const auto base=m_conversations->baseUrl().toString();
    const auto id=m_conversations->conversationId(),project=m_conversations->repositoryPath();
    if(base==m_base && id==m_id && project==m_project) return;
    m_base=base; m_id=id; m_project=project; ++m_generation; m_pending=0; m_busy=false;
    m_items.clear(); m_summary={}; m_error.clear(); emit changed(); emit scopeChanged();
    if(m_open) refresh();
}
void MemoryController::open() { m_open=true; synchronize(); refresh(); }
void MemoryController::close() { m_open=false; }
void MemoryController::refresh()
{
    if(loading() || m_busy) return;
    m_error.clear(); m_pending=m_id.isEmpty()?1:2; const auto generation=++m_generation; emit changed();
    m_request("GET","/api/v1/memories"+(m_project.isEmpty()?QString{}:"?repository_path="+QString::fromLatin1(QUrl::toPercentEncoding(m_project))),{},
        [=,this](const QJsonObject &body,int,const QString &error){
            if(generation!=m_generation) return;
            --m_pending; if(error.isEmpty()) m_items=body.value("items").toArray().toVariantList(); else m_error=error; emit changed();
        });
    if(m_id.isEmpty()) { m_summary={}; return; }
    m_request("GET","/api/v1/conversations/"+m_id+"/context",{},[=,this](const QJsonObject &body,int status,const QString &error){
        if(generation!=m_generation) return;
        --m_pending;
        if(error.isEmpty()) m_summary=body;
        else { m_error=error; if(status==404) m_summary={}; }
        emit changed();
    });
}
void MemoryController::mutate(const QByteArray &method,const QString &path,const QJsonObject &body)
{
    if(m_busy || loading()) return;
    m_busy=true; m_error.clear(); const auto generation=++m_generation; emit changed();
    m_request(method,path,body,[=,this](const QJsonObject &reply,int,const QString &error){
        if(generation!=m_generation) return;
        m_busy=false; m_error=error;
        if(reply.value("error").toObject().value("code").toString()=="api_internal") m_error="记忆操作未完成，请刷新后重试。";
        emit changed();
        if(error.isEmpty()) { emit saved(); refresh(); }
    });
}
void MemoryController::save(const QString &id,const QString &content,const QString &scope,int revision,bool source)
{
    if(scope=="project" && m_project.isEmpty()) { m_error="请先关联项目，再添加当前项目记忆。"; emit changed(); return; }
    QJsonObject body{{"content",content.trimmed()},{"scope",scope}};
    if(scope=="project") body["repository_path"]=m_project;
    if(id.isEmpty()) {
        if(source && !m_id.isEmpty()) body["source_conversation_id"]=m_id;
        mutate("POST","/api/v1/memories",body);
    } else if(Protocol::validTaskId(id)) {
        body["revision"]=revision; mutate("PATCH","/api/v1/memories/"+id,body);
    }
}
void MemoryController::remove(const QString &id)
{ if(Protocol::validTaskId(id)) mutate("DELETE","/api/v1/memories/"+id,{}); }
void MemoryController::saveSummary(const QString &content)
{
    if(m_id.isEmpty() || m_summary.value("summary").toObject().isEmpty()) return;
    mutate("PATCH","/api/v1/conversations/"+m_id+"/context",{{"revision",m_summary.value("revision")},{"content",content.trimmed()}});
}
void MemoryController::removeSummary()
{
    if(m_id.isEmpty() || m_summary.isEmpty()) return;
    mutate("DELETE","/api/v1/conversations/"+m_id+"/context?revision="+QString::number(m_summary.value("revision").toInt()),{});
}
void MemoryController::enableSummary()
{
    if(m_id.isEmpty() || m_summary.isEmpty()) return;
    mutate("PATCH","/api/v1/conversations/"+m_id+"/context",{{"revision",m_summary.value("revision")},{"enabled",true}});
}
