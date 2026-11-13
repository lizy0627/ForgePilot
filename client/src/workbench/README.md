# Qt Mission 工作台

`main.cpp` 将 MissionController 作为 `missions` 注入主窗口，
`client/workbench/` 提供 QML 工作台。自由聊天、v1 API 和旧 `TaskController`
保留；聊天历史、记忆或 MCP 配置不会自动进入 Mission 请求。

实现前核对了 `docs/development-technical-plan.md` 第 5、20、21 节和实际后端 v2
模型/路由。原 `AgentApiClient` 仅支持 v1，JSON 检查只覆盖顶层对象；原 `SseClient`
只根据事件序号处理 v1，遇到缺口直接报错；旧 `TaskController` 同时承担计划、构建、
Diff、Trace 等投影。这些既有接口继续保留，v2 校验与工作台职责单独实现。

## 职责

| 类 | 职责 |
| --- | --- |
| `MissionController` | 创建/切换任务、读取一致快照、SSE 补偿、取消/重试/显式恢复，协调模型和操作可用性 |
| `MissionListModel` | 本会话创建或打开的任务，按 task_id 更新且 revision 不回退；后端当前没有 v2 列表端点 |
| `GraphModel` | 节点状态、依赖、Agent、后端指标及操作提示；恢复期间禁用审批/直接重试 |
| `ArtifactModel` | 产物 envelope、类型、来源、哈希、内容；详情通过后端读取 |
| `EvidenceModel` | EvidenceBundle 中的证据，使用 artifact_id + evidence_id 区分不同来源 |
| `DiffModel` | 最新累计 ChangeSet 的文件和 patch，保留 ChangeSet 历史 |
| `ValidationModel` | 最新验证命令、测试结论和历史报告 |
| `ApprovalController` | 显式批准/拒绝当前 TaskSpec、Plan、Scope；单次提交，不自动重试 |
| `DeliveryController` | 向后端请求交付预览，再显式提交绑定 revision 的 token；不执行本地 Git |
| `MissionProtocol` | v2 请求/响应类型和关键嵌套字段、标识、整数、枚举及稳定中文错误映射 |
| `WorkbenchListModel` | 只读 Qt role 投影、行更新与 begin/end 通知 |

## 信号与一致性

1. `AgentApiClient::responseReceived/requestError` 按 request id 路由。v2 使用严格
   JSON 解析，拒绝重复键、错误 Unicode、错误字段类型和跨任务响应；取消请求后不再发结果。
   v2 POST 使用不可回卷的上传流，断线不会由 Qt 隐式重发。
2. `SseClient::eventApplied` 只发布尚未处理的 event_id / task_revision。旧版本不回退，
   乱序缺口停止应用；`snapshotRequired` 要求补快照，恢复后以 `Last-Event-ID` 续订。
3. `MissionController` 合并事件刷新：先 GET Mission，再读取 Graph / Artifacts / Diff /
   Validation，最后 GET Mission 校验。前后 revision、游标以及 Graph revision 一致，且覆盖
   已观察的事件版本，才提交整批模型。刷新过程中操作不可用。
4. 审批固定提交 `expected_revision`、`task_spec_hash`、`plan_hash`、`scope_hash`。
   stale 或不确定网络结果触发读取，原审批绝不重发。用户需审阅刷新后的内容，再明确操作。
   审批节点只冻结并展示方案；批准前（包括拒绝路径）不运行仓库的 CMake/CTest 进程。
   批准后先由 `baseline_capture` 确定性节点采集基线，再启动 Implementer；基线配置失败
   暂停该节点，重试/恢复仍须完成基线，不消耗 Repair 次数，也不增加日常执行审批。
5. 交付预览和恢复计划绑定任务及 revision；事件、版本变化或冲突会使旧 token 失效。
   请求取消只分离客户端，不代表后台 Mission 被取消；任务取消必须调用后端 cancel。
6. 请求和事件均由 QObject 上下文、任务代次及 QPointer 守护。切换/析构取消请求和流；
   同步信号中的切换或销毁也不能继续提交旧快照。

Graph 的指标来自后端 `NodeResult.metrics`；没有后端结果时为零，不推算运行中的用量。
操作 role 是展示提示，最终权限与状态判断仍由后端负责。所有控制器只调用 HTTP/SSE，
不执行 Git、模型推理、构建命令或仓库文件修改。

新任务使用冻结图 `.v2`（版本 2）。旧 `.v1` 任务的历史、Diff 和报告继续可读；未完成的
旧任务必须重新创建并审批，后端明确提示，不静默迁移。

## 验证入口

- `scripts/Test-Client.cmd -V`：保留既有通信回归，增加 API、SSE、模型、控制器与工作台 QML QtTest。
- `scripts/Test-ClientIntegration.cmd`：保留聊天/旧任务集成，增加离线真实后端 Mission 场景。
- `scripts/Test-Agent.cmd`：后端全量测试。

HTTP fixture 覆盖严格解析、状态转换、revision 冲突、去重/乱序/迟到、断线补偿、取消、
对象销毁、任务切换和中文错误。模型使用 `QAbstractItemModelTester`；真实后端生成的七类
artifact fixture 同时验证协议和模型，集成测试不使用外部模型服务。
