#pragma once
#include "BackendConnection.h"
#include "AgentApiClient.h"
#include "SseClient.h"
#include "RecordListModel.h"
#include "RepositoryModel.h"
#include <QVariantMap>

class TaskController : public QObject {
    Q_OBJECT
    Q_PROPERTY(QUrl baseUrl READ baseUrl WRITE setBaseUrl NOTIFY baseUrlChanged)
    Q_PROPERTY(QString repositoryPath READ repositoryPath WRITE setRepositoryPath NOTIFY repositoryPathChanged)
    Q_PROPERTY(QString taskId READ taskId NOTIFY taskChanged)
    Q_PROPERTY(QString state READ state NOTIFY taskChanged)
    Q_PROPERTY(bool ragRequested READ ragRequested NOTIFY taskChanged)
    Q_PROPERTY(bool ragUsed READ ragUsed NOTIFY taskChanged)
    Q_PROPERTY(QString ragStatus READ ragStatus NOTIFY taskChanged)
    Q_PROPERTY(int planVersion READ planVersion NOTIFY taskChanged)
    Q_PROPERTY(QVariantMap plan READ plan NOTIFY taskChanged)
    Q_PROPERTY(QVariantMap snapshot READ snapshot NOTIFY taskChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY taskChanged)
    Q_PROPERTY(bool canApprove READ canApprove NOTIFY taskChanged)
    Q_PROPERTY(bool canFeedback READ canFeedback NOTIFY taskChanged)
    Q_PROPERTY(bool feedbackOpen READ feedbackOpen NOTIFY feedbackChanged)
    Q_PROPERTY(QString feedbackDraft READ feedbackDraft WRITE setFeedbackDraft NOTIFY feedbackChanged)
    Q_PROPERTY(int feedbackSourceVersion READ feedbackSourceVersion NOTIFY feedbackChanged)
    Q_PROPERTY(QString feedbackNotice READ feedbackNotice NOTIFY feedbackChanged)
    Q_PROPERTY(bool feedbackSending READ feedbackSending NOTIFY feedbackChanged)
    Q_PROPERTY(bool feedbackReplanning READ feedbackReplanning NOTIFY feedbackChanged)
    Q_PROPERTY(bool feedbackShowsPreviousPlan READ feedbackShowsPreviousPlan NOTIFY feedbackChanged)
    Q_PROPERTY(bool canCancel READ canCancel NOTIFY taskChanged)
    Q_PROPERTY(QString connectionState READ connectionState NOTIFY connectionStateChanged)
    Q_PROPERTY(QVariantList tasks READ tasks NOTIFY tasksChanged)
    Q_PROPERTY(RecordListModel* traces READ traces CONSTANT)
    Q_PROPERTY(RepositoryModel* repository READ repository CONSTANT)
    Q_PROPERTY(QVariantList buildReports READ buildReports NOTIFY taskChanged)
    Q_PROPERTY(QVariantList diffFiles READ diffFiles NOTIFY taskChanged)
    Q_PROPERTY(bool historyReadOnly READ historyReadOnly NOTIFY taskChanged)
    Q_PROPERTY(QString serverStatus READ serverStatus NOTIFY serverStatusChanged)
    Q_PROPERTY(QString connectionWarning READ connectionWarning NOTIFY connectionStateChanged)
    Q_PROPERTY(bool historyHasMore READ historyHasMore NOTIFY tasksChanged)
    Q_PROPERTY(bool historyLoading READ historyLoading NOTIFY tasksChanged)
    Q_PROPERTY(int historyTotal READ historyTotal NOTIFY tasksChanged)
    Q_PROPERTY(RecordListModel* storedTrace READ storedTrace CONSTANT)
    Q_PROPERTY(bool traceHasMore READ traceHasMore NOTIFY traceChanged)
    Q_PROPERTY(bool traceLoading READ traceLoading NOTIFY traceChanged)
    Q_PROPERTY(QString traceDetailText READ traceDetailText NOTIFY traceChanged)
public:
    explicit TaskController(QObject *parent = nullptr);
    explicit TaskController(BackendConnection &connection, QObject *parent = nullptr);
    ~TaskController() override { m_requests.clear(); m_api.abortPending(); m_sse.disconnect(); }
    QUrl baseUrl() const { return m_connection.baseUrl(); }
    void setBaseUrl(const QUrl &url);
    QString repositoryPath() const { return m_repository; }
    void setRepositoryPath(const QString &path);
    QString taskId() const { return m_taskId; }
    QString state() const { return m_snapshot.value("state").toString(); }
    bool ragRequested() const { return m_snapshot.value("rag_requested").toBool(true); }
    bool ragUsed() const { return m_snapshot.value("rag_used").toBool(); }
    QString ragStatus() const { return m_snapshot.value("rag_status").toString("unavailable"); }
    int planVersion() const { return m_snapshot.value("plan_version").toInt(); }
    QVariantMap plan() const { return m_snapshot.value("plan").toObject().toVariantMap(); }
    QVariantMap snapshot() const { return m_snapshot.toVariantMap(); }
    QString error() const { return m_error; }
    bool busy() const { return m_mutation != 0 || m_feedbackRequest != 0; }
    bool canApprove() const { return !m_historyReadOnly && !busy() && !m_feedbackNeedsRefresh && state() == "waiting_approval" && planVersion() > 0 && !plan().isEmpty(); }
    bool canFeedback() const { return canApprove() && m_snapshot.value("steps").toInt() < 30; }
    bool feedbackOpen() const { return m_feedbackOpen; }
    QString feedbackDraft() const { return m_feedbackDraft; }
    void setFeedbackDraft(const QString &text);
    int feedbackSourceVersion() const { return m_feedbackSourceVersion; }
    QString feedbackNotice() const;
    bool feedbackSending() const { return m_feedbackRequest != 0; }
    bool feedbackReplanning() const { return m_hasFeedback && state() == "planning"; }
    bool feedbackShowsPreviousPlan() const { return feedbackReplanning() && !m_feedbackPlanArrived && planVersion() > 0; }
    bool canCancel() const;
    QString connectionState() const { return m_sse.connectionState(); }
    qint64 eventSequence() const { return m_eventSequence; }
    QVariantList tasks() const { return m_tasks; }
    RecordListModel *traces() { return &m_traces; }
    RepositoryModel *repository() { return &m_repositoryModel; }
    QVariantList buildReports() const;
    QVariantList diffFiles() const;
    bool historyReadOnly() const { return m_historyReadOnly; }
    QString serverStatus() const { return m_serverStatus; }
    QString connectionWarning() const { return m_connectionWarning; }
    bool historyHasMore() const { return !m_historyCursor.isEmpty(); }
    bool historyLoading() const { return m_historyLoading; }
    int historyTotal() const { return m_historyTotal; }
    RecordListModel* storedTrace() { return &m_storedTrace; }
    bool traceHasMore() const { return m_traceCursor>0; }
    bool traceLoading() const { return m_traceLoading; }
    QString traceDetailText() const { return m_traceDetailText; }
    Q_INVOKABLE QString errorSummary(const QString &code) const;
    void setConversationFilter(const QString &id);
    Q_INVOKABLE void viewHistoryTask(const QString &taskId);
    Q_INVOKABLE void openExecutionView();
    Q_INVOKABLE bool configureBackend(const QString &url);
    Q_INVOKABLE void clearError() { setError({}); }
    Q_INVOKABLE void approvePlan();
    Q_INVOKABLE void openPlanFeedback();
    Q_INVOKABLE void closePlanFeedback();
    Q_INVOKABLE void submitPlanFeedback(const QString &feedback);
    Q_INVOKABLE int feedbackCharacterCount(const QString &text) const;
    Q_INVOKABLE QString feedbackValidationError(const QString &text) const;
    Q_INVOKABLE void cancelTask();
    Q_INVOKABLE void rejectPlan();
    Q_INVOKABLE void selectTask(const QString &taskId);
    Q_INVOKABLE void refreshTask();
    Q_INVOKABLE void refreshTasks();
    Q_INVOKABLE void loadMoreTasks();
    Q_INVOKABLE void refreshTrace();
    Q_INVOKABLE void loadMoreTrace();
    Q_INVOKABLE void loadTraceDetail(qint64 sequence);
    Q_INVOKABLE void reconnectEvents();
signals:
    void baseUrlChanged();
    void repositoryPathChanged();
    void taskChanged();
    void tasksChanged();
    void errorChanged();
    void connectionStateChanged();
    void planAvailable();
    void approvalRequired();
    void buildFinished(bool success);
    void testFinished(bool success);
    void taskFinished(const QString &state);
    void serverStatusChanged();
    void traceChanged();
    void feedbackChanged();
private:
    void initialize();
    void resetBackend();
    void resetSelection();
    void watch(quint64 request, bool mutation = false);
    bool applySnapshot(const QJsonObject &body);
    void applyEvent(const QString &id, const QJsonObject &payload);
    void notifyTransition(const QString &oldState, int oldVersion);
    void setError(const QString &message);
    void clearFeedback();
    void projectEvent(const QString &id,const QJsonObject &payload);
    BackendConnection &m_connection;
    AgentApiClient m_api;
    SseClient m_sse;
    RecordListModel m_traces;
    RepositoryModel m_repositoryModel;
    QHash<QString,QStringList> m_openTools;
    QString m_historyCursor,m_traceDetailText,m_conversationFilter;
    bool m_appendHistory=false,m_historyLoading=false,m_traceLoading=false;
    int m_historyTotal=0;
    qint64 m_traceCursor=0;
    quint64 m_traceRequest=0,m_traceDetailRequest=0;
    RecordListModel m_storedTrace;
    bool m_historyReadOnly=false,m_conversationScoped=false;
    QString m_serverStatus{"disconnected"},m_connectionWarning;
    QString m_repository, m_taskId, m_error, m_snapshotTime;
    QJsonObject m_snapshot;
    QVariantList m_tasks;
    QHash<quint64, quint64> m_requests;
    quint64 m_generation = 0, m_mutation = 0, m_lastListRequest = 0;
    quint64 m_feedbackRequest = 0;
    QString m_feedbackDraft, m_feedbackTask;
    int m_feedbackSourceVersion = 0;
    bool m_feedbackOpen = false, m_feedbackUnconfirmed = false, m_hasFeedback = false;
    bool m_feedbackNeedsRefresh = false, m_feedbackPlanArrived = false;
    qint64 m_revision = 0, m_eventSequence = 0;
};
