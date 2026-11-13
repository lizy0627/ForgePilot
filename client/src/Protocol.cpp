#include "Protocol.h"
#include <QRegularExpression>
#include <QSet>

bool Protocol::validBaseUrl(const QUrl &url)
{
    return url.isValid() && url.scheme() == "http"
        && QSet<QString>{"127.0.0.1", "localhost", "::1"}.contains(url.host())
        && url.userInfo().isEmpty() && !url.hasQuery() && !url.hasFragment()
        && (url.path().isEmpty() || url.path() == "/")
        && (url.port() == -1 || (url.port() > 0 && url.port() <= 65535));
}
bool Protocol::validTaskId(const QString &id)
{
    static const QRegularExpression pattern("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");
    return pattern.match(id).hasMatch();
}
qint64 Protocol::sequence(const QString &id, const QString &taskId)
{
    if (id.isEmpty()) return 0;
    const auto prefix = taskId + ':';
    if (!id.startsWith(prefix)) return -1;
    const auto number = id.mid(prefix.size());
    bool ok = false;
    const auto value = number.toLongLong(&ok);
    return ok && value > 0 && QString::number(value) == number ? value : -1;
}
bool Protocol::terminal(const QString &state)
{
    return state == "completed" || state == "failed" || state == "cancelled";
}
bool Protocol::validState(const QString &state)
{
    static const QSet<QString> states{"created", "analyzing", "planning", "waiting_approval", "modifying",
        "building", "testing", "reflecting", "completed", "failed", "cancelled"};
    return states.contains(state);
}
bool Protocol::validEvent(const QJsonObject &p, const QString &event, const QString &taskId)
{
    static const QSet<QString> events{"task_state_changed", "message", "plan_created", "approval_required",
        "rag_usage", "tool_started", "tool_finished", "file_changed", "build_started", "build_finished", "test_started",
        "test_finished", "reflection", "task_completed", "task_failed"};
    return events.contains(event) && p.value("event").toString() == event && p.value("task_id").toString() == taskId
        && p.value("timestamp").isString() && p.value("data").isObject()
        && (event != "task_state_changed" || validState(p.value("data").toObject().value("state").toString()));
}
QUrl Protocol::endpoint(const QUrl &base, const QString &path)
{
    QUrl url(base);
    url.setPath("/api/v1/tasks" + path);
    return url;
}
