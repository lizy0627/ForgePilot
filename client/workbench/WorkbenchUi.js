.pragma library
.import "../panels/Theme.js" as Theme
var labels = {ready:"已就绪", pending:"待执行", running:"执行中", awaiting_human:"等待确认", succeeded:"已成功", failed:"失败", interrupted:"已中断", skipped:"已跳过", retryable:"可重试", cancelled:"已取消"};
var colors = {ready:Theme.muted, pending:Theme.muted, running:Theme.accent, awaiting_human:Theme.warning,
              succeeded:Theme.success, failed:Theme.danger, interrupted:Theme.warning,
              skipped:Theme.disabledText, retryable:Theme.purple, cancelled:Theme.muted};
function label(status) { return labels[status] || (status ? "状态：" + status : "未开始"); }
function color(status) { return colors[status] || Theme.secondary; }
function appendActivity(events, event) {
    return events.slice(-19).concat([{
        event_type: event.event_type, node_id: event.node_id, timestamp: event.timestamp,
        state: (event.data && event.data.state) || event.state || ""
    }]);
}
function artifact(model, type) {
    if (!model) return {};
    var count = model.count;
    for (var i = count - 1; i >= 0; --i) {
        var item = model.get(i);
        if (item.artifactType === type) return item;
    }
    return {};
}
function artifactById(model, id) {
    if (!model || !id) return {};
    var count = model.count;
    for (var i = 0; i < count; ++i) {
        var item = model.get(i);
        if (item.artifactId === id) return item;
    }
    return {};
}
function lines(list) { return list && list.length ? list.join("、") : "无"; }
function state(mission, hasData) {
    if (!mission || !mission.taskId) return "empty";
    if (mission.error && !mission.synchronized) return "error";
    if (mission.connectionState === "reconnecting" || mission.connectionState === "recovering" || mission.connectionState === "error") return "degraded";
    if (!mission.synchronized) return "loading";
    if (mission.status === "interrupted" || mission.snapshot.recovery_required) return "interrupted";
    if (mission.status === "awaiting_human") return "awaiting_human";
    if (["succeeded", "failed", "cancelled"].indexOf(mission.status) >= 0) return "completed";
    return hasData ? "ready" : "empty";
}
function stateText(state, emptyText) {
    var text = {loading:"正在加载当前任务…", error:"读取失败，请刷新任务。", degraded:"连接中断，正在重新同步；操作暂不可用。", interrupted:"任务已中断，请检查恢复预览。", awaiting_human:"等待人工确认当前方案。", completed:"任务已结束。"};
    return state === "empty" ? emptyText : (text[state] || "");
}
