.pragma library
var labels = {pending:"待执行", running:"执行中", awaiting_human:"等待确认", succeeded:"已成功", failed:"失败", interrupted:"已中断", skipped:"已跳过", retryable:"可重试", cancelled:"已取消"};
var icons = {pending:"○", running:"▶", awaiting_human:"!", succeeded:"✓", failed:"×", interrupted:"!", skipped:"–", retryable:"↻", cancelled:"–"};
var colors = {pending:"#7a7d84", running:"#2866b5", awaiting_human:"#90621a", succeeded:"#268349", failed:"#b42332", interrupted:"#cb6d16", skipped:"#a5a7ac", retryable:"#90621a", cancelled:"#7a7d84"};
function label(status) { return labels[status] || (status ? "状态：" + status : "未开始"); }
function icon(status) { return icons[status] || "•"; }
function color(status) { return colors[status] || "#62646a"; }
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
