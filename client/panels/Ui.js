.pragma library
.import "Theme.js" as Theme
function status(s) {
    if(s === "interrupted") return "未完成";
    var labels = {created:"已创建",analyzing:"分析中",planning:"规划中",waiting_approval:"等待审批",modifying:"修改中",building:"构建中",testing:"测试中",reflecting:"失败原因分析中",completed:"已完成",failed:"失败",cancelled:"已取消",succeeded:"通过",passed:"通过",running:"运行中",not_run:"未运行",no_tests:"无测试",skipped:"已跳过",connected:"已连接",connecting:"连接中",reconnecting:"重连中",disconnected:"未连接",ended:"事件流已结束",error:"连接失败",checking:"检查中",timeout:"已超时",timed_out:"已超时"};
    return labels[s] || (s ? "未知状态（" + s + "）" : "未开始");
}
function color(s) {
    if (["completed","succeeded","passed","connected"].indexOf(s)>=0) return Theme.success;
    if (["failed","error","timeout","timed_out"].indexOf(s)>=0) return Theme.danger;
    if (["waiting_approval","no_tests","reconnecting","reflecting"].indexOf(s)>=0) return Theme.warning;
    return Theme.secondary;
}
function eventKind(s) {
    var labels = {task_finished:"任务结束",task_error:"任务错误",task_waiting:"等待审批",plan_feedback:"方案修改意见",plan_approved:"方案已批准",plan_rejected:"方案已拒绝",review_cancelled:"审批已取消",build_selection:"构建配置",build_command_finished:"构建命令结束",file_restore:"文件恢复",git_baseline:"Git 基线",mcp_status:"MCP 状态",task_created:"任务创建",task_started:"任务开始",task_state_changed:"任务状态变化",task_completed:"任务完成",task_failed:"任务失败",task_cancelled:"任务取消",message:"消息",analysis_started:"分析开始",analysis_completed:"分析完成",llm_request:"模型请求",llm_response:"模型响应",llm_error:"模型错误",tool_started:"工具开始",tool_finished:"工具结束",plan_created:"方案生成",plan_validated:"方案校验",approval_required:"等待审批",approval_decision:"审批决定",file_changed:"文件修改",build_started:"构建开始",build_finished:"构建结束",test_started:"测试开始",test_finished:"测试结束",validation_started:"验证开始",validation_finished:"验证结束",repair_started:"自动修复开始",repair_finished:"自动修复结束",reflection_started:"失败原因分析开始",reflection_finished:"失败原因分析结束",reflection:"失败原因分析",service_interrupted:"服务中断"};
    return labels[s] || "事件（" + s + "）";
}
function duration(ms) { return ms === undefined || ms === null ? "—" : Number(ms).toFixed(0) + " 毫秒"; }
function time(t) { return t ? String(t).replace("T"," ").replace("+00:00"," UTC") : "—"; }
function terminal(s) { return ["completed","failed","cancelled"].indexOf(s)>=0; }
function array(v) { var result=[]; if(v) for(var i=0;i<v.length;++i) result.push(v[i]); return result; }
