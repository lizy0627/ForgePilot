# 批次 08 · Qt 列表刷新技能晋升评审

**暂缓内置，真实效果待验证。** 候选为批次 01 的手写技能「Qt 保存成功后列表仍显示旧值」v1，规范化内容 SHA-256 为 `d408c9d2375c0e929e54f54aba97c85222b538bdbff363cfd64fc9bb5241da4d`。本次仅在临时 SQLite 库收录该版本。

## 离线工程链路

从 `eval/tasks.json` 引用 `qt-signal-01` 和 `qt-signal-02`，分别复现连接缺失和重复连接。两题是开发样本。推荐标签均为负，因为该技能针对 Qt 模型与视图的保存后刷新。新增题在指导正文冻结后编写，使用同一个小型 Qt 列表模型。语料交付了可执行的缺陷、公开回归和候选目录之外的行为 Oracle。

| 留出样本 | 要区分的行为 | 推荐预期 | HTTP 实测 |
| --- | --- | --- | --- |
| qt-list-01 | 保存完成后模型行仍是旧值 | 命中 | 命中 |
| qt-list-02 | 更新非首行时通知了错误的行 | 命中 | 漏荐 |
| qt-list-03 | 委托依赖的角色未出现在通知中 | 命中 | 命中 |
| qt-list-04 | 当前视图正确，重开后读错持久记录 | 无推荐 | 误荐 |
| qt-list-05 | 空白输入应被拒绝且保留旧标签 | 无推荐 | 无推荐 |

指导正文和匹配条件在 `eval/reviewed-qt-skill-v1.json`。评分端持有 `eval/skill_cases/answers`、`oracles` 和既有开发题答案。候选仓库只含任务说明、源代码、CMake 文件和公开测试。固定清单分别校验任务、仓库起点、答案、标签和 Oracle 的哈希。新题使用内存中的后备数据模拟重开。这五题来自同一模型家族。

### 勾选对照

每题各跑三对，两臂从同一已审核数据库快照和同一份候选源码独立开始。配对锁覆盖 TaskSpec、模型配置、拓扑、阶段预算、源码与验证器、工具二进制和执行器版本。报告同时比对两个分析节点实际收到的 TaskSpec 内容、系统提示与工具清单摘要。勾选臂还核对根因分析节点收到的正文与使用回执。每臂允许 600 秒，脚本写入部分最多调用工具 8 次，修复预算为 0。

本次复用了生产分析图、技能解析与快照、节点路由和审批。模型回复由脚本提供，检查点使用测试后端，获批后的源文件由固定写入脚本修改。每份候选都独立运行 Debug 构建、公开 CTest 和受保护 Oracle，并检查修改范围和评分前后完整性。调查工具数为两个分析节点的实际工具调用，总工具数另计脚本读写。耗时自该臂开始准备候选至验证结束，含编译和四路并行时的资源竞争。脚本没有提供方输入 token 计量。

2026-11-23 最终轮共 21 对，全部条件锁和节点观测核验通过。7 道题各臂都有 3 条记录。独立语料测试确认每道题的公开测试和 Oracle 均能检出初始缺陷，并接受固定修复。

| 离线指标 | 不勾选 | 勾选 |
| --- | ---: | ---: |
| 首次通过 | 21/21 | 21/21 |
| 最终通过 | 21/21 | 21/21 |
| 其中留出正例首次 / 最终通过 | 9/9 · 9/9 | 9/9 · 9/9 |
| 修复轮数中位数 | 0 | 0 |
| 工具调用中位数（其中调查调用） | 4（2） | 4（2） |
| 输入 token 可观测次数 | 0/21 | 0/21 |
| 端到端耗时中位数 | 22.94 s | 24.11 s |
| 失败 / 未执行次数 | 0 / 0 | 0 / 0 |

两个分析节点各读一次源码，固定写入器另读写各一次。留出正例的首次通过差为 0，调查工具调用中位数降幅为 0%。两项都是本次脚本的工程读数。

### 推荐

8 条标注请求包括上表 5 题、2 道开发题及 1 条无关描述。正例 Top-3 命中 **2/3**。共误荐 **1 项**，出现在 **1/5** 个负例中。未知 **0/8**。冻结版本保留了这次漏荐和误荐。

Windows 本机 loopback HTTP 在 500 项已发布元数据上执行查询，其中 1 项是受评技能、499 项是合成干扰项。5 次预热后对 50 个不同请求计时，最近秩 P95 为 **17.11 ms**，低于 100 ms 的初始目标。8 条质量请求与 55 条计时请求均保留原始响应标识和耗时。受监测的仓库、进程、模型及检索入口调用数均为 0。此处测量的是本机推荐接口。

## 真实证据与晋升门槛

| 证据层 | 本批状态 |
| --- | --- |
| 离线工程链路 | 已运行。证明技能选择能抵达指定分析节点，配对调度、独立候选验证、计数和报告可复算。 |
| 主动授权的真实 Mission 试用 | 无本批主动授权，实际运行 0 次。个别开发者试用须单独记录。 |
| 正式真实模型基准 | 实际运行 0 次。现行 CLI 在调用模型前因候选进程隔离未全面验收而拒绝 `real`，也拒绝真实数据集和 multi 拓扑。 |

正式入口还要求明确费用许可、模型配置、显式 `--allow-paid-real` 和总 token 上限。项目的四组协议共用 SQLite 账本，上限 160 万 token，微型单/多各 60 万、真实单/多各 20 万。缺失用量、未决预留会阻断后续付费请求。技能对照没有获分配正式实验预算。

按研发方案第 8 节，仍需真实配对证明至少一项留出正例的首次通过改善，或最终成功率持平且调查工具调用中位数下降至少 20%。还需从反例的实际执行中检查是否稳定诱导错误修复，并确认权限、证据与恢复无回归。当前的离线写入脚本对两臂应用相同修复，无法测出这些真实行为。

## 复跑与核验

在仓库根目录运行。Windows 原生验证使用根目录 `agent.venv` 及 `scripts/local-env.cmd` 中配置的 MSVC、Ninja、CMake 和 Qt Debug kit。以下配对命令每次创建新的报告目录。

```powershell
.\scripts\Run-SkillPairs.cmd --repetitions 3 --jobs 4
.\agent.venv\Scripts\python.exe -m pytest -q -s agent/tests/benchmark_skill_recommendations.py
.\agent.venv\Scripts\python.exe -m pytest -q agent/tests/test_skill_pair_metrics.py agent/tests/test_skill_pair_runner.py
.\scripts\Test-Agent.cmd -q tests/test_skill_eval_corpus.py
```

配对程序写出逐次 `results.json`、按题/分组的 `summary.json` 和 `report.md`。推荐套件另行打印唯一的 `recommendations.json` 路径。解开本批归档可以直接审计保存的 42 行及重新生成对照表，无需编译或模型凭据。

```powershell
Expand-Archive docs/skill-evaluation-batch08.zip -DestinationPath logs/skill-evidence-review
.\agent.venv\Scripts\python.exe eval/run_skill_pairs.py --audit logs/skill-evidence-review/final/results.json
```

归档的 `final/` 保存最终配对和推荐的完整原始 JSON，`preliminary/` 保存开发期间第一次完整配对和推荐记录。两轮均保留全部计划样本。本页只统计最终轮。同一内容在独立数据库中的本机技能 ID 可以不同，跨轮以版本和内容摘要识别。语料以规范化 UTF-8 生成相同的候选字节。执行器和工具摘要记录当次运行的实际字节，另一份检出的脚本换行或工具链版本可能产生不同的执行环境摘要。

其余验收从仓库根目录使用以下命令，最后一条在已初始化的 x64 MSVC 开发者环境中执行。

```powershell
.\agent.venv\Scripts\python.exe -m pytest -q agent/tests/test_benchmark_metrics.py agent/tests/test_benchmark_ab.py agent/tests/test_skill_selection_store.py agent/tests/test_skill_recommendations_http.py agent/tests/test_mission_skill_workflow.py agent/tests/test_mission_skill_resume.py
.\agent.venv\Scripts\python.exe -m pytest -q agent/tests/test_benchmark_runtime.py
.\agent.venv\Scripts\python.exe -m pytest -q -ra agent/tests/test_skill_draft_evidence.py agent/tests/test_mission_skill_routes.py
.\scripts\Test-Agent.cmd -q tests/test_benchmark.py
```

语料与隔离测试 8 项、配对执行与指标测试 16 项、推荐基准与算术测试 2 项均通过。以上四组回归分别通过 50、11、26、34 项，均无跳过。开发过程中曾在编辑评测目录的同时启动旧微型基准，出现 3 项失败（含数据完整性检查）。结束编辑并初始化 MSVC 后，整组 34 项复跑通过。暂存内容独立检出的 17 项轻量检查和已保存结果审计也通过。
