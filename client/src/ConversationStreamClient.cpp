#include "ConversationStreamClient.h"
#include "Protocol.h"
#include "JsonHttp.h"
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QSet>
#include <algorithm>

ConversationStreamClient::ConversationStreamClient(QObject *parent):QObject(parent)
{
    m_network.setProxy(QNetworkProxy::NoProxy);
    m_reconnect.setSingleShot(true); m_idle.setSingleShot(true);
    connect(&m_reconnect,&QTimer::timeout,this,&ConversationStreamClient::open);
    connect(&m_idle,&QTimer::timeout,this,[this]{ if(m_reply) m_reply->abort(); });
}

bool ConversationStreamClient::setBaseUrl(const QUrl &url)
{
    if(!Protocol::validBaseUrl(url)) return false;
    disconnectStream(); m_base=url; return true;
}

void ConversationStreamClient::setState(const QString &state)
{
    if(m_state==state) return;
    m_state=state; emit connectionStateChanged(state,m_sessionGeneration);
}

void ConversationStreamClient::detach()
{
    ++m_connectionGeneration; m_reconnect.stop(); m_idle.stop();
    if(m_reply) {
        auto *reply=m_reply.data(); m_reply=nullptr;
        QObject::disconnect(reply,nullptr,this,nullptr); reply->abort(); reply->deleteLater();
    }
    m_parser.reset(); m_responseBytes=0;
}

void ConversationStreamClient::disconnectStream()
{
    ++m_sessionGeneration; m_active=false; detach(); setState("disconnected");
}

quint64 ConversationStreamClient::connectToRequest(const QString &conversationId,const QString &requestId,
                                                    const QString &lastEventId)
{
    disconnectStream();
    const auto revision=Protocol::sequence(lastEventId,requestId);
    if(!Protocol::validTaskId(conversationId) || !Protocol::validTaskId(requestId) || revision<0) {
        fail("stream_identity"); return m_sessionGeneration;
    }
    ++m_sessionGeneration;
    m_conversationId=conversationId; m_requestId=requestId; m_lastId=lastEventId; m_revision=revision;
    m_retry=m_backoff=1000; m_reconnectCount=0; m_active=true; open(); return m_sessionGeneration;
}

void ConversationStreamClient::requestRecovery()
{
    if(!m_active) return;
    reconnect("stream_revision_gap");
}

void ConversationStreamClient::reconnect(const QString &errorCode)
{
    if(!m_active) return;
    ++m_reconnectCount;
    detach(); setState("reconnecting"); emit transientError(errorCode,m_sessionGeneration);
    m_reconnect.start(m_backoff); m_backoff=std::min(m_backoff*2,30000);
}

void ConversationStreamClient::fail(const QString &errorCode)
{
    m_active=false; detach(); setState("error"); emit fatalError(errorCode,m_sessionGeneration);
}

void ConversationStreamClient::markUnsupported()
{
    m_active=false; detach(); setState("unsupported");
    emit unsupported("stream_unsupported",m_sessionGeneration);
}

void ConversationStreamClient::open()
{
    if(!m_active) return;
    m_parser.reset(); m_responseBytes=0;
    setState(m_lastId.isEmpty()?"connecting":"reconnecting");
    QUrl url=m_base;
    url.setPath("/api/v1/conversations/"+m_conversationId+"/requests/"+m_requestId+"/events");
    QNetworkRequest request(url);
    JsonHttp::authenticate(request);
    request.setRawHeader("Accept","text/event-stream");
    if(!m_lastId.isEmpty()) request.setRawHeader("Last-Event-ID",m_lastId.toUtf8());
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,QNetworkRequest::ManualRedirectPolicy);
    auto *reply=m_network.get(request); m_reply=reply; reply->setReadBufferSize(128*1024);
    const auto generation=++m_connectionGeneration; m_idle.start(45000);
    connect(reply,&QIODevice::readyRead,this,[=,this]{ read(reply,generation); });
    connect(reply,&QNetworkReply::finished,this,[=,this]{
        if(generation!=m_connectionGeneration) return;
        read(reply,generation); if(generation!=m_connectionGeneration) return;
        m_idle.stop(); const int status=reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        m_reply=nullptr; reply->deleteLater();
        if(status==204) { m_active=false; setState("ended"); emit streamEnded(m_sessionGeneration); return; }
        if(status==404) { markUnsupported(); return; }
        if(status>=300 && status<400) { fail("stream_redirect"); return; }
        if(status==401) { fail("stream_http_401"); return; }
        if(status==403) { fail("stream_http_403"); return; }
        if(status==409) { fail("stream_http_409"); return; }
        if(status>=400 && status<500) { fail("stream_http_4xx"); return; }
        if(m_active) reconnect("stream_network");
    });
}

void ConversationStreamClient::read(QNetworkReply *reply,quint64 generation)
{
    if(generation!=m_connectionGeneration || !m_active) return;
    if(reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt()!=200) return;
    if(!reply->header(QNetworkRequest::ContentTypeHeader).toString().startsWith("text/event-stream",Qt::CaseInsensitive)) {
        fail("stream_content_type"); return;
    }
    setState("connected");
    while(reply->bytesAvailable() && generation==m_connectionGeneration) {
        const auto bytes=reply->read(64*1024); m_responseBytes+=bytes.size();
        if(m_responseBytes>1024*1024) { fail("stream_response_limit"); return; }
        const auto frames=m_parser.feed(bytes); m_idle.start(45000);
        if(!m_parser.error().isEmpty()) { fail("stream_event_limit"); return; }
        for(const auto &frame:frames) {
            if(generation!=m_connectionGeneration) return;
            if(frame.retry>=0) { m_retry=m_backoff=frame.retry; continue; }
            accept(frame);
        }
    }
}

void ConversationStreamClient::accept(const SseFrame &frame)
{
    static const QSet<QString> allowed{"answer_started","answer_delta","answer_snapshot",
        "answer_segment_discarded","answer_segment_committed","tool_activity",
        "answer_completed","answer_cancelled","answer_failed"};
    const auto revision=Protocol::sequence(frame.id,m_requestId);
    if(revision<=0) { fail("stream_event_id"); return; }
    if(revision<=m_revision) return;
    QJsonParseError error; const auto document=QJsonDocument::fromJson(frame.data,&error);
    const auto payload=document.object();
    if(error.error!=QJsonParseError::NoError || !document.isObject() || !allowed.contains(frame.event)
       || payload.value("type").toString()!=frame.event
       || payload.value("conversation_id").toString()!=m_conversationId
       || payload.value("request_id").toString()!=m_requestId
       || payload.value("revision").toInteger()!=revision) {
        fail("stream_protocol"); return;
    }
    const bool snapshot=frame.event=="answer_snapshot";
    if(!snapshot && revision!=m_revision+1) { requestRecovery(); return; }
    if(frame.event=="answer_delta" && payload.value("previous_revision").toInteger()!=m_revision) {
        requestRecovery(); return;
    }
    m_revision=revision; m_lastId=frame.id; m_backoff=m_retry;
    const auto session=m_sessionGeneration;
    emit eventReceived(frame.event,payload,session);
    if(session!=m_sessionGeneration) return;
    if(frame.event=="answer_completed" || frame.event=="answer_cancelled" || frame.event=="answer_failed") {
        m_active=false; detach(); setState("ended"); emit streamEnded(session);
    }
}
