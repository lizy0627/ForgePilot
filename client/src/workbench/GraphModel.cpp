#include "GraphModel.h"
#include <QJsonArray>
#include <QSet>
#include <QPointer>

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
        const auto role = definition.value("settings").toObject().value("recovery_role").toString();
        const auto executionName = (agent.isEmpty() ? handler : agent).toLower();
        const bool writer = role == "writer" || type == "delivery" || role == "delivery"
            || (role.isEmpty() && (executionName.contains("implementer") || executionName.contains("repair")));
        if (status == "awaiting_human" && type == "human_gate" && handler == "execution_approval"
            && !m_recoveryRequired) actions << "approve" << "reject";
        if (graphStatus != "running" && status == "retryable" && !writer && !m_recoveryRequired) actions << "retry";
        if (graphStatus != "running" && (status == "interrupted" || (status == "retryable" && (writer || m_recoveryRequired))))
            actions << "resume";
        row.insert("actions", actions);
        next.append(row);
    }
    const bool metadataChanged = task != m_taskId || revision != m_revision || graphStatus != m_status || currentAgents != m_currentAgents;
    m_snapshot = graph;
    m_taskId = task;
    m_revision = revision;
    m_status = graphStatus;
    m_currentAgents = currentAgents;
    QPointer<GraphModel> guard(this);
    if (!replaceRows(next, "nodeId")) return false;
    if (metadataChanged) emit snapshotChanged();
    return !guard.isNull();
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
    m_revision = 0; m_recoveryRequired = false;
    if (!replaceRows({}, {})) return;
    emit snapshotChanged();
    });
}
