#pragma once
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QSet>
#include <QVariantMap>

// One transport per workbench. Each open editor owns the serials it issued.
class SkillDraftClient : public QObject {
    Q_OBJECT
public:
    explicit SkillDraftClient(QObject *parent = nullptr);
    ~SkillDraftClient() override;
    void setContext(const QUrl &backend, const QString &task);
    Q_INVOKABLE QString newRequestId() const;
    Q_INVOKABLE quint64 listDrafts(int offset = 0);
    Q_INVOKABLE quint64 listAllDrafts(int offset = 0);
    Q_INVOKABLE quint64 createDraft(const QString &note, const QString &mode, const QString &requestId);
    Q_INVOKABLE quint64 getDraft(const QString &draftId);
    Q_INVOKABLE quint64 getSourceEvent(const QString &eventId);
    Q_INVOKABLE quint64 saveDraft(const QString &draftId, qint64 revision, const QVariantMap &changes);
    Q_INVOKABLE quint64 regenerate(const QString &draftId, qint64 revision, const QString &requestId);
    Q_INVOKABLE quint64 publish(const QString &draftId, qint64 revision, const QString &requestId);
    Q_INVOKABLE quint64 listSkills(int offset = 0);
    Q_INVOKABLE quint64 getVersion(const QString &skillId, qint64 version);
    Q_INVOKABLE quint64 revise(const QString &skillId, qint64 version, const QString &requestId);
    Q_INVOKABLE quint64 disable(const QString &skillId, qint64 revision);
signals:
    void succeeded(quint64 serial, const QVariantMap &body);
    void failed(quint64 serial, int status, const QString &code, const QString &message);
private:
    enum class Result { MissionDraft, Draft, RevisionDraft, Version, Identity, MissionDraftPage, DraftPage, SkillPage };
    quint64 request(const QByteArray &method, const QString &route, Result result, const QJsonObject &body = {},
                    const QString &recordId = {}, qint64 version = 0);
    quint64 reject();
    bool matches(Result result, const QJsonObject &body, const QString &recordId, qint64 version) const;
    void abortPending();
    QNetworkAccessManager m_network;
    QSet<QNetworkReply *> m_pending;
    QUrl m_backend;
    QString m_task;
    quint64 m_serial = 0, m_generation = 0;
};
