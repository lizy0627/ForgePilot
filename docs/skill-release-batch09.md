# 技能库发行与回滚

本版正式内置 **0 项**。Qt 列表刷新候选 v1 的规范化哈希为 `d408c9d2375c0e929e54f54aba97c85222b538bdbff363cfd64fc9bb5241da4d`。批次 08 的离线配对中，两臂首次通过均为 21/21，调查工具调用中位数均为 2。真实配对为 0 次。发行验收使用这份已审核夹具，测试完成后丢弃夹具目录。[批次 08 评审与原始记录](skill-evaluation-batch08.md)

## 从已审核版本导出

从仓库根目录进入 `agent`，为下面的 PowerShell 变量填入源数据库、已审核技能 ID、本机版本、目标公开短名、整数发行版本和临时评审目录。读取使用只读 SQLite 连接。

```powershell
Push-Location agent
try {
    ..\agent.venv\Scripts\python.exe -m forgepilot.skill_package --database $database --skill-id $skillId --version $version --slug $slug --release-version $releaseVersion --output-directory $reviewDir
} finally { Pop-Location }
```

`$reviewDir/<slug>/SKILL.md` 保存标题、可选摘要和指导正文。`manifest.json` 保存发行版本、适用阶段、匹配规则、使用范围、适用边界及 SHA-256。导出器回读两份文件，使用冻结任务相同的内容模型重新计算摘要并与已审核版本比较。Markdown 文件另有字节哈希。私有手记、Mission 标识、证据引用、生成尝试和日志均无导出字段。公开字段也会检查常见的路径、标识、代码和日志片段。人工逐行审阅全部自由文本仍是发布步骤。

对每个拟晋升的版本完成两项审核后，再把文件纳入 Git。

1. 核对固定配置的真实模型配对记录，包含至少三项留出正例、两项反例、各臂分母和失败样本。至少一项留出正例首次通过改善，或最终成功率持平且调查调用中位数下降至少 20%。反例须排除稳定错误修复，权限、证据与恢复回归须通过。
2. 核对导出的通用指导、匹配范围和哈希，确认可公开。将两份文件放入 `resources/skills/<slug>/`，在 `catalog.json` 的 `packages` 数组中加入同一 `slug`、`version`、`content_sha256`。修改已发布包时递增发行版本。该目录的 Git 评审记录承载晋升决定，哈希用于复核字节和内容一致性。

## 启动、升级与撤回

开发后端从仓库资源目录定位清单，冻结后端从 PyInstaller 的运行时资源目录定位。构建只暂存清单列出的两个包文件。数据库 v14 在单事务中导入整份清单。已有同版内容可以重复加载，同版分歧、版本倒退或损坏的清单会阻止本次导入。修复发行资源后可重新打开数据库，也可先停用新入口找回草稿。

内置身份为 `builtin:<slug>`。本机晋升来源通过不可变版本的内容哈希关联，可在 `skill_promotion_sources` 查询。当前启用的内置与本机版本内容相同，推荐优先显示一个本机身份。从内置版改编会产生独立本机身份。新发行版仅更新内置指针，历史版本继续供原有任务恢复。移出清单会撤下推荐，重新上架时保留用户显式停用的选择。

功能回滚用 `FORGEPILOT_DISABLE_SKILL_ENTRIES=1` 重启。仍可审核现有草稿和读取冻结任务，后端跳过内置导入，空选任务沿用原创建流程。旧程序需要的库，用 `database-copy` 从升级前备份恢复到另一目录。先保存升级后的数据库及制品目录，默认制品根为 `%LOCALAPPDATA%\ForgePilot\data\tasks`，自定义位置见 `FORGEPILOT_ARTIFACT_ROOT`。[Windows 发布包操作说明](windows-release-readme.md)

自动备份由数据库 schema 升级触发。后续发行若沿用 v14，又需要能够恢复升级前的目录状态，请在更新前停止实例，用当前程序的 `database-copy` 保存一份数据库，再更换发行包。

## 重跑发行验收

```powershell
.\scripts\Build-Client.cmd
$out = Join-Path $env:TEMP ('fps09-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
.\scripts\Package-Windows.cmd -Version skills-verify -OutputDirectory $out -Verify
.\agent.venv\Scripts\python.exe -I scripts/verify_frozen_release.py (Join-Path $out 'ForgePilot-skills-verify-windows-x64.zip') --output (Join-Path $out 'frozen-start-stop.json')
.\scripts\Test-Client.cmd
.\scripts\ci-qt-integration.ps1 -Only 'mission skills'
```

本批后端回归从仓库根目录选取以下套件。批次 08 的完整语料与配对实验记录见该批评审。

```powershell
$files = @(rg --files agent/tests -g 'test_*skill*.py' -g '!test_skill_eval_corpus.py' -g '!test_skill_pair_runner.py')
.\agent.venv\Scripts\python.exe -m pytest -q -ra @files agent/tests/test_persistence.py agent/tests/test_release_boundaries.py agent/tests/test_runtime_release.py agent/tests/test_protocol_v2.py agent/tests/test_desktop_launcher.py
.\scripts\Test-Agent.cmd -q tests/benchmark_skill_recommendations.py
```

`-Verify` 在解压目录执行离线 Qt/后端自检。四次后端启动各使用独立的临时用户目录，分别读取开发版与冻结版的正式清单、以及加入 Qt 夹具的两份临时清单。验收比对文件集合、数据库中的版本、重新计算的内容哈希和私有草稿数，并使用解压出的程序保存升级后数据库和恢复升级前副本。

本批 `skills-verify` ZIP 的 SHA-256 为 `b7876bc589eb47f1a2a1f7b77162386e52b7db1876598b503192b5e5f719de84`，正式目录清单的 SHA-256 为 `ce4eda9f4df511beaf04c2dc4564ccba1af369e4cc7ca7922f6e90420ff9d27e`。开发与冻结夹具的内容哈希均与批次 08 一致，四个用户库的私有草稿数均为 0。额外运行 `scripts/verify_frozen_release.py` 检查归档的 1562 个文件，禁入项为 0，并从解压目录启动和关闭可见 Qt 客户端及后端，模型请求数为 0。

独立暂存检出的 31 项格式、导入、回滚、资源和协议检查全部通过。随后仅在这份临时树中加入 `qt-list-probe` v1，以 `-Version fixture-rehearsal -Verify` 再构建一次。PyInstaller 将两份夹具文件收进 ZIP，原样解压的程序导入了相同哈希。另用批次 08 的 `ca448b2` 代码创建真实 v13 库，升级后分别保留 v14 和恢复 v13。旧版后端成功打开恢复副本，读回升级前的会话和草稿，对副本的后续写入与 v14 原库隔离。

| 研发阶段 | 已交付提交 | 验收范围 |
| --- | --- | --- |
| A | `338eb93` | 本机草稿、审核、不可变版本和数据库升级 |
| B | `e92d201`、`173924f` | 推荐、选中版本冻结、执行注入与恢复 |
| C | `d086d6e`、`9b221c3` | 终态任务手记、私有证据与受策略约束的建议生成 |
| D | `b43ed92`、`8e4382e` | Qt 推荐选择、提炼审核和本机 HTTP 桌面闭环 |
| E | `ca448b2` 和本批 | 离线配对、可审阅导出、冻结发行和回滚。真实效果仍待验证。 |

正式真实基准仍受批次 08 的隔离与许可条件约束。团队共享、跨机同步和团队审核权限留给后续计划。
