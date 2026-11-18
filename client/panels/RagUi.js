.pragma library
.import "Theme.js" as Theme

function statusLabel(status, reachable, statusReadFailed, hasRepository, loading) {
    if (hasRepository && !reachable && loading && !statusReadFailed) return "正在读取";
    if (hasRepository && (!reachable || statusReadFailed)) return "状态不可用";
    var labels = {
        disabled: "未启用", unavailable: "不可用", empty: "尚未索引",
        pending: "待更新", indexing: "索引中", cancelling: "正在取消",
        ready: "已就绪", degraded: "降级可用", failed: "索引失败",
        cancelled: "已取消"
    };
    return labels[status] || "状态未知";
}

function statusColor(status, reachable, statusReadFailed, hasRepository) {
    if (hasRepository && (!reachable || statusReadFailed)) return Theme.warning;
    if (status === "ready") return Theme.accent;
    if (status === "failed") return Theme.danger;
    if (status === "degraded" || status === "pending" || status === "cancelling") return Theme.warning;
    return Theme.secondary;
}

function safeReason(code) {
    var reasons = {
        disabled: "后端未启用代码 RAG。",
        not_configured: "Embedding 配置不完整。",
        invalid_configuration: "RAG 配置无效。",
        rag_secret_unavailable: "Embedding 凭据不可用。",
        rag_index_interrupted: "后端重启中断了上一次索引，可重新构建。",
        rag_index_failed: "索引任务未能完成，可重试。",
        rag_index_file_failed: "部分文件未能索引，检索会使用可用来源。",
        qdrant_unavailable: "向量存储不可用，检索会降级。",
        vector_unavailable: "本地向量存储不可用，检索会使用可用的其他来源。",
        rag_vector_unavailable: "本地向量存储不可用，检索会使用可用的其他来源。",
        embedding_unavailable: "Embedding 服务不可用，检索会使用可用的其他来源。",
        initialization_failed: "Embedding 初始化失败，检索会使用可用的其他来源。"
    };
    return reasons[code] || "";
}

function statusDetail(status, errorCode, reachable, statusReadFailed, hasRepository, loading) {
    if (hasRepository === false) return "请先为当前对话选择项目。";
    if (hasRepository && !reachable && loading && !statusReadFailed) return "正在读取代码索引状态…";
    if (!reachable) return "无法读取后端状态；会自动重连，原任务流程不受影响。";
    if (statusReadFailed) return "代码索引状态查询失败，请检查本机后端并刷新；原任务流程不受影响。";
    var reason = safeReason(errorCode);
    if (reason) return reason;
    var details = {
        disabled: "后端未启用代码 RAG；请在后端启动参数中加入 --enable-rag 并重启后端。",
        unavailable: "索引依赖当前不可用；原任务流程仍可继续。",
        empty: "当前项目还没有代码索引。",
        pending: "项目文件有变化，建议更新索引。",
        indexing: "正在建立当前项目的检索索引。",
        cancelling: "已请求取消，正在等待当前批次安全停止。",
        ready: "当前项目的代码索引可用于新任务。",
        degraded: "部分索引或检索来源不可用，将使用其余来源继续。",
        failed: "索引未完成；可重试，原任务流程仍可继续。",
        cancelled: "索引已取消；保留已成功写入的可重试状态。"
    };
    return details[status] || "后端返回了无法识别的索引状态。";
}

function taskUsage(requested, used, status) {
    if (!requested || status === "not_requested") return "本次未使用 RAG（发送时开关已关闭）";
    if (used) return status === "degraded" ? "本次已使用 RAG（部分来源降级）" : "本次已使用 RAG";
    if (status === "degraded") return "本次未使用 RAG（检索失败，已降级）";
    if (status === "unavailable") return "本次未使用 RAG（后端不可用）";
    if (status === "available") return "本次尚未使用 RAG";
    return "本次未使用 RAG";
}
