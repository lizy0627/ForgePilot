.pragma library

// Presentation only: records and their backend states are never changed here.
var stageDefinitions = [
    {id: "requirement", title: "需求", description: "明确目标、验收条件和允许修改的范围。"},
    {id: "analysis", title: "分析", description: "并行分析根因、影响与测试，汇总代码证据。"},
    {id: "plan", title: "计划", description: "根据需求和证据生成执行方案。"},
    {id: "approval", title: "审批", description: "核对冻结的需求、计划与范围后人工决定。"},
    {id: "baseline", title: "基线", description: "批准后采集构建、测试及工作区完整性基线。"},
    {id: "implementation", title: "实现", description: "在隔离工作区内按批准的范围修改代码。"},
    {id: "verification", title: "验证与修复", description: "运行验证和独立审查；发现问题时按轮次修复。"},
    {id: "delivery", title: "交付", description: "准备交付结果，预览后由用户确认最终操作。"}
];

var statusLabels = {
    pending: "待执行", ready: "待执行", running: "执行中", awaiting_human: "等待确认",
    succeeded: "已完成", failed: "执行失败", interrupted: "已中断", retryable: "等待重试",
    skipped: "已跳过", cancelled: "已取消", needs_repair: "需要修复"
};

function list(model) {
    if (!model) return [];
    var result = [];
    var count = typeof model.count === "number" ? model.count : model.length;
    for (var i = 0; i < (count || 0); ++i)
        result.push(typeof model.get === "function" ? model.get(i) : model[i]);
    return result;
}

function nodes(graph) { return list(graph); }
function idOf(record) { return record ? (record.nodeId || record.node_id || "") : ""; }
function artifactId(record) { return record.artifactId || record.artifact_id || ""; }
function artifactType(record) { return record.artifactType || record.artifact_type || ""; }

function stageId(record) {
    var id = idOf(record);
    if (id === "draft_task_spec") return "requirement";
    if (["analyst_fanout", "root_cause_analysis", "impact_test_analysis", "join_evidence", "generalist_analysis"].indexOf(id) >= 0) return "analysis";
    if (id === "generate_plan" || id === "generalist_plan") return "plan";
    if (id === "execution_approval") return "approval";
    if (id === "baseline_capture") return "baseline";
    if (id === "implementer") return "implementation";
    if (/^(validation|reviewer|repair)_\d+$/.test(id)) return "verification";
    if (id === "delivery_bundle") return "delivery";
    return "other";
}

function nodeTitle(record) {
    var id = idOf(record);
    var titles = {
        draft_task_spec: "明确任务需求", analyst_fanout: "分配分析任务",
        root_cause_analysis: "分析问题根因", impact_test_analysis: "分析影响与测试",
        join_evidence: "汇总分析证据", generate_plan: "制定执行计划",
        generalist_analysis: "分析任务证据", generalist_plan: "制定执行计划",
        execution_approval: "确认执行方案", baseline_capture: "采集执行基线",
        implementer: "实现代码修改", delivery_bundle: "准备交付结果"
    };
    if (titles[id]) return titles[id];
    var round = /^(validation|reviewer|repair)_(\d+)$/.exec(id);
    if (round) {
        var number = Number(round[2]);
        if (round[1] === "repair") return "第 " + number + " 轮修复";
        var title = round[1] === "validation" ? "构建与测试" : "独立代码审查";
        return number === 0 ? title : "修复 " + number + " 后" + title;
    }
    return id || "未命名节点";
}

function nodeDescription(record) {
    var id = idOf(record);
    var descriptions = {
        draft_task_spec: "将请求整理为目标、验收条件、修改范围和风险。",
        analyst_fanout: "将只读分析分配给根因分析和影响测试分析节点。",
        root_cause_analysis: "读取仓库代码，定位根因并给出可核查的证据。",
        impact_test_analysis: "检查改动影响、兼容性和需要验证的测试。",
        generalist_analysis: "读取仓库，分析根因、影响和测试证据。",
        join_evidence: "合并分析结果，核对证据位置和文件版本。",
        generate_plan: "结合需求和证据生成步骤、范围、风险及验证方式。",
        generalist_plan: "结合任务证据生成执行步骤与验证方式。",
        execution_approval: "显示当前冻结方案；只有人工批准后才能采集基线和修改代码。",
        baseline_capture: "确定性采集 CMake、CTest 和完整性基线；此节点不生成独立报告产物。",
        implementer: "在隔离工作区按批准范围实现修改，记录实际代码变更。",
        delivery_bundle: "汇总已验证的变更和审查结论；最终交付仍需用户预览并确认。"
    };
    if (descriptions[id]) return descriptions[id];
    if (/^validation_\d+$/.test(id)) return "运行构建、测试、范围和完整性检查；结果决定进入审查或修复。";
    if (/^reviewer_\d+$/.test(id)) return "独立审查本轮变更与验收条件，决定是否建议交付。";
    if (/^repair_\d+$/.test(id)) return "根据上一轮验证或审查问题，在批准范围内修复；共享最多三轮修复预算。";
    return "保留原始节点状态和执行结果，可在详情中核对。";
}

function artifactsForNode(artifacts, record) {
    if (!record) return [];
    var all = list(artifacts);
    var ids = record.result ? (record.result.output_artifact_ids || []) : [];
    var found = [];
    if (ids.length) {
        // A missing referenced output stays missing until synchronization catches up.
        // Falling back here could display a report from a previous attempt.
        for (var i = 0; i < ids.length; ++i)
            for (var j = 0; j < all.length; ++j)
                if (artifactId(all[j]) === ids[i]) { found.push(all[j]); break; }
        return found;
    }
    var id = idOf(record);
    if (!id) return [];
    for (var k = 0; k < all.length; ++k)
        if ((all[k].producerNode || all[k].producer_node) === id) found.push(all[k]);
    return found;
}

function visualStatus(record, artifacts) {
    var systemStatus = (record && record.status) || "pending";
    var needsRepair = false;
    var description = record && record.result ? (record.result.decision_summary || record.result.error_summary || "") : "";
    if (systemStatus === "succeeded") {
        needsRepair = !!record.result && record.result.next_route === "repair";
        var outputs = artifactsForNode(artifacts, record);
        for (var i = 0; i < outputs.length; ++i) {
            var type = artifactType(outputs[i]);
            var content = outputs[i].content || {};
            if (type === "ValidationReport") {
                if ((content.status && content.status !== "passed")
                        || (content.failure_classification && content.failure_classification !== "none")
                        || Number(content.tests_failed || 0) > 0 || content.tests_passed === 0) needsRepair = true;
                var checks = ["workspace_integrity", "test_integrity", "diff_scope"];
                for (var check = 0; check < checks.length; ++check)
                    if (content[checks[check]] && content[checks[check]] !== "passed") needsRepair = true;
                if (content.hidden_oracle && ["passed", "not_run"].indexOf(content.hidden_oracle) < 0) needsRepair = true;
                var commands = content.commands || [];
                for (var command = 0; command < commands.length; ++command)
                    if (typeof commands[command].exit_code === "number" && commands[command].exit_code !== 0) needsRepair = true;
            }
            if (type === "ReviewReport") {
                if ((content.verdict && content.verdict !== "approve") || content.recommend_delivery === false
                        || (content.blocking_issues || []).length) needsRepair = true;
                var criteria = content.acceptance_criteria || [];
                for (var c = 0; c < criteria.length; ++c)
                    if (criteria[c].status && criteria[c].status !== "met") needsRepair = true;
            }
        }
    }
    var status = needsRepair ? "needs_repair" : systemStatus;
    if (needsRepair) description = "节点执行完成，本轮结果要求修复。";
    return {status: status, label: statusLabels[status] || ("状态：" + status),
            description: description, needsRepair: needsRepair, systemStatus: systemStatus};
}

function executed(record) {
    // Unused conditional branches are marked skipped/cancelled without running.
    if (record.status === "skipped") return false;
    return Number(record.attempt || 0) > 0 || ["running", "awaiting_human", "succeeded", "failed", "interrupted", "retryable"].indexOf(record.status) >= 0;
}

function currentRecord(records) {
    var priority = ["running", "awaiting_human", "retryable", "interrupted", "failed", "ready"];
    for (var p = 0; p < priority.length; ++p)
        for (var i = 0; i < records.length; ++i)
            if (records[i].status === priority[p]) return records[i];
    for (var last = records.length - 1; last >= 0; --last)
        if (executed(records[last])) return records[last];
    for (var first = 0; first < records.length; ++first)
        if (records[first].status === "pending") return records[first];
    return records.length ? records[0] : null;
}

function activeNodeId(graph) { return idOf(currentRecord(nodes(graph))); }

function buildStages(graph, artifacts) {
    var records = nodes(graph);
    if (!records.length) return [];
    var stages = [];
    for (var s = 0; s < stageDefinitions.length; ++s) {
        var definition = stageDefinitions[s];
        stages.push({id: definition.id, title: definition.title, nodes: [], status: "pending",
                     currentNodeId: "", summary: definition.description});
    }
    for (var n = 0; n < records.length; ++n) {
        var key = stageId(records[n]);
        var stage = null;
        for (var f = 0; f < stages.length; ++f) if (stages[f].id === key) stage = stages[f];
        if (!stage) {
            stage = {id: "other", title: "其他节点", nodes: [], status: "pending", currentNodeId: "", summary: "保留工作流中的其他节点。"};
            stages.push(stage);
        }
        stage.nodes.push(records[n]);
    }
    for (var j = 0; j < stages.length; ++j) {
        var item = stages[j];
        var current = currentRecord(item.nodes);
        item.currentNodeId = idOf(current);
        if (!current) continue;
        var visual = visualStatus(current, artifacts);
        item.status = visual.status;
        var count = 0;
        var pending = false;
        for (var r = 0; r < item.nodes.length; ++r) {
            if (executed(item.nodes[r])) ++count;
            if (item.nodes[r].status === "pending" || item.nodes[r].status === "ready") pending = true;
        }
        if (item.status === "succeeded" && pending) {
            // Pending future Repair branches do not make an approving review incomplete.
            var approvedReview = /^reviewer_\d+$/.test(idOf(current)) && current.result && current.result.next_route === "deliver";
            if (item.id !== "verification" || !approvedReview) item.status = "running";
        }
        item.summary = count ? "已执行 " + count + " 个节点 · " + nodeTitle(current) + "：" + (statusLabels[item.status] || item.status) : item.summary;
        if (visual.needsRepair) item.summary += "，本轮结果要求修复";
    }
    return stages;
}
