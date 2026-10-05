# ForgePilot v1 本机 HTTP 与 SSE 协议

Phase 6–8，协议版本 1.0.0。Phase 8 兼容增加分页、read_only、diff_summary、duration_complete 及只读 Trace 查询。请求/响应以 [openapi.json](openapi.json) 为机器可读定义；Task 事件见 [events.schema.json](events.schema.json)，会话回答事件见 [conversation-events.schema.json](conversation-events.schema.json)，示例见 [examples.json](examples.json)。两类 SSE 事件不共享权限或终态语义。Schema 由代码生成并由测试检查一致性。

服务入口为 `python -m forgepilot.server`，默认 `http://127.0.0.1:8765`，可选择 `--host ::1`。v1 使用本机 SQLite，仍为单写进程，没有远程账号、多用户或跨进程执行恢复。Host 仅接受回环 IP/localhost，禁止跨 Origin 浏览器访问，不开启 CORS。模型 API Key 只属于后端启动配置，不接受在任务 JSON 中传入；MCP 凭证仅通过下述专用管理接口提交，不写请求日志或 Trace。

## MCP 请求调用记录与模型契约

MCP 主链扩展（数据库 v5）：受理问答/工作请求时按模式固定配置快照和工具白名单；普通问答不需要项目。模型只收到统一 `ToolSpec(name, description, input_schema)`，由 `ModelClient.chat(messages, tools) -> ChatReply` 和 `aclose()` 负责模型适配，OpenAI 工具编码不进入 Registry。

请求状态的可空 `tool_activity` 表示本轮当前/最后一次 MCP 调用；新客户端主要从会话 SSE 获取活动，旧服务则回退状态轮询。`GET /api/v1/conversations/{conversation_id}/requests/{request_id}/mcp-tools` 返回 `{conversation_id, request_id, items}`，旧历史返回空 `items`。问答记录保存在请求行，工作记录投影自原 Trace；该接口只读，模型不可调用。状态为 `running/succeeded/failed/cancelled/interrupted`，来源是独立的标题/HTTP(S) URL。每请求最多 8 条记录、10 个来源，参数/结果摘录上限分别为 1024/2048 字符；问答入库字段与该 GET 的工作 Trace 投影合计最多 64 KiB，不将原工作 Trace 总大小声称为 64 KiB。删除对话同步删除记录。字段和示例以 OpenAPI 及 `conversation_mcp_tools` 示例为准。

配置变更只影响未来请求；等待 Plan 审批时关闭连接，恢复时沿用原快照和剩余预算、重新核对已选工具定义。启动参数 `--mcp-config` 在后台执行一次导入并记录完成标记，后续删除不会复活；失败不阻断问答。`--disable-mcp` 同时禁止导入、连接和 Schema 暴露。

## 验收与兼容

协议仍为 v1；`tool_activity` 是向后兼容的可空增量，Task SSE 事件类型不变。工作沿用原工具事件/Trace，问答在可用时使用独立 SSE，否则保持原请求轮询。OpenAPI 包含会话 SSE 路由，`events.schema.json` 保持原 Task 事件契约，`conversation-events.schema.json` 单独定义回答事件；`python -m forgepilot.export_protocol` 统一生成三者。数据库 v5 升级保留迁移备份，旧请求的 MCP 记录默认为空，运行中断记录不会重放。

第二个可注入 ModelClient 已通过同一工具往返测试；不代表自训练模型质量验收。A01–A20 及未执行的公网验证见 [验收报告](../docs/mcp-visual-management-acceptance.md)。预设精确匹配 `tavily-search` / `tavily-extract`，远端目录不匹配时返回 `mcp_preset_mismatch`，不模糊匹配或增加授权。

## 启动

在继承已有 MSVC x64 开发环境的 PowerShell 中运行。路径仅为本机启动示例；业务代码没有这些硬编码，参数与 CLI 共用同一构建配置。

```powershell
Set-Location D:\Development\ForgePilot\agent
. ..\scripts\agent-env.ps1
& 'D:\uv\uv.exe' sync --locked
$env:FORGEPILOT_BASE_URL = Read-Host 'OpenAI-Compatible API root，例如 https://your-endpoint/v1'
$env:FORGEPILOT_MODEL = Read-Host '模型名称'
$env:FORGEPILOT_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'API Key' -AsSecureString)).Password
& 'D:\uv\uv.exe' run --no-sync --locked python -m forgepilot.server `
    --rg 'D:\ripgrep\rg.exe' --cmake 'D:\CMake\bin\cmake.exe' `
    --generator Ninja --make-program 'D:\Ninja\ninja.exe'
```

服务仅在需要时通过原 OpenAI-Compatible Client 访问模型。上述真实模型命令尚未使用真实凭证验证；验收使用可注入 fake/mock。`UV_OFFLINE` 只影响 uv，不限制模型 HTTP。现有 configure/build/test-preset、configuration、三个阶段 timeout 等参数与 CLI 一致；每次创建请求不能覆盖工具路径、模型、批准范围或预算。

## 会话接口（第二至第四阶段增量）

原任务协议、审批和 SSE 保持兼容。新增会话接口、消息结构、幂等规则及上下文预算见 [会话说明](../docs/phase2-conversations.md)。`GET /api/v1/tasks` 额外接受可选 `conversation_id`，筛选关联任务。问答不创建 TaskService；执行任务才使用原任务接口审批。

第三阶段增加 `DELETE /api/v1/conversations/{conversation_id}`：无活动执行时删除，活动回答/任务返回 `409 conversation_busy`，用户确认后传 `?stop_active=true`，等待停止及资源关闭，再事务删除。成功返回 `200 {"conversation_id":"…","deleted":true}`；不存在的 ID 同样成功，可安全重试。删除中拒绝本对话新请求（`409 conversation_deleting`）与相关审批，断开 HTTP 不撤销已接受操作。旧 SSE 结束，删除后查询/重新订阅任务返回 404。完整范围及客户端约定见 [删除说明](../docs/phase3-deletion.md)。

第四阶段增加 `/api/v1/memories` 的列表、新增及按 ID 编辑/删除，以及 `/api/v1/conversations/{id}/context` 的摘要查看、编辑、删除和重新启用。记忆及摘要不是任务授权，模型无这些管理工具。后端每次模型调用读取当前有效记忆；删除源对话同步删除摘要和来源记忆。字段、预算、版本冲突和迁移见 [记忆说明](../docs/phase4-memory.md)，以更新后的 OpenAPI 为准。

## MCP 管理接口

公共前缀 `/api/v1/mcp`。业务复用 MCPService、SQLite v5 与当前 Windows 用户 DPAPI；Qt 三种固定表单调用这些接口，问答/工作在受理时读取按模式过滤的不可变白名单快照。旧 stdio CLI 入口仍兼容。Tavily 真实服务仍未验证，预设返回 `real_service_verified=false`。

| 方法与相对路径 | 请求 | 成功响应 |
| --- | --- | --- |
| GET /presets | 无 | 200 `{items:[MCPPreset]}`；固定端点、鉴权、所需凭证名与默认工具名 |
| GET /servers | 无 | 200 MCPServerList；items 只有概要，无 Schema；disabled 为启动级总开关 |
| GET /servers/{server_id} | UUID | 200 MCPServerDetail；完整非敏感编辑信息、目录、revision、has_secret |
| POST /probe | MCPProbeInput；平铺配置，可选 server_id/base_revision，secret_updates | 200 MCPProbeResponse；tools、catalog_hash、server_info、checked_at、解析后 connection、预设默认选择；不落库 |
| PUT /servers/{server_id} | MCPSaveInput；平铺完整配置、base_revision、catalog_hash、selected_tools、secret_updates | 新增 201、更新 200 MCPServerDetail；重新探测验证并原子提交 |
| PATCH /servers/{server_id} | `{revision,enabled}` | 200 MCPServerDetail；停用直接提交，启用重新核对已选工具 |
| POST /servers/{server_id}/test | `{revision}` | 200 MCPServerDetail；仅更新检查状态，不递增配置版本或替换目录 |
| DELETE /servers/{server_id}?revision=N | UUID、正整数 revision；无 body | 200 `{server_id,deleted:true}`；保留历史 |

HTTP/stdio 请求按 transport 区分，拒绝未知/混用字段、类型转换、非 read 权限。请求字段和响应模型以 OpenAPI 为准；可校验的平铺 JSON 示例在 `examples.json` 的 `mcp_*` 条目中，示例凭证只是明确占位值。`POST/PUT/PATCH` 必须使用 `Content-Type: application/json`（可带 charset），读取时限制 512 KiB；畸形、重复 JSON 键或非有限数字均拒绝。DELETE 通过查询参数传 revision。

`secret_updates` 中 HTTP 使用 `api_key`，stdio 使用 `env:变量名`。未提交某项表示保留；字符串表示替换；null 表示清除。编辑密钥框留空时省略该项，不提交掩码；仍要求鉴权或环境变量但缺少其值时拒绝保存，清除要同时移除对应声明。任何 GET/成功响应/错误响应都不返回密钥；环境变量只返回名称。查看和删除不要求解密成功。

HTTP 仅在 transport、协议、主机、有效端口、原样路径、鉴权方式及请求头名都一致时可保留旧密钥（协议/主机/头名忽略大小写，空路径等于 `/`，默认端口换算）。范围改变必须显式提交有效 `api_key`，否则在任何网络请求前返回 `mcp_secret_required`。

stdio connection 可选 `public_env_names`，默认 `[]`；名字必须属于 `env_names`，按 Windows 大小写不敏感去重，最多 16 项。它仅声明普通配置值无需按已知秘密脱敏，所有变量值仍通过 `secret_updates["env:NAME"]` 和 DPAPI 保存，GET 只返回名称和分类。旧请求不含此字段时所有变量均敏感。参见 `mcp_stdio_public_env_probe_request` 示例。

新增表单在整个编辑流程中固定一个 UUID，base_revision=0。保存响应丢失或后端断开时，通过 GET 相同 ID 确认状态；勿换新 UUID 自动重建。更新携带读到的 revision；探测前、网络后和事务内检查版本，冲突刷新后重试。停用/删除不等待其他探测，旧请求的迟到结果返回冲突，不能污染新状态。检查成功只代表握手与发现，不能显示永久在线或声称业务搜索已验证。

所有 MCP 网络操作使用 TaskHost 现有工作循环，最多同时两项，清理完成才释放名额；第三项立即返回 429，无排队。探测、保存、启用和测试共享此限制，网络期限 20 秒、另有最多 3 秒资源清理。客户端 TCP 断开或请求任务取消时，取消对应管理探测并等待 owner/进程回收；原工作任务/审批操作的断连语义不变。后端强制关闭传输时可能无法送达 JSON 响应，保存结果仍按固定 UUID 查询。

启动 `--disable-mcp` 时，列表 `disabled=true`；允许预设/列表/详情、停用和删除，拒绝 probe、test、启用及 PUT 保存（即使保存 enabled=false）。服务初始化不连接外部 MCP、不解密已有配置，也不读取 legacy 文件，普通问答可独立启动。非禁用启动下，显式 legacy 配置通过同一 probe/save 后台导入，固定 UUID 与 `legacy-mcp-config-v1` 标记同事务提交；已经导入后删除不会复活。导入失败可重试，不阻断普通问答。CLI 的显式旧入口保留，HTTP 任务使用数据库快照。

所有正常业务错误沿用 `{error:{code,message,details,task_id}}`，无原始请求值或服务端正文。MCP 请求校验不回显错误字段路径，以防将凭证放入未知键/环境变量更新键时泄漏。

| HTTP | MCP 管理错误码 |
| --- | --- |
| 400 / 403 | 原 Host / Origin 限制；`mcp_disabled` 为 403 |
| 404 | `unknown_mcp_server` |
| 409 | `mcp_config_conflict`、`mcp_catalog_changed` |
| 413 / 415 | 请求超过 512 KiB 的 `mcp_limit` / 非 JSON 的 `invalid_request` |
| 422 | `invalid_request`、`mcp_config`、`mcp_auth`、`mcp_not_mcp`、`mcp_version`、`mcp_protocol`、`mcp_selection`、`mcp_secret_required`、`mcp_secret_unavailable`、`mcp_executable_missing`、配置/目录/响应超限的 `mcp_limit` 等 |
| 429 | `mcp_capacity`；本地两项准入已满或远端限流 |
| 499 | `mcp_cancelled`；客户端已断连，通常不能实际接收此响应 |
| 503 / 504 | `mcp_storage` 或 `server_stopping` / `mcp_timeout` |

详情/probe 响应最多 512 KiB、目录最多 256 KiB。保存响应超限在提交前回滚。8 个保存服务、4 个启用服务、20 个启用工具和各模式 24,576 字符工具定义限额继续由业务层检查；减少配置或勾选后重试。管理接口从未注册为模型工具。

## 原任务 HTTP 接口

| 接口 | 请求 | 成功响应 |
| --- | --- | --- |
| POST /api/v1/tasks | CreateTask：repository_path、prompt | 202 TaskResponse；仓库校验及仓库锁准入完成，后台继续分析 |
| GET /api/v1/tasks/{task_id} | 无 | 200 TaskResponse；纯数据快照 |
| GET /api/v1/tasks | limit=20(1–100)、before=上一页 next_cursor | 200 TaskList：items、total、next_cursor；全部持久化历史创建时间倒序 |
| POST /api/v1/tasks/{task_id}/approve | ApproveTask：plan_version 正整数 | 202 TaskResponse；批准当前完整方案并启动执行 |
| POST /api/v1/tasks/{task_id}/feedback | PlanFeedback：plan_version 正整数、feedback 字符串 | 202 TaskResponse；已持久化为 planning，仍为原版本，后台重规划 |
| POST /api/v1/tasks/{task_id}/cancel | 可省略 body；或 CancelTask | 200 TaskResponse；等待拥有的执行/进程树清理，返回 cancelled |
| GET /api/v1/tasks/{task_id}/events | 可选 Last-Event-ID header | 200 text/event-stream；终态已完整确认则 204 |
| GET /api/v1/tasks/{task_id}/trace | after=0、limit=50(1–100) | 200 TracePage；内部事件摘要，不载入长 data |
| GET /api/v1/tasks/{task_id}/trace/{sequence} | 内部时间线 sequence | 200 TraceDetail；单条完整 data |

请求拒绝未知字段及类型转换（例如 plan_version 的字符串或 bool），prompt 必须非空且不超过 16,384 字符，repository_path 不超过 4,096 字符。TaskResponse 含完整 Plan、plan_version、approved_plan_version、approved_files、repair_round、repair_records、validation_history、build_result、task_diff、错误、usage 与时间；不包含密钥、原始快照字节、执行句柄或整个内部模型对话。TaskSummary 是列表中的轻量子集。所有时间使用 UTC ISO 8601。

`last_event_id` 是与该快照一起原子发布的最新事件游标，初始可能为 null。终态快照（含最终 Diff、验证结果和 finished_at）与终态事件在 Runtime finally、仓库锁释放、LLM 客户端关闭后一起发布，避免先看到 completed 再看到未完成结果。进行中的 snapshot 反映最后一个发布点；finished_at 在终态才非空。

同一仓库包括大小写/规范路径别名只能有一个修改任务。进程内预约阻止重复创建，原 Windows mutex 同时阻止 CLI/其他进程占用的仓库。冲突返回 409 repository_busy，不等待原任务完成。准入失败可能已经分配 task_id，该失败记录仍可查询/订阅；error.task_id 指明记录。创建连接断开后准入/任务仍继续，可从列表找回该进程内记录；不应盲目重复 POST。

最多接纳 32 个活动请求（问答与执行任务合计，等待审批也占用），满额返回 503 task_capacity；请求结束后释放执行资源，累计完成数量不设 32 次上限。SQLite 历史仍保留且不占活动额度。列表按 created_at / task_id 键集分页；删除以对话为单位，无恢复执行接口。任务及会话列表返回自包含的 `next_cursor`，客户端原样传回 `before`（上限 512 字符），删除上一页边界行后仍可继续加载。旧 ID 游标在对应行存在时保持兼容。旧记录 read_only=true，approve/cancel 返回 409 invalid_state。正常停机取消；异常退出遗留记录在下次启动原子变为 failed / service_interrupted 并追加 task_state_changed、task_failed，保留既有证据。存储路径及版本迁移说明见 [persistence.md](../docs/persistence.md)。

Trace 摘要含 sequence、trace_sequence、kind、timestamp、preview(最多240字符)、size(字符数)，翻页传 next_cursor 为 after。单条详情包含完整 data；这两个 Trace 序号不能代替公开 SSE 游标。任务详情保存完整 task_diff 与轻量 diff_summary；Test Result 在 build_result.tests 及 validation_history 各轮中。usage 未知值为null；duration_complete=false 表示中断前记录的耗时不完整。可执行分页查询示例见持久化说明。

## Approval 与 Reject

批准只能作用于当前 waiting_approval 的 plan_version。旧版本返回 stale_plan；重复已批准的版本返回 already_approved 或仍在执行时的 task_busy；终态/错误状态返回 409。并发批准最多启动一次执行。新增文件范围会产生更大的 plan_version，客户端必须显示并批准完整新方案，不能复用旧批准。

### 方案反馈

`POST /api/v1/tasks/{task_id}/feedback` 只受理当前进程内、待审批且已完成资源清理的任务。请求示例：

```json
{"plan_version":1,"feedback":"保留公开接口，补充边界条件验证。"}
```

版本必须是非 bool 的正整数；feedback 原始长度最多 4,000 字符，去掉首尾空白后非空，禁止 NUL，保留内部段落。拒绝额外字段、TaskPlan 和授权字段。202 只表示已受理，响应固定为 planning 与原版本；模型工作在后台继续。反馈撤销原批准能力，合法完整新计划版本加一，即使内容相同也必须重新审批。新计划可先到达，但 waiting_approval 和 approval_required 在 MCP 清理完成后才发布。

同一版本的反馈与反馈、反馈与批准由同一控制锁串行决定，最多一方成功。重复/过期请求返回 409（task_busy、invalid_state、stale_plan 或 already_approved）；全任务 30 次模型请求预算耗尽时返回 409 step_limit，不重置预算。未知任务返回 404 unknown_task；参数错误返回 422 invalid_request；停止或存储故障返回 503 server_stopping/storage_unavailable/storage_busy，错误不回显整段反馈。受理后模型或计划校验失败通过原 TaskResponse/SSE 报告 failed，不追加 HTTP 错误响应。

HTTP/SSE 断线不取消受理工作；网络错误后先 GET 查询状态，不自动重发 POST。取消、删除对话和停服务等待原资源清理，不回滚已有 Diff。历史任务只读，approve/feedback 均返回 409 invalid_state；重启时未完成任务仍按 service_interrupted 终止，不恢复执行。

脱敏反馈通过现有内部 Trace 的 plan_feedback（原 plan_version、feedback）记录一次，各版完整计划使用既有 plan_created。可通过 trace/trace/{sequence} 查询；不增加公开 SSE 类型、conversation message/request 或数据库结构。关联的对话请求在原任务终态才结束。

v1 **Reject 映射到 cancel**，不新增 reject 路由：

```json
{"reason":"reject","plan_version":2}
```

该请求只在对应版本的 waiting_approval 有效，调用原 TaskService.reject，最终 state=cancelled、outcome=rejected、error_code=rejected。普通取消使用空 body 或 `{"reason":"user"}`，不得携带 plan_version；重复普通取消已 cancelled 的任务是幂等 200，不重复发终态事件。completed/failed 不能再取消。approve 与 cancel 在任务内串行决定；若取消先取得执行权，后续批准返回 409；若批准先执行，取消会中止随后正在运行的步骤。

客户端断连、关闭 SSE 或 GET 请求被取消均不等于取消任务。只有明确的 cancel/reject，或整个本机服务器停止，才请求取消。服务停机取消所有在执行和等待审批的任务，等待进程树和线程清理；不恢复或回滚源码。

## SSE 帧与重连

UTF-8，`Content-Type: text/event-stream`，`Cache-Control: no-cache`。每条事件以空行结束：

```text
retry: 1000

id: 11111111-1111-4111-8111-111111111111:7
event: task_state_changed
data: {"event":"task_state_changed","task_id":"11111111-1111-4111-8111-111111111111","timestamp":"2026-09-18T08:00:00+00:00","data":{"from":"planning","state":"waiting_approval","repair_round":0,"outcome":null,"error":null}}

: heartbeat

```

id 为 `<task_id>:<连续序号>`，从 1 开始，只对应公开事件；不是内部 Trace 的序号。JSON 必含 event、task_id、timestamp、data，event 与帧 event 字段相同。正文中的换行由 JSON 转义，不拆成假事件。空闲时每 15 秒发送无 id 的注释心跳；首条事件产生前也可能收到心跳。retry 建议重连间隔 1 秒。

不带 Last-Event-ID 时从第一条重放，然后持续发送新事件，因此可以晚于任务启动甚至完成后订阅。带合法 header 时仅补发严格大于该序号的事件，再继续实时传输；查询/注册等待之间重新检查游标。事件保存在 SQLite，重启后仍从同一事件源补发；重放不重复累计 usage、工具数或耗时。

服务提供**可重放、至少一次消费约定**，不声称 exactly-once：客户端按 `(task_id, sequence)` 去重，忽略已应用的序号；在成功应用 payload 后保存游标。重复连接同一游标得到相同后缀，无游标重连会再次看到全部历史。跨任务、非规范数字、负数等 id 返回 400；超过当前最新序号返回 409 event_cursor_ahead。不要因失败请求而把本地游标清零。

终态流会先送完所有积压事件再 EOF；取消也发送 cancelled 状态后关闭。重连时若 Last-Event-ID 已确认全部终态事件，返回 204，阻止原生 EventSource 无意义重连。普通 200 连接收到终态后，客户端应主动关闭订阅。

| 事件 | data 的关键内容 |
| --- | --- |
| task_state_changed | from、state、repair_round、outcome、error；包括 cancelled 终态 |
| message | role/content/step 的完整分析或修改文本；或 kind=task_error/build_selection 的信息；不是 token 增量流 |
| plan_created | plan_version、完整 plan |
| approval_required | plan_version、files |
| tool_started / tool_finished | name、arguments 或 success/output/error/duration_ms；模型调用含 tool_call_id，Runtime 构建含 source/repair_round |
| file_changed | path、created、before_sha256、after_sha256 |
| build_started / build_finished | phase=cmake_configure 或 cmake_build，repair_round；finished 含 success/error 和本轮 result |
| test_started / test_finished | phase=run_tests，repair_round；finished 含实际 CTest result |
| reflection | repair_round、category、含 root_cause 的完整修复 plan |
| task_completed | state、outcome、steps、usage、final_result 等终态摘要 |
| task_failed | state、error/error_code、outcome 等失败摘要 |

取消没有额外 task_cancelled 事件，也不伪造 task_failed。`task_state_changed` 的 cancelled 即取消终态。Configure 与 Build 各产生一对 build 事件，以 phase 区分；三轮 Repair 沿用同一任务事件序列，repair_round 区分验证轮。

## 会话回答 SSE

`GET /api/v1/conversations/{conversation_id}/requests/{request_id}/events` 只用于 `mode=chat`，必须接受 `text/event-stream`。每个 JSON 都必含 `type`、`conversation_id`、`request_id`、严格单调的 `revision` 和当前 `segment_id`；SSE id 固定为 `<request_id>:<revision>`。客户端只在成功应用事件后更新游标，重连通过 `Last-Event-ID` 发送该完整 id。请求 ID 不匹配或非规范数字返回 400 `invalid_event_id`，超前返回 409 `event_cursor_ahead`，请求不属于对话返回 404 `unknown_request`。

事件类型为 `answer_started`、`answer_delta`、`answer_snapshot`、`answer_segment_discarded`、`answer_segment_committed`、`tool_activity`、`answer_completed`、`answer_cancelled`、`answer_failed`。`answer_delta` 额外含 `previous_revision` 和仅新增的已脱敏 `text`。工具轮的 provisional segment 会先 `discarded`，后续新 segment 不得拼接旧文本；`tool_activity` 不包含参数或输出。`committed` 表示模型段已确认，但客户端仍等待 terminal 后从会话详情取得真实 `message_id`。

每请求最多缓存 256 个事件和 1 MiB SSE 帧，安全草稿最多 32,768 字符，最多 8 个订阅者。当游标早于环形缓冲区时，服务端发送一个 `answer_snapshot`：`content` 是当前完整安全草稿，`revision` 直接跳到最新值，`terminal` 指明是否已终止。应用 snapshot 后不得再应用旧 delta。终态为 `answer_completed/cancelled/failed`；已确认全部终态事件时可返回 204。终态内存流默认保留 60 秒，释放后可由已持久化 request/message 重建只读终态。

空闲期间每 15 秒可发送 `: heartbeat`，建议 `retry: 1000`；心跳不带 id 且不改变 revision。断线不取消模型，客户端保留游标并指数退避重连；revision 不连续时重连以获得 replay/snapshot。404 表示旧服务不支持此可选端点，客户端应回退 request/detail 轮询；401/403/409、Content-Type 错误、非法 JSON、身份或 revision 错误不得无限重连，应保留游标并 GET 真实状态。容量超限为 409 `conversation_stream_capacity`，流已丢失为 503 `conversation_stream_unavailable`，服务端内部状态/大小错误为 `conversation_stream_state` / `conversation_stream_limit`。

## 错误结构和状态码

```json
{"error":{"code":"stale_plan","message":"Plan version changed; review the current plan before deciding","details":[],"task_id":null}}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | invalid_event_id、invalid_page、host_not_allowed | 非法事件/历史游标或本机 Host |
| 403 | origin_not_allowed | 禁止跨 Origin |
| 404 | unknown_task、unknown_trace、unknown_request、route_not_found | 数据库无任务/事件/会话请求或不存在接口 |
| 409 | repository_busy、task_busy | 仓库/任务正在占用 |
| 409 | stale_plan、already_approved、plan_changed、invalid_state、step_limit | 版本/授权/状态冲突或重规划预算耗尽 |
| 409 | event_cursor_ahead | 游标超过已存事件 |
| 409 | conversation_stream_capacity | 会话回答流订阅者达到上限 |
| 422 | invalid_request | JSON/schema 错误；details 仅给字段位置与类型，不回显输入 |
| 422 | repository_*、git_*、invalid_prompt 等原服务错误 | 创建准入失败；具体安全错误码由 TaskService 给出 |
| 503 | task_capacity、server_stopping、llm_config、build_config、storage_busy、storage_unavailable、conversation_stream_unavailable | 会话容量/停止/启动配置、存储或回答流不可用 |
| 500 | task_internal、api_internal、conversation_stream_limit、conversation_stream_state | 未预期内部错误或回答流安全上限/状态错误；不返回原始 traceback |

任务获准创建后的 LLM/Build/Test/Repair 失败属于任务结果，查询仍为 200，state=failed、error_code/failure_kind 给具体原因；不把一次模型 401 映射为查询接口的 HTTP 401。

## 可直接执行的 API 调用

另开 PowerShell，服务保持运行。先创建、查询并展示待审批方案：

```powershell
$base = 'http://127.0.0.1:8765'
$repository = Read-Host '目标 Git 仓库的绝对路径'
$body = @{ repository_path=$repository; prompt='分析并修复项目问题，保留现有测试并执行验证' } | ConvertTo-Json
$task = Invoke-RestMethod -Method Post -Uri "$base/api/v1/tasks" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$id = $task.task_id
Invoke-RestMethod "$base/api/v1/tasks" | ConvertTo-Json -Depth 8
do {
    $task = Invoke-RestMethod "$base/api/v1/tasks/$id"
    if ($task.state -in @('created','analyzing','planning')) { Start-Sleep -Milliseconds 300 }
} while ($task.state -in @('created','analyzing','planning'))
$task | ConvertTo-Json -Depth 12
```

审阅完整 plan 与 build_result.selection 后，明确执行批准；若方案扩展，应重新查询/展示并使用新版本：

```powershell
$approval = @{ plan_version=$task.plan_version } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/api/v1/tasks/$id/approve" -ContentType 'application/json' -Body $approval
curl.exe -N "$base/api/v1/tasks/$id/events"
Invoke-RestMethod "$base/api/v1/tasks/$id" | ConvertTo-Json -Depth 20
```

重新订阅时使用实际收到并应用过的 id，不能猜测序号：

```powershell
$lastEventId = Read-Host '粘贴上次已应用事件的完整 id'
curl.exe -N -H "Last-Event-ID: $lastEventId" "$base/api/v1/tasks/$id/events"
```

取消活动任务（可在另一终端设置 base/id 后执行），或拒绝仍待审批的当前方案：

```powershell
Invoke-RestMethod -Method Post "$base/api/v1/tasks/$id/cancel" -ContentType 'application/json' -Body '{}'

# 拒绝是另一个选择；只对仍 waiting_approval 的任务执行。
$task = Invoke-RestMethod "$base/api/v1/tasks/$id"
$rejection = @{ reason='reject'; plan_version=$task.plan_version } | ConvertTo-Json
Invoke-RestMethod -Method Post "$base/api/v1/tasks/$id/cancel" -ContentType 'application/json' -Body $rejection
```

生成 Schema：在 agent 目录执行 `uv run --no-sync --locked python -m forgepilot.export_protocol`（本机 uv 可使用其绝对路径）。设计依据：[WHATWG SSE 标准](https://html.spec.whatwg.org/multipage/server-sent-events.html)、[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)、[FastAPI 并发说明](https://fastapi.tiangolo.com/async/)。
