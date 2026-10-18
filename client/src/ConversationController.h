#pragma once
#include "RecordListModel.h"
#include "TaskController.h"
#include "MemoryController.h"
#include "ConversationStreamClient.h"
#include <QMap>
#include <QTimer>
#include <QJsonArray>
#include <functional>

// Conversation text never carries TaskController permissions or execution state.
class ConversationController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString conversationId READ conversationId NOTIFY changed)
    Q_PROPERTY(QString title READ title NOTIFY changed)
    Q_PROPERTY(QString repositoryPath READ repositoryPath NOTIFY changed)
    Q_PROPERTY(QString mode READ mode WRITE setMode NOTIFY changed)
    Q_PROPERTY(QString toolMode READ toolMode WRITE setToolMode NOTIFY changed)
    Q_PROPERTY(bool useRag READ useRag WRITE setUseRag NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QString serverStatus READ serverStatus NOTIFY changed)
    Q_PROPERTY(QString answerStatus READ answerStatus NOTIFY changed)
    Q_PROPERTY(QString contextNotice READ contextNotice NOTIFY changed)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(bool active READ active NOTIFY changed)
    Q_PROPERTY(QString activeMode READ activeMode NOTIFY changed)
    Q_PROPERTY(bool retryPending READ retryPending NOTIFY changed)
    Q_PROPERTY(bool hasMoreMessages READ hasMoreMessages NOTIFY changed)
    Q_PROPERTY(bool loading READ loading NOTIFY changed)
    Q_PROPERTY(bool historyLoading READ historyLoading NOTIFY historyChanged)
    Q_PROPERTY(bool historyHasMore READ historyHasMore NOTIFY historyChanged)
    Q_PROPERTY(QVariantList history READ history NOTIFY historyChanged)
    Q_PROPERTY(int historyTotal READ historyTotal NOTIFY historyChanged)
    Q_PROPERTY(QString deleteTarget READ deleteTarget NOTIFY changed)
    Q_PROPERTY(QString deleteTitle READ deleteTitle NOTIFY changed)
    Q_PROPERTY(QString deleteError READ deleteError NOTIFY changed)
    Q_PROPERTY(bool deleteLoading READ deleteLoading NOTIFY changed)
    Q_PROPERTY(bool deleteActive READ deleteActive NOTIFY changed)
    Q_PROPERTY(bool deleting READ deleting NOTIFY changed)
    Q_PROPERTY(RecordListModel* messages READ messages CONSTANT)
    Q_PROPERTY(MemoryController* memory READ memory CONSTANT)
    Q_PROPERTY(QVariantMap toolActivity READ toolActivity NOTIFY changed)
    Q_PROPERTY(QVariantList mcpTools READ mcpTools NOTIFY mcpToolsChanged)
    Q_PROPERTY(bool mcpToolsLoading READ mcpToolsLoading NOTIFY mcpToolsChanged)
    Q_PROPERTY(QString mcpToolsError READ mcpToolsError NOTIFY mcpToolsChanged)
    Q_PROPERTY(QString mcpToolsRequest READ mcpToolsRequest NOTIFY mcpToolsChanged)
public:
    explicit ConversationController(TaskController *tasks, QObject *parent=nullptr);
    ~ConversationController() override;
    QString conversationId() const { return m_id; }
    QString title() const { return m_detail.value("title").toString("新对话"); }
    QString repositoryPath() const { return m_detail.value("repository_path").toString(); }
    QString mode() const { return m_mode; }
    void setMode(const QString &mode);
    QString toolMode() const { return m_toolMode; }
    void setToolMode(const QString &mode);
    bool useRag() const { return m_useRag; }
    void setUseRag(bool enabled) { if (m_useRag != enabled) { m_useRag = enabled; emit changed(); } }
    QString error() const { return m_error; }
    QString serverStatus() const { return m_serverStatus; }
    QString answerStatus() const { return m_answerStatus; }
    QString contextNotice() const { return m_detail.value("context_notice").toString(); }
    bool busy() const { return m_creating || m_sending.contains(m_id) || m_editing || m_removing.contains(pendingKey()); }
    bool active() const { return !m_detail.value("active_request").toObject().isEmpty(); }
    QString activeMode() const { return m_detail.value("active_request").toObject().value("mode").toString(); }
    bool retryPending() const { return m_pending.contains(pendingKey()); }
    bool hasMoreMessages() const { return m_before>0; }
    bool loading() const { return m_loading; }
    bool historyLoading() const { return m_historyLoading; }
    bool historyHasMore() const { return !m_cursor.isEmpty(); }
    QVariantList history() const { return m_history; }
    int historyTotal() const { return m_historyTotal; }
    QString deleteTarget() const { return m_deleteTarget; }
    QString deleteTitle() const { return m_deleteTitle; }
    QString deleteError() const { return m_deleteError; }
    bool deleteLoading() const { return m_deleteLoading; }
    bool deleteActive() const { return m_deleteActive; }
    bool deleting() const { return m_deleting; }
    RecordListModel *messages() { return &m_messages; }
    MemoryController *memory() { return m_memory; }
    QVariantMap toolActivity() const;
    int uiModelUpdates() const { return m_uiModelUpdates; }
    int streamReconnectCount() const { return m_stream.reconnectCount(); }
    int requestPollInterval() const { return m_poll.interval(); }
    QVariantList mcpTools() const { return m_mcpTools; }
    bool mcpToolsLoading() const { return m_mcpToolsLoading; }
    QString mcpToolsError() const { return m_mcpToolsError; }
    QString mcpToolsRequest() const { return m_mcpToolsRequest; }
    Q_INVOKABLE void showMcpTools(const QString &requestId);
    Q_INVOKABLE void refreshMcpTools();
    Q_INVOKABLE void closeMcpTools();
    Q_INVOKABLE void openMcpSource(const QString &url);
    static bool safeSourceUrl(const QString &url);
    Q_INVOKABLE void refreshHistory(bool more=false);
    Q_INVOKABLE void newConversation();
    Q_INVOKABLE void selectConversation(const QString &id);
    Q_INVOKABLE void refresh(bool older=false);
    Q_INVOKABLE void renameConversation(const QString &title);
    Q_INVOKABLE void renameConversationById(const QString &id,const QString &title);
    Q_INVOKABLE void prepareDelete(const QString &id,const QString &title);
    Q_INVOKABLE void confirmDelete();
    Q_INVOKABLE void cancelDelete();
    Q_INVOKABLE void chooseRepository(const QUrl &folder);
    Q_INVOKABLE void clearRepository();
    Q_INVOKABLE void sendMessage(const QString &prompt);
    Q_INVOKABLE void retrySend();
    Q_INVOKABLE void stopAnswer();
    Q_INVOKABLE void showActiveTask();
    Q_INVOKABLE void clearError() { m_error.clear(); emit changed(); }
signals:
    void changed();
    void historyChanged();
    void selectionChanged();
    void submitted(const QString &conversationId,const QString &prompt);
    void conversationDeleted(const QString &conversationId);
    void mcpToolsChanged();
private:
    friend class MemoryController;
    using Callback=std::function<void(const QJsonObject&,int,const QString&)>;
    void request(const QByteArray &method,const QString &path,const QJsonObject &body,Callback callback,qint64 limit=16*1024*1024);
    void applyDetail(const QJsonObject &body,bool older=false);
    void pollActiveRequest();
    void resetRequestPolling();
    void bindRequestTask(const QJsonObject &previous,const QJsonObject &request);
    void finishRequest(const QJsonObject &request,bool detailLoaded=false);
    void beginAnswerStream(const QJsonObject &request);
    void stopAnswerStream(bool removeRow=false);
    void applyStreamEvent(const QString &type,const QJsonObject &payload,quint64 streamGeneration);
    void flushStreamBuffer();
    void updateStreamRow();
    bool reconcileStreamMessage(const QJsonArray &messages);
    bool streamOwnsPolling() const;
    void restorePollInterval();
    void recordClientStreamMetrics(const QString &errorCode);
    void patch(const QJsonObject &body);
    void create(const QString &prompt={},const QString &repositoryPath={});
    void postPending();
    void forgetConversation(const QString &id);
    QString pendingKey() const { return m_tasks->baseUrl().toString()+'/'+m_id; }
    QString path() const { return "/api/v1/conversations/"+m_id; }
    TaskController *m_tasks;
    MemoryController *m_memory;
    QNetworkAccessManager m_network;
    QSet<QNetworkReply*> m_replies;
    QTimer m_poll;
    QTimer m_streamFlush;
    ConversationStreamClient m_stream;
    RecordListModel m_messages;
    QMap<qint64,QVariantMap> m_rows;
    QHash<QString,QJsonObject> m_pending;
    QSet<QString> m_sending;
    QSet<QString> m_deleted,m_removing;
    QJsonObject m_detail;
    QString m_id,m_mode="chat",m_toolMode="auto",m_error,m_cursor,m_serverStatus="disconnected",m_answerStatus;
    QString m_streamRequest,m_streamSegment,m_streamDraft,m_streamBuffer,m_streamKey,m_streamSuppressedRequest;
    QVariantMap m_streamToolActivity;
    QString m_deleteTarget,m_deleteTitle,m_deleteError;
    QVariantList m_history;
    quint64 m_generation=0,m_listGeneration=0;
    quint64 m_streamGeneration=0;
    qint64 m_streamRevision=0;
    int m_uiModelUpdates=0;
    quint64 m_deleteGeneration=0;
    int m_historyTotal=0;
    bool m_deleteLoading=false,m_deleteActive=false,m_deleting=false;
    bool m_conversationStreamSupported=true,m_streamMetricsRecorded=false;
    qint64 m_before=0;
    bool m_loading=false,m_historyLoading=false,m_creating=false,m_editing=false,m_useRag=true;
    bool m_pollingRequest=false;
    quint64 m_pollGeneration=0;
    QString m_finishedRequest;
    QVariantList m_mcpTools;
    QString m_mcpToolsRequest,m_mcpToolsError;
    bool m_mcpToolsLoading=false;
    quint64 m_mcpToolsGeneration=0;
};
