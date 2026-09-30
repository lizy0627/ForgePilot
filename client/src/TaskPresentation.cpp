#include "TaskController.h"
#include <QJsonDocument>
#include <QJsonArray>
#include <QMap>

QString TaskController::errorSummary(const QString &code) const
{
    static const QHash<QString, QString> labels{
        {"network_error", "无法完整读取后端响应，请检查连接。"},
        {"http_error", "后端未能处理请求。"},
        {"invalid_response", "后端响应格式无效。"}, {"response_limit", "后端响应超过大小上限。"},
        {"unknown_task", "任务不存在，请刷新历史记录。"}, {"unknown_trace", "执行记录不存在，请刷新。"},
        {"repository_invalid", "仓库目录无效，请选择存在的本地 Git 仓库。"},
        {"repository_busy", "仓库正由其他任务使用，请等待该任务结束。"},
        {"task_busy", "任务仍在执行，请稍后刷新。"},
        {"invalid_state", "当前任务状态不允许此操作，请刷新状态。"},
        {"stale_plan", "方案版本已变化，请审阅当前版本后重新决定。"},
        {"plan_changed", "方案内容或文件范围已变化，请刷新后重新审阅。"},
        {"already_approved", "此方案已获批准，本次不会重复执行。"},
        {"invalid_plan_version", "方案版本无效，请刷新方案。"},
        {"approval_required", "此操作超出已批准的文件范围，需要重新审批。"},
        {"invalid_request", "请求格式无效。"}, {"invalid_task_id", "任务标识无效。"},
        {"invalid_page", "分页参数或游标无效，请刷新列表。"},
        {"invalid_event_id", "事件游标无效，请刷新任务。"},
        {"event_cursor_ahead", "事件游标超出当前记录，请刷新任务。"},
        {"task_capacity", "活动请求数量已达上限，请等待当前请求完成。"},
        {"server_stopping", "后端正在关闭或不可用。"},
        {"llm_config", "模型配置不可用，请检查后端配置。"},
        {"llm_http", "模型服务请求失败，请查看原始错误。"},
        {"build_config", "构建配置不可用，请检查后端配置。"},
        {"storage_busy", "历史数据库正由其他进程使用。"},
        {"storage_unavailable", "无法访问历史数据库。"},
        {"service_interrupted", "服务中断，任务未能继续执行。"},
        {"validation_failed", "构建或测试验证失败。"},
        {"modification_failed", "修改执行失败。"},
        {"step_limit", "已达到模型请求次数上限。"}, {"context_limit", "对话内容超过上下文预算。"},
        {"host_not_allowed", "只允许访问本机后端地址。"}, {"origin_not_allowed", "不允许跨来源访问。"},
        {"route_not_found", "后端接口不存在，请检查地址及版本。"},
        {"task_internal", "任务处理发生内部错误，请刷新状态。"},
        {"api_internal", "后端处理发生内部错误，请刷新任务状态。"}
    };
    return labels.value(code.section(':', 0, 0).trimmed(), "操作失败，请查看错误详情。");
}

static QString textValue(const QJsonValue &v)
{
    if(v.isString()) return v.toString();
    if(v.isObject()) return QString::fromUtf8(QJsonDocument(v.toObject()).toJson(QJsonDocument::Compact));
    if(v.isArray()) return QString::fromUtf8(QJsonDocument(v.toArray()).toJson(QJsonDocument::Compact));
    return {};
}
void TaskController::projectEvent(const QString &id,const QJsonObject &payload)
{
    const auto event=payload.value("event").toString();
    const auto data=payload.value("data").toObject();
    if(event=="task_completed" || event=="task_failed" || (event=="task_state_changed" &&
        QStringList{"completed","failed","cancelled"}.contains(data.value("state").toString()))) {
        for(const auto &pending:m_openTools) for(const auto &key:pending)
            m_traces.upsert(key,{{"status",data.value("state").toString()=="cancelled"?"cancelled":"interrupted"},
                {"finishedAt",payload.value("timestamp").toString()},{"error","任务已结束，工具未报告完成结果。"}});
        m_openTools.clear();
    }
    if(event!="tool_started" && event!="tool_finished") return;
    const auto name=data.value("name").toString();
    const auto call=data.value("tool_call_id").toString();
    const auto correlation=call.isEmpty()?QString("runtime:%1:%2").arg(data.value("repair_round").toInt()).arg(name):"call:"+call;
    QVariantMap row{{"name",name},{"round",data.value("repair_round").toInt()}};
    if(event=="tool_started") {
        m_openTools[correlation].append(id);
        row.insert("startedAt",payload.value("timestamp").toString()); row.insert("status","running");
        const auto args=textValue(data.value("arguments"));
        row.insert("arguments",args); row.insert("argumentsSummary",args.simplified().left(180));
        m_traces.upsert(id,row);
    } else {
        auto &pending=m_openTools[correlation];
        const auto key=pending.isEmpty()?id:pending.takeFirst();
        row.insert("finishedAt",payload.value("timestamp").toString());
        row.insert("status",data.value("success").toBool()?"succeeded":
            data.value("error").toString().startsWith("cancelled")?"cancelled":"failed");
        row.insert("durationMs",data.value("duration_ms").toVariant());
        row.insert("output",textValue(data.value("output"))); row.insert("error",data.value("error").toString());
        m_traces.upsert(key,row);
    }
}
QVariantList TaskController::diffFiles() const
{ return m_snapshot.value("task_diff").toObject().value("files").toArray().toVariantList(); }
QVariantList TaskController::buildReports() const
{
    QMap<int,QJsonObject> reports;
    for(const auto &v:m_snapshot.value("validation_history").toArray()) {
        const auto r=v.toObject(); reports.insert(r.value("repair_round").toInt(),r);
    }
    const auto current=m_snapshot.value("build_result").toObject();
    if(!current.isEmpty()) reports.insert(current.value("repair_round").toInt(),current);
    QVariantList result;
    for(auto it=reports.begin();it!=reports.end();++it) {
        const auto report=it.value(); QVariantList stages;
        for(const auto &phase:QStringList{"cmake_configure","cmake_build","run_tests"}) {
            QJsonObject command;
            for(const auto &c:report.value("commands").toArray()) if(c.toObject().value("phase").toString()==phase) command=c.toObject();
            QString status=report.value("stages").toObject().value(phase).toString("not_run");
            const auto tests=report.value("tests").toObject();
            if(phase=="run_tests" && tests.value("status").toString()=="no_tests") status="no_tests";
            stages.append(QVariantMap{{"phase",phase},{"status",status},{"exitCode",command.value("returncode").toVariant()},
                {"durationMs",command.value("duration_ms").toVariant()},{"stdout",command.value("stdout").toString()},
                {"stderr",command.value("stderr").toString()},{"truncated",command.value("truncated").toBool()},
                {"command",command.value("command").toArray().toVariantList()}});
        }
        result.append(QVariantMap{{"round",it.key()},{"label",it.key()==0?"首次修改":QString("第 %1 轮自动修复").arg(it.key())},
            {"stages",stages},{"tests",report.value("tests").toObject().toVariantMap()},
            {"selection",report.value("selection").toObject().toVariantMap()},{"error",report.value("error").toString()}});
    }
    return result;
}
