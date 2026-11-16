#include "MissionProtocol.h"
#include "../Protocol.h"
#include <QDateTime>
#include <QJsonArray>
#include <QRegularExpression>
#include <QSet>
#include <limits>

namespace {
bool text(const QJsonValue &v, bool empty = false) { return v.isString() && (empty || !v.toString().isEmpty()); }
bool nullableText(const QJsonValue &v) { return v.isNull() || text(v); }
bool nullableAnyText(const QJsonValue &v) { return v.isNull() || v.isString(); }
bool integer(const QJsonValue &v, qint64 minimum = 0) {
    if (!v.isDouble()) return false;
    const auto n = v.toInteger(std::numeric_limits<qint64>::min());
    return n >= minimum && n <= std::numeric_limits<qint64>::max() - 1 && QJsonValue(n) == v;
}
bool strings(const QJsonValue &v) {
    if (!v.isArray()) return false;
    for (const auto &item : v.toArray()) if (!text(item)) return false;
    return true;
}
bool serverIds(const QJsonValue &value) {
    if (!value.isArray() || value.toArray().size() > 4) return false;
    QSet<QString> ids;
    for (const auto &entry : value.toArray()) {
        if (!entry.isString() || !MissionProtocol::validId(entry.toString()) || ids.contains(entry.toString())) return false;
        ids.insert(entry.toString());
    }
    return true;
}
bool toolContext(const QJsonValue &value) {
    if (!value.isObject()) return false;
    const auto context = value.toObject();
    for (const auto &name : {"rag", "clangd", "mcp"}) {
        if (!context.value(name).isObject()) return false;
        const auto state = context.value(name).toObject();
        if (!state.value("used").isBool() || !strings(state.value("fallback_reasons"))) return false;
        if (QString(name) != "mcp" && !state.value("enabled").isBool()) return false;
        if (QString(name) == "rag" && !state.value("requested").isBool()) return false;
    }
    const auto mcp = context.value("mcp").toObject();
    if (!serverIds(mcp.value("server_ids")) || !mcp.value("servers").isArray()) return false;
    QSet<QString> seen;
    for (const auto &value : mcp.value("servers").toArray()) {
        if (!value.isObject()) return false;
        const auto server = value.toObject();
        const auto id = server.value("server_id").toString();
        if (!mcp.value("server_ids").toArray().contains(id) || seen.contains(id)
            || !text(server.value("name")) || !strings(server.value("tools"))) return false;
        seen.insert(id);
    }
    return seen.size() == mcp.value("server_ids").toArray().size();
}
bool objects(const QJsonValue &v) {
    if (!v.isArray()) return false;
    for (const auto &item : v.toArray()) if (!item.isObject()) return false;
    return true;
}
bool stamp(const QJsonValue &v) { return text(v) && QDateTime::fromString(v.toString(), Qt::ISODateWithMs).isValid(); }
bool oneOf(const QJsonValue &v, const QSet<QString> &choices) { return v.isString() && choices.contains(v.toString()); }
bool only(const QJsonObject &o, const QSet<QString> &fields) {
    for (auto it = o.begin(); it != o.end(); ++it) if (!fields.contains(it.key())) return false;
    return true;
}
bool optional(const QJsonObject &o, const QString &name, bool (*check)(const QJsonValue &)) {
    return !o.contains(name) || check(o.value(name));
}
bool nodeType(const QJsonValue &v) {
    return oneOf(v, {"agent", "deterministic", "parallel_fanout", "join", "human_gate", "conditional", "checkpoint", "delivery"});
}
bool metrics(const QJsonObject &o) {
    for (const auto &key : {"duration_ms", "model_calls", "tool_calls", "total_tokens"}) if (!integer(o.value(key))) return false;
    return true;
}
bool result(const QJsonValue &v) {
    if (v.isNull()) return true;
    if (!v.isObject()) return false;
    const auto o = v.toObject();
    return oneOf(o.value("status"), {"succeeded", "failed", "retryable", "awaiting_human", "cancelled", "interrupted"}) && strings(o.value("output_artifact_ids"))
        && text(o.value("decision_summary"), true) && nullableText(o.value("next_route"))
        && o.value("retryable").isBool() && o.value("metrics").isObject() && metrics(o.value("metrics").toObject())
        && nullableText(o.value("failure_kind")) && nullableAnyText(o.value("error_summary"));
}
bool review(const QJsonValue &v, const QString &id, qint64 revision) {
    if (v.isNull()) return true;
    if (!v.isObject()) return false;
    const auto o = v.toObject();
    return o.value("task_id").toString() == id && MissionProtocol::validRevision(o.value("revision"))
        && o.value("revision").toInteger() <= revision
        && MissionProtocol::validId(o.value("task_spec_artifact_id").toString())
        && MissionProtocol::validId(o.value("plan_artifact_id").toString())
        && MissionProtocol::validHash(o.value("task_spec_hash")) && MissionProtocol::validHash(o.value("plan_hash"))
        && MissionProtocol::validHash(o.value("scope_hash")) && MissionProtocol::validHash(o.value("review_hash"));
}
bool artifactContent(const QString &type, const QJsonObject &o) {
    if (type == "EvidenceBundle") {
        if (!text(o.value("summary")) || !objects(o.value("items"))) return false;
        for (const auto &v : o.value("items").toArray()) {
            const auto e = v.toObject();
            if (!text(e.value("evidence_id")) || !text(e.value("claim")) || !text(e.value("reason"))
                || !oneOf(e.value("source"), {"local_repository", "rag", "external", "user", "approved_mcp", "tool_output"}) || !e.value("confidence").isDouble()
                || e.value("confidence").toDouble() < 0 || e.value("confidence").toDouble() > 1) return false;
            for (const auto &k : {"path", "symbol"}) if (!nullableText(e.value(k))) return false;
            for (const auto &k : {"start_line", "end_line"}) if (!e.value(k).isNull() && !integer(e.value(k), 1)) return false;
            if (!e.value("file_hash").isNull() && !MissionProtocol::validHash(e.value("file_hash"))) return false;
            if (e.value("start_line").isNull() != e.value("end_line").isNull()
                || (!e.value("start_line").isNull() && e.value("end_line").toInteger() < e.value("start_line").toInteger())
                || (e.value("path").isNull() && (!e.value("start_line").isNull() || !e.value("file_hash").isNull()))) return false;
        }
        return true;
    }
    if (type == "ChangeSet") {
        if (!text(o.value("summary")) || !objects(o.value("files"))
            || !MissionProtocol::validId(o.value("execution_plan_artifact_id").toString())
            || !o.value("requires_additional_approval").isBool()) return false;
        for (const auto &v : o.value("files").toArray()) {
            const auto f = v.toObject();
            if (!text(f.value("path")) || !text(f.value("unified_diff"))
                || !oneOf(f.value("change_type"), {"added", "modified", "deleted"})
                || !strings(f.value("plan_step_ids")) || !strings(f.value("acceptance_criteria_ids"))
                || !oneOf(f.value("test_change"), {"none", "new_test", "existing_test", "production_and_test"})) return false;
            for (const auto &k : {"before_sha256", "after_sha256"})
                if (!f.value(k).isNull() && !MissionProtocol::validHash(f.value(k))) return false;
            const auto kind = f.value("change_type").toString();
            if ((kind == "added" && (!f.value("before_sha256").isNull() || f.value("after_sha256").isNull()))
                || (kind == "deleted" && (f.value("before_sha256").isNull() || !f.value("after_sha256").isNull()))
                || (kind == "modified" && (f.value("before_sha256").isNull() || f.value("after_sha256").isNull()))) return false;
        }
        return true;
    }
    if (type == "ValidationReport") {
        if (!text(o.value("toolchain_profile")) || !objects(o.value("commands")) || !text(o.value("summary"))
            || !oneOf(o.value("status"), {"passed", "failed", "cancelled", "timed_out"})
            || !integer(o.value("tests_passed")) || !integer(o.value("tests_failed"))
            || !strings(o.value("compile_errors")) || !strings(o.value("log_artifact_ids"))
            || !oneOf(o.value("hidden_oracle"), {"not_run", "passed", "failed"})
            || !oneOf(o.value("test_integrity"), {"not_checked", "passed", "failed"})
            || !oneOf(o.value("failure_classification"), {"none", "compile", "test", "timeout", "policy", "infrastructure", "cancelled", "unknown", "code_failure", "infrastructure_failure", "policy_failure"})
            || (o.contains("pipeline_stages") && !strings(o.value("pipeline_stages")))) return false;
        for (const auto &key : {"workspace_integrity", "diff_scope"})
            if (o.contains(key) && !oneOf(o.value(key), {"not_checked", "passed", "failed"})) return false;
        bool failedCommand = false;
        for (const auto &v : o.value("commands").toArray()) {
            const auto c = v.toObject();
            if (!oneOf(c.value("kind"), {"configure", "build", "test", "protected_test", "integrity", "custom"}) || !text(c.value("command_summary")) || !stamp(c.value("started_at"))
                || !stamp(c.value("finished_at")) || !integer(c.value("exit_code"), -2147483648LL)
                || c.value("exit_code").toInteger() > 2147483647LL || !text(c.value("output_summary"), true)) return false;
            failedCommand |= c.value("exit_code").toInteger() != 0;
        }
        const bool passed = o.value("status").toString() == "passed";
        if (passed && (failedCommand || o.value("tests_failed").toInteger() || o.value("failure_classification").toString() != "none")) return false;
        if (!passed && o.value("failure_classification").toString() == "none") return false;
        return true;
    }
    if (type == "TaskSpec") {
        if (!text(o.value("title")) || !text(o.value("objective")) || !oneOf(o.value("task_type"), {"bug_fix", "feature", "maintenance"})
            || !o.value("repository").isObject() || !o.value("problem").isObject() || !objects(o.value("acceptance_criteria"))
            || !o.value("allowed_scope").isObject() || !strings(o.value("forbidden_scope"))
            || !o.value("test_policy").isObject() || !o.value("data_policy").isObject()
            || !oneOf(o.value("risk_level"), {"low", "medium", "high", "critical"})
            || !strings(o.value("assumptions")) || (o.contains("risks") && !strings(o.value("risks")))) return false;
        const auto repo = o.value("repository").toObject(), problem = o.value("problem").toObject();
        const auto scope = o.value("allowed_scope").toObject(), tests = o.value("test_policy").toObject(), policy = o.value("data_policy").toObject();
        if (!text(repo.value("base_ref")) || !text(repo.value("toolchain_profile")) || !text(problem.value("observed_behavior"))
            || !text(problem.value("expected_behavior")) || !strings(problem.value("reproduction_steps"))
            || !strings(scope.value("path_patterns")) || scope.value("path_patterns").toArray().isEmpty()
            || !integer(scope.value("max_changed_files"), 1) || scope.value("max_changed_files").toInteger() > 1000
            || !tests.value("existing_tests_read_only").isBool() || !tests.value("allow_new_tests").isBool()
            || !strings(tests.value("modifiable_existing_test_patterns")) || !policy.value("cloud_model_allowed").isBool()
            || !policy.value("external_mcp_allowed").isBool()) return false;
        QSet<QString> ids;
        for (const auto &v : o.value("acceptance_criteria").toArray()) {
            const auto c = v.toObject(); const auto id = c.value("id").toString();
            if (id.isEmpty() || ids.contains(id) || !text(c.value("description"))
                || !oneOf(c.value("verification"), {"build", "test", "ctest", "protected_test", "static_analysis", "inspection", "manual"})) return false;
            ids.insert(id);
        }
        return !ids.isEmpty();
    }
    if (type == "ExecutionPlan") {
        if (!objects(o.value("steps")) || !text(o.value("summary")) || !MissionProtocol::validId(o.value("task_spec_artifact_id").toString())) return false;
        QSet<QString> ids;
        for (const auto &v : o.value("steps").toArray()) {
            const auto s = v.toObject(); const auto id = s.value("step_id").toString();
            if (id.isEmpty() || ids.contains(id) || !text(s.value("owner")) || !strings(s.value("depends_on"))
                || !strings(s.value("expected_files")) || !text(s.value("action")) || !strings(s.value("verification"))
                || !text(s.value("risk")) || !s.value("adds_tests").isBool() || !strings(s.value("acceptance_criteria_ids"))) return false;
            ids.insert(id);
        }
        for (const auto &v : o.value("steps").toArray()) {
            const auto s = v.toObject();
            for (const auto &dep : s.value("depends_on").toArray()) if (!ids.contains(dep.toString()) || dep == s.value("step_id")) return false;
        }
        return !ids.isEmpty();
    }
    if (type == "ReviewReport") return oneOf(o.value("verdict"), {"approve", "request_changes", "reject"})
        && objects(o.value("acceptance_criteria")) && objects(o.value("blocking_issues"))
        && strings(o.value("non_blocking_suggestions")) && o.value("recommend_delivery").isBool() && text(o.value("summary"));
    if (type == "DeliveryBundle") return MissionProtocol::validId(o.value("change_set_artifact_id").toString())
        && MissionProtocol::validId(o.value("validation_report_artifact_id").toString())
        && MissionProtocol::validId(o.value("review_report_artifact_id").toString()) && strings(o.value("changed_files"))
        && text(o.value("build_test_summary")) && text(o.value("reviewer_conclusion")) && text(o.value("usage_instructions"))
        && strings(o.value("risks")) && text(o.value("rollback"));
    return false;
}
bool artifactList(const QJsonValue &v, const QString &id, const QString &type = {}) {
    if (!v.isArray()) return false;
    QSet<QString> ids;
    for (const auto &item : v.toArray()) {
        if (!item.isObject()) return false;
        const auto a = item.toObject();
        const auto artifactId = a.value("artifact_id").toString();
        if (!MissionProtocol::validArtifact(a) || a.value("task_id").toString() != id || ids.contains(artifactId)
            || (!type.isEmpty() && a.value("artifact_type").toString() != type)) return false;
        ids.insert(artifactId);
    }
    return true;
}
bool recovery(const QJsonValue &v, const QString &id, qint64 revision) {
    if (v.isNull()) return true;
    if (!v.isObject()) return false;
    const auto o = v.toObject();
    if (o.value("task_id").toString() != id || !MissionProtocol::validRevision(o.value("revision"))
        || o.value("revision").toInteger() != revision || !nullableText(o.value("blocked_reason"))) return false;
    // A blocked/terminal recovery deliberately carries an empty, unusable token.
    if (!MissionProtocol::validHash(o.value("token"))
        && !(text(o.value("blocked_reason")) && o.value("token").isString() && o.value("token").toString().isEmpty())) return false;
    if ((o.contains("checkpoint_id") && !nullableText(o.value("checkpoint_id")))
        || (o.contains("checks") && !strings(o.value("checks")))
        || (o.contains("requires_confirmation") && !o.value("requires_confirmation").isBool())
        || (o.contains("evidence") && !o.value("evidence").isNull() && !o.value("evidence").isObject())) return false;
    if (o.contains("actions")) {
        if (!objects(o.value("actions"))) return false;
        for (const auto &v : o.value("actions").toArray()) {
            const auto action = v.toObject();
            if (!text(action.value("node_id")) || !text(action.value("action"))
                || (action.contains("checkpoint_id") && !nullableText(action.value("checkpoint_id")))
                || (action.contains("artifact_ids") && !strings(action.value("artifact_ids")))) return false;
        }
    }
    return true;
}
bool delivery(const QJsonValue &v, const QString &id) {
    if (!v.isObject()) return false;
    const auto o = v.toObject();
    if (o.contains("task_id") && o.value("task_id").toString() != id) return false;
    if (o.contains("token")) {
        if (!MissionProtocol::validHash(o.value("token")) || o.value("task_id").toString() != id) return false;
    } else if (!oneOf(o.value("mode"), {"squash", "keep_branch", "patch", "bundle", "abandon"})
        && o.value("status").toString() != "abandoned") return false;
    if (o.contains("mode") && !oneOf(o.value("mode"), {"preview", "squash", "keep_branch", "patch", "bundle", "abandon"})) return false;
    for (const auto &key : {"head_commit", "commit", "target_commit", "target_branch", "branch", "worktree_path", "status", "outcome"})
        if (o.contains(key) && !nullableText(o.value(key))) return false;
    for (const auto &key : {"target_clean", "target_checked_out", "validated"})
        if (o.contains(key) && !o.value(key).isBool()) return false;
    if (o.contains("file_hashes")) {
        if (!o.value("file_hashes").isObject()) return false;
        const auto hashes = o.value("file_hashes").toObject();
        for (auto it = hashes.begin(); it != hashes.end(); ++it) if (it.key().isEmpty() || !MissionProtocol::validHash(it.value())) return false;
    }
    for (const auto &key : {"patch", "bundle", "manifest"}) if (o.contains(key) && !o.value(key).isObject()) return false;
    return true;
}
}

bool MissionProtocol::validId(const QString &id) {
    static const QRegularExpression p("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$");
    return p.match(id).hasMatch();
}
bool MissionProtocol::validHash(const QJsonValue &v) {
    static const QRegularExpression p("^[0-9a-f]{64}$");
    return v.isString() && p.match(v.toString()).hasMatch();
}
bool MissionProtocol::validRevision(const QJsonValue &v) { return integer(v, 1); }
bool MissionProtocol::validState(const QString &s) {
    static const QSet<QString> states{"pending", "ready", "running", "awaiting_human", "succeeded", "failed", "interrupted", "retryable", "skipped", "cancelled", "completed"};
    return states.contains(s);
}
bool MissionProtocol::terminal(const QString &s) {
    return QSet<QString>{"succeeded", "completed", "failed", "cancelled", "skipped"}.contains(s);
}
bool MissionProtocol::validMission(const QJsonObject &o) {
    const auto id = o.value("task_id").toString();
    if (!validId(id) || !validState(o.value("status").toString()) || !validRevision(o.value("current_revision"))
        || !text(o.value("graph_id")) || !integer(o.value("graph_version"), 1)
        || !stamp(o.value("created_at")) || !stamp(o.value("updated_at"))
        || (!o.value("finished_at").isNull() && !stamp(o.value("finished_at")))
        || !o.value("recovery_required").isBool() || !review(o.value("review"), id, o.value("current_revision").toInteger())) return false;
    for (const auto &key : {"repository_path", "request", "task_type", "orchestration_mode", "base_ref", "toolchain_profile"})
        if (!text(o.value(key))) return false;
    if (!optional(o, "use_rag", [](const QJsonValue &v) { return v.isBool(); })
        || !optional(o, "mcp_server_ids", serverIds) || !optional(o, "tool_context", toolContext)) return false;
    return o.value("last_event_id").isNull() || (o.value("last_event_id").isString() && Protocol::sequence(o.value("last_event_id").toString(), id) > 0);
}
bool MissionProtocol::validMissionPage(const QJsonObject &o) {
    if (!objects(o.value("items")) || !integer(o.value("total")) || !nullableText(o.value("next_cursor"))) return false;
    QSet<QString> ids;
    for (const auto &item : o.value("items").toArray()) {
        const auto m = item.toObject(); const auto id = m.value("task_id").toString();
        if (!validId(id) || ids.contains(id) || !validState(m.value("status").toString())
            || !validRevision(m.value("current_revision")) || !stamp(m.value("created_at")) || !stamp(m.value("updated_at"))
            || (!m.value("finished_at").isNull() && !stamp(m.value("finished_at")))
            || !m.value("recovery_required").isBool()) return false;
        for (const auto &key : {"repository_path", "request", "task_type", "orchestration_mode"})
            if (!text(m.value(key))) return false;
        ids.insert(id);
    }
    return o.value("total").toInteger() >= ids.size();
}
bool MissionProtocol::validGraph(const QJsonObject &o) {
    if (!validId(o.value("task_id").toString()) || !text(o.value("graph_id")) || !integer(o.value("graph_version"), 1)
        || !validHash(o.value("definition_hash")) || !validState(o.value("status").toString())
        || !validRevision(o.value("current_revision")) || !objects(o.value("nodes")) || !o.value("definition").isObject()) return false;
    QSet<QString> ids;
    QHash<QString, QJsonValue> nodeTypes;
    for (const auto &v : o.value("nodes").toArray()) {
        const auto n = v.toObject(); const auto id = n.value("node_id").toString();
        if (id.isEmpty() || ids.contains(id) || !nodeType(n.value("node_type")) || !validState(n.value("status").toString())
            || !integer(n.value("attempt")) || !result(n.value("result"))) return false;
        ids.insert(id);
        nodeTypes.insert(id, n.value("node_type"));
    }
    const auto d = o.value("definition").toObject();
    if (d.value("graph_id") != o.value("graph_id") || d.value("version") != o.value("graph_version")
        || !objects(d.value("nodes")) || !objects(d.value("edges"))) return false;
    QSet<QString> definitions;
    for (const auto &v : d.value("nodes").toArray()) {
        const auto n = v.toObject(); const auto id = n.value("node_id").toString();
        if (!ids.contains(id) || definitions.contains(id) || nodeTypes.value(id) != n.value("node_type") || !nodeType(n.value("node_type")) || !text(n.value("handler"))
            || !strings(n.value("dependencies")) || !nullableText(n.value("agent_id")) || !n.value("settings").isObject()
            || (!n.value("agent_definition_hash").isNull() && !validHash(n.value("agent_definition_hash")))) return false;
        definitions.insert(id);
        for (const auto &dep : n.value("dependencies").toArray()) if (!ids.contains(dep.toString()) || dep.toString() == id) return false;
    }
    for (const auto &v : d.value("edges").toArray()) {
        const auto e = v.toObject(); const auto c = e.value("condition").toObject();
        if (!ids.contains(e.value("source").toString()) || !ids.contains(e.value("target").toString())
            || !e.value("condition").isObject() || !nullableText(c.value("route")) || !strings(c.value("statuses"))) return false;
        for (const auto &s : c.value("statuses").toArray()) if (!validState(s.toString())) return false;
    }
    return definitions == ids;
}
bool MissionProtocol::validArtifact(const QJsonObject &o) {
    if (!validId(o.value("artifact_id").toString()) || !validId(o.value("task_id").toString())
        || o.value("schema_version").toString() != "1.0" || !text(o.value("producer")) || !text(o.value("producer_node"))
        || !stamp(o.value("created_at")) || !validHash(o.value("content_sha256"))
        || !strings(o.value("parent_artifact_ids")) || !o.value("content").isObject()) return false;
    for (const auto &id : o.value("parent_artifact_ids").toArray()) if (!validId(id.toString())) return false;
    return artifactContent(o.value("artifact_type").toString(), o.value("content").toObject());
}
bool MissionProtocol::validArtifacts(const QJsonObject &o) {
    const auto id = o.value("task_id").toString();
    return validId(id) && artifactList(o.value("items"), id);
}
bool MissionProtocol::validDiff(const QJsonObject &o) {
    const auto id = o.value("task_id").toString();
    return validId(id) && text(o.value("patch"), true) && artifactList(o.value("change_sets"), id, "ChangeSet");
}
bool MissionProtocol::validValidation(const QJsonObject &o) {
    const auto id = o.value("task_id").toString();
    if (!validId(id) || !artifactList(o.value("items"), id, "ValidationReport")) return false;
    const auto items = o.value("items").toArray();
    return items.isEmpty() ? o.value("latest").isNull() : o.value("latest") == items.last();
}
bool MissionProtocol::validEvent(const QJsonObject &o, const QString &event, const QString &id) {
    static const QRegularExpression p("^(task|node|artifact|approval|tool|model|validation|workspace|security)\\.[a-z][a-z0-9_.]{0,95}$");
    if (!validId(id) || o.value("task_id").toString() != id || o.value("event_type").toString() != event
        || !p.match(event).hasMatch() || !validRevision(o.value("task_revision")) || !stamp(o.value("timestamp"))
        || !text(o.value("event_id")) || Protocol::sequence(o.value("event_id").toString(), id) < 1
        || !nullableText(o.value("node_id")) || !nullableText(o.value("actor")) || !o.value("data").isObject()) return false;
    const auto d = o.value("data").toObject();
    if (d.contains("state") && !validState(d.value("state").toString())) return false;
    for (const auto &key : {"duration_ms", "model_calls", "tool_calls", "total_tokens", "input_tokens", "output_tokens", "attempt"})
        if (d.contains(key) && !integer(d.value(key))) return false;
    return (!d.contains("metrics") || (d.value("metrics").isObject() && metrics(d.value("metrics").toObject())))
        && (!d.contains("artifact_ids") || strings(d.value("artifact_ids")));
}
bool MissionProtocol::validResponse(const QString &op, const QString &id, const QJsonObject &o) {
    if (op == "mission.discard_preview") return o.value("discarded").isBool() && o.value("discarded").toBool();
    if (op == "mission.preview") return validHash(o.value("preview_token"))
        && text(o.value("base_commit")) && text(o.value("source_head"))
        && strings(o.value("baseline_paths")) && strings(o.value("changed_files"))
        && o.value("patch").isString() && o.value("build").isObject();
    if (op == "mission.list") return validMissionPage(o);
    if (op == "mission.resume" || op == "mission.delivery") {
        if (!o.value("mission").isObject() || !validMission(o.value("mission").toObject())
            || o.value("mission").toObject().value("task_id").toString() != id) return false;
        return op == "mission.resume" ? recovery(o.value("recovery"), id, o.value("mission").toObject().value("current_revision").toInteger())
                                      : delivery(o.value("delivery"), id);
    }
    if (op != "mission.create" && o.value("task_id").toString() != id) return false;
    if (op == "mission.graph") return validGraph(o);
    if (op == "mission.artifacts") return validArtifacts(o);
    if (op == "mission.artifact") return validArtifact(o);
    if (op == "mission.diff") return validDiff(o);
    if (op == "mission.validation") return validValidation(o);
    return validMission(o);
}
bool MissionProtocol::validRequest(const QString &op, const QJsonObject &o) {
    if (op == "mission.list") return only(o, {"before", "repository_path", "status"})
        && o.value("before").isString() && o.value("repository_path").isString()
        && o.value("status").isString() && (o.value("status").toString().isEmpty() || validState(o.value("status").toString()));
    if (op == "mission.discard_preview") return only(o, {"preview_token"}) && validHash(o.value("preview_token"));
    if (op == "mission.preview") return only(o, {"repository_path", "base_ref", "baseline_paths", "configure_preset", "build_preset", "test_preset"})
        && text(o.value("repository_path")) && (!o.contains("base_ref") || text(o.value("base_ref")))
        && (!o.contains("baseline_paths") || strings(o.value("baseline_paths")))
        && (!o.contains("configure_preset") || text(o.value("configure_preset")))
        && (!o.contains("build_preset") || text(o.value("build_preset")))
        && (!o.contains("test_preset") || text(o.value("test_preset")));
    if (op == "mission.create") return only(o, {"repository_path", "request", "task_type", "orchestration_mode", "base_ref", "toolchain_profile",
            "baseline_paths", "configure_preset", "build_preset", "test_preset", "preview_token", "baseline_confirmed", "mcp_server_ids", "use_rag"})
        && text(o.value("repository_path")) && text(o.value("request"))
        && (!o.contains("mcp_server_ids") || serverIds(o.value("mcp_server_ids")))
        && (!o.contains("use_rag") || o.value("use_rag").isBool())
        && (!o.contains("task_type") || oneOf(o.value("task_type"), {"auto", "bug_fix", "feature"}))
        && (!o.contains("orchestration_mode") || o.value("orchestration_mode").toString() == "multi_agent")
        && (!o.contains("base_ref") || text(o.value("base_ref"))) && (!o.contains("toolchain_profile") || text(o.value("toolchain_profile")))
        && (!o.contains("baseline_paths") || strings(o.value("baseline_paths")))
        && (!o.contains("preview_token") || validHash(o.value("preview_token")))
        && (!o.contains("baseline_confirmed") || o.value("baseline_confirmed").isBool())
        && (!o.contains("configure_preset") || text(o.value("configure_preset")))
        && (!o.contains("build_preset") || text(o.value("build_preset")))
        && (!o.contains("test_preset") || text(o.value("test_preset")));
    if (op == "mission.get" || op == "mission.graph" || op == "mission.artifacts" || op == "mission.diff" || op == "mission.validation") return o.isEmpty();
    if (op == "mission.artifact") return only(o, {"artifact_id"}) && validId(o.value("artifact_id").toString());
    if (!validRevision(o.value("expected_revision"))) return false;
    if (op == "mission.approve") return only(o, {"expected_revision", "approval_type", "task_spec_hash", "plan_hash", "scope_hash", "decision"})
        && (!o.contains("approval_type") || o.value("approval_type").toString() == "execution")
        && validHash(o.value("task_spec_hash")) && validHash(o.value("plan_hash")) && validHash(o.value("scope_hash"))
        && oneOf(o.value("decision"), {"approved", "rejected"});
    if (op == "mission.cancel") return only(o, {"expected_revision"});
    if (op == "mission.retry") {
        static const QRegularExpression p("^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$");
        return only(o, {"expected_revision", "node_id"}) && (!o.contains("node_id") || (text(o.value("node_id")) && p.match(o.value("node_id").toString()).hasMatch()));
    }
    if (op == "mission.resume") return only(o, {"expected_revision", "recovery_token"}) && optional(o, "recovery_token", validHash);
    if (op == "mission.delivery") return only(o, {"expected_revision", "mode", "token", "target_branch"})
        && (!o.contains("mode") || oneOf(o.value("mode"), {"preview", "squash", "keep_branch", "patch", "bundle", "abandon"}))
        && optional(o, "token", validHash) && (!o.contains("target_branch") || text(o.value("target_branch")));
    return false;
}
bool MissionProtocol::validError(const QJsonObject &o, const QString &id) {
    if (!o.value("error").isObject()) return false;
    const auto e = o.value("error").toObject();
    return text(e.value("code")) && text(e.value("message")) && objects(e.value("details"))
        && (e.value("task_id").isNull() || (text(e.value("task_id")) && (id.isEmpty() || e.value("task_id").toString() == id)));
}
bool MissionProtocol::staleError(const QString &code) {
    return QSet<QString>{"revision_conflict", "mission_revision_conflict", "approval_conflict", "mission_approval_conflict", "approval_stale", "recovery_conflict", "recovery_stale", "delivery_changed"}.contains(code);
}
QString MissionProtocol::errorMessage(const QString &code, int status) {
    if (staleError(code)) return "任务版本或审批内容已变化，请刷新并重新审阅后提交。旧审批不会自动重试。";
    if (code == "invalid_response") return "后端响应格式无效，请刷新任务；已发送的操作可能已经执行。";
    if (code == "invalid_request" || code == "invalid_task_id") return "任务请求无效，请检查任务标识、版本和必填内容。";
    if (code == "response_limit") return "响应超过 64 MiB 上限，请缩小查询范围。";
    if (code == "network_error") return "连接后端失败或响应不完整；请刷新任务状态后再决定是否重试。";
    if (code == "request_cancelled") return "请求已取消。";
    if (code == "unknown_mission" || code == "unknown_graph" || status == 404) return "任务或产物不存在，请检查任务标识并刷新状态。";
    if (status == 403) return "操作被权限或安全策略拒绝，请检查审批和允许范围。";
    if (code == "recovery_required" || code == "recovery_stale") return "任务需要恢复确认，请先刷新并检查恢复预览。";
    if (code == "mission_frozen_config_missing") return "该历史任务缺少冻结的源码基线或构建配置，无法安全恢复。请重新创建任务并审批执行方案；历史记录仍可查看。";
    if (code == "build_selection_changed") return "冻结的预设文件、构建配置或工具身份已变化，当前任务无法继续执行或恢复。请重新创建任务并审批执行方案；历史记录仍可查看。";
    if (code == "graph_definition") return "任务冻结流程已变化，请重新创建任务并审批执行方案；历史记录仍可查看。";
    if (status == 409) return "任务状态已变化，当前操作无法执行，请刷新后检查。";
    if (status == 429 || code == "task_capacity") return "后端繁忙，请稍后刷新。";
    if (status == 503 || code == "mission_unavailable") return "任务服务暂不可用，请检查后端配置并稍后刷新。";
    if (status == 422 || status == 400 || status == 413 || status == 415) return "后端拒绝了请求，请检查输入和当前任务版本。";
    return "任务请求失败，请刷新状态并检查后端日志。";
}
QUrl MissionProtocol::endpoint(const QUrl &base, const QString &path) {
    auto url = base; url.setPath("/api/v2/missions" + path); return url;
}
