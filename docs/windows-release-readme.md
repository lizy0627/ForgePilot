# ForgePilot Windows x64 发布包

本 ZIP 含 ForgePilot 的 Qt 客户端、Python 后端冻结后的 `bin\ForgePilotAgent.exe` 和运行所需的依赖。目标 Windows x64 机器无需安装 Python、uv 或 Qt SDK。普通聊天不需要 Visual Studio/CMake；执行 C++/Qt 代码任务仍需可用的 CMake、CTest 与相应编译工具链。模型连接需要用户自己的 API 配置和网络访问。

1. 将 ZIP 完整解压到可写目录，保留目录结构。
2. 双击 `scripts\Check-Release.cmd`，运行 Qt/QML 客户端加载与后端 OpenAPI 的离线自检。后端使用占位密钥和本机回环地址，不会调用真实模型或公网 MCP。
3. 双击 `Configure-ForgePilot.cmd`，在 GUI 中配置模型地址、模型名称和 API Key。保存配置不发送模型请求；显式点击连接检测或运行 `Check-Model.cmd` 才发送真实请求，可能产生费用。配置与加密密钥仅保存在当前 Windows 用户的本机目录，受控重启后生效。
4. 双击 `Start-ForgePilot.cmd` 启动；使用 `Stop-ForgePilot.cmd` 关闭。`Check-Model.cmd` 使用已保存的配置和密钥再次检查各模型，仅在准备好消耗模型额度时运行。

Qt 客户端依赖 Microsoft Visual C++ Runtime；本包附有 `client\build\Release\vc_redist.x64.exe`，仅在目标机器缺少运行库时需要安装。Windows 安全策略仍可能阻止未知发行者的可执行文件，此时请检查 Windows 应用控制事件。

目录中不应有 API Key、模型配置、任务历史、数据库或日志。运行后生成的数据位于用户本机配置目录及解压目录的 `logs` 下。

## 内置技能与回滚

本版正式内置技能为 **0 项**。Qt 列表刷新候选仍在等待真实模型配对评测。启动后端会从 `bin\_internal\resources\skills\catalog.json` 核对发行清单和内容哈希，并将列出的版本导入当前用户配置的数据库。

需要暂时关闭技能入口时，在解压目录的同一个 PowerShell 窗口执行以下命令。

```powershell
.\Stop-ForgePilot.cmd
$env:FORGEPILOT_DISABLE_SKILL_ENTRIES = '1'
.\Start-ForgePilot.cmd
```

请使用已配置的桌面实例。重启后推荐为空，新提炼与重新生成返回停用状态。携带旧勾选提交的新任务需要清除选择后重试。已有草稿可以继续查看、编辑和审核，已接纳任务按冻结版本继续。恢复入口时先停止实例，执行 `Remove-Item Env:FORGEPILOT_DISABLE_SKILL_ENTRIES`，再启动。其他终端和后续登录需要单独设置该环境变量。

如需运行旧版程序，先停止实例并找到该实例配置中 `server_args` 的 `--database` 路径。桌面默认文件名为 `history.sqlite3`。升级至数据库 v14 前生成的备份位于该文件旁，名称含原版本及 `-backup-`。确认备份与目标旧程序版本匹配。在 PowerShell 中将 `$database`、`$beforeUpgrade` 设为当前数据库和选定备份的完整路径，将 `$preserved`、`$restored` 设为两个尚未创建的目录路径。依次执行：

```powershell
.\bin\ForgePilotAgent.exe database-copy --source $database --destination $preserved
if ($LASTEXITCODE) { throw '保存升级后数据库失败' }
.\bin\ForgePilotAgent.exe database-copy --source $beforeUpgrade --destination $restored
if ($LASTEXITCODE) { throw '恢复升级前副本失败' }
```

两个新目录各有一份经过 SQLite 完整性检查的 `history.sqlite3`。旧程序的 `--database` 应指向第二个目录中的文件。升级后产生的记录保存在第一个目录和原库。另行保存任务关联的制品、仓库和工作树。此命令只复制 SQLite 数据。
