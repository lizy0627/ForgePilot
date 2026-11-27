#include "GraphModel.h"
#include "MissionProtocol.h"
#include <QJsonArray>
#include <QSet>
#include <QPointer>

namespace {
QStringList legacyRetryActions(const QJsonObject &definition, const QString &status,
                               const QString &graphStatus, bool recoveryRequired)
{
    if (graphStatus != "retryable" && graphStatus != "interrupted") return {};
    if (status == "interrupted") return {"resume"};
    if (status != "retryable") return {};
    if (graphStatus == "interrupted" || recoveryRequired) return {"resume"};
    const auto role = definition.value("settings").toObject().value("recovery_role").toString();
    if (!role.isEmpty()) return {role == "writer" || role == "delivery" ? "resume" : "retry"};
    const auto handler = definition.value("handler").toString();
    const auto name = definition.value("agent_id").toString(handler).toLower();
    const bool writer = name.contains("implementer") || name.contains("repair")
        || (name == "generalist.v1" && (definition.value("node_id") == "implementer" || handler == "repair.v1"));
    return {writer || definition.value("node_type") == "delivery" ? "resume" : "retry"};
}
}

GraphModel::GraphModel(QObject *parent)
    : WorkbenchListModel({{NodeIdRole,"nodeId"},{NodeTypeRole,"nodeType"},{StatusRole,"status"},
        {AttemptRole,"attempt"},{DependenciesRole,"dependencies"},{AgentIdRole,"agentId"},
        {CurrentAgentRole,"currentAgent"},{MetricsRole,"metrics"},{ActionsRole,"actions"},
        {ResultRole,"result"}}, parent) {}

void GraphModel::setSnapshot(const QJsonObject &graph)
{ runUpdate([this, graph](quint64) { setSnapshotNow(graph); }); }

bool GraphModel::setSnapshotNow(const QJsonObject &graph)
{
    const QString task = graph.value("task_id").toString();
    const qint64 revision = graph.value("current_revision").toInteger();
    if (task.isEmpty() || revision < 1 || !graph.value("nodes").isArray()
        || !graph.value("definition").isObject() || (task == m_taskId && revision < m_revision)) return false;
    QHash<QString,QJsonObject> definitions;
    for (const auto &entry : graph.value("definition").toObject().value("nodes").toArray()) {
        const auto definition = entry.toObject();
        definitions.insert(definition.value("node_id").toString(), definition);
    }
    QList<QVariantMap> next;
    QStringList currentAgents;
    bool hasResumeAction = false;
    QSet<QString> ids;
    const QString graphStatus = graph.value("status").toString();
    for (const auto &entry : graph.value("nodes").toArray()) {
        if (!entry.isObject()) return false;
        const auto node = entry.toObject();
        const auto id = node.value("node_id").toString();
        if (id.isEmpty() || ids.contains(id) || !definitions.contains(id)) return false;
        ids.insert(id);
        const auto definition = definitions.value(id);
        const auto status = node.value("status").toString();
        const auto type = node.value("node_type").toString();
        const auto agent = definition.value("agent_id").toString();
        const auto result = node.value("result").toObject();
        auto row = node.toVariantMap();
        row.insert("nodeId", id);
        row.insert("nodeType", type);
        row.insert("dependencies", definition.value("dependencies").toArray().toVariantList());
        row.insert("agentId", agent);
        row.insert("currentAgent", status == "running" ? agent : QString{});
        if (status == "running" && !agent.isEmpty() && !currentAgents.contains(agent)) currentAgents.append(agent);
        QVariantMap metrics{{"duration_ms",0},{"model_calls",0},{"tool_calls",0},{"total_tokens",0}};
        const auto reported = result.value("metrics").toObject().toVariantMap();
        for (auto it = reported.cbegin(); it != reported.cend(); ++it) metrics.insert(it.key(), it.value());
        row.insert("metrics", metrics);
        // Affordances only: every command is still revision-checked by the backend.
        QStringList actions;
        const auto handler = definition.value("handler").toString();
        const auto pendingAction = MissionProtocol::pendingAction(node, handler);
        row.insert("pendingAction", pendingAction);
        if (pendingAction == "execution" && !m_recoveryRequired) actions << "approve" << "reject";
        const auto advertised = node.value("retry_actions");
        actions += advertised.isUndefined() ? legacyRetryActions(definition, status, graphStatus, m_recoveryRequired)
                                            : advertised.toVariant().toStringList();
        hasResumeAction |= actions.contains("resume");
        row.insert("actions", actions);
        next.append(row);
    }
    const bool metadataChanged = task != m_taskId || revision != m_revision || graphStatus != m_status
        || currentAgents != m_currentAgents || hasResumeAction != m_hasResumeAction;
    m_snapshot = graph;
    m_taskId = task;
    m_revision = revision;
    m_status = graphStatus;
    m_currentAgents = currentAgents;
    m_hasResumeAction = hasResumeAction;
    QPointer<GraphModel> guard(this);
    if (!replaceRows(next, "nodeId")) return false;
    if (metadataChanged) emit snapshotChanged();
    return !guard.isNull();
}

bool GraphModel::allowsRetry(const QString &nodeId) const
{
    for (const auto &row : rows()) {
        if (row.value("nodeId") == nodeId && row.contains("retry_actions"))
            return row.value("actions").toStringList().contains("retry");
    }
    // Older backends resolve the optional node ID and check its state on POST.
    return !m_recoveryRequired;
}

void GraphModel::setRecoveryRequired(bool required)
{
    runUpdate([this, required](quint64 epoch) {
    if (m_recoveryRequired == required) return;
    m_recoveryRequired = required;
    QPointer<GraphModel> guard(this);
    const auto snapshot = m_snapshot;
    if (!snapshot.isEmpty() && !setSnapshotNow(snapshot)) return;
    if (guard && currentUpdate(epoch)) emit snapshotChanged();
    });
}

void GraphModel::clear()
{
    runUpdate([this](quint64) {
    if (m_taskId.isEmpty() && count() == 0 && !m_recoveryRequired) return;
    m_snapshot = {};
    m_taskId.clear(); m_status.clear(); m_currentAgents.clear();
    m_revision = 0; m_recoveryRequired = false; m_hasResumeAction = false;
    if (!replaceRows({}, {})) return;
    emit snapshotChanged();
    });
}
