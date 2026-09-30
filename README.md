# ForgePilot

ForgePilot 是一个面向本地 CMake / C++ / Qt 项目的桌面研发助手。它把“先分析、再确认、后修改”做成了一套完整流程：模型可以阅读代码、整理修改方案，但只有用户确认后才能写文件、构建和运行测试。

项目目前主要在 Windows x64 环境下开发，界面使用 Qt Quick，后端使用 Python。除了代码任务，也可以把它当作带多轮历史、项目上下文和记忆管理的本地聊天工具。

> 目前仍是开发中的个人项目。离线流程和固定评测已经跑通，真实模型完成过分析与方案生成验证，但还不能把它当成成熟的自动修复工具。执行前请认真检查修改范围，并自行保留重要代码。

## 能做什么

- 普通问答与连续追问，不选择项目也能使用
- 关联本地 Git 仓库后，只读分析 C++ / CMake / Qt 代码
- 生成修改方案，并把文件范围、步骤和风险交给用户确认
- 获批后修改文件，执行 CMake、构建和 CTest
- 构建或测试失败时，根据日志继续修复，最多尝试三轮
- 展示执行过程、代码差异、构建结果和历史任务
- 保存多轮对话、摘要以及全局或项目级记忆
- 接入经过选择的 MCP 只读工具，例如搜索和网页正文提取
- 使用 clangd 查找定义和引用；不可用时退回文本搜索

仓库里还带有一个独立的模型训练实验环境，位于 `training/`。它和 ForgePilot 主程序使用不同的虚拟环境，适合做小模型结构、训练流程和本地推理实验，不会替换主程序当前使用的模型服务。

## 工作方式

ForgePilot 把问答和代码执行分开处理：

1. “问答”模式只提供聊天和只读项目分析。
2. “工作”模式先读取代码并生成方案。
3. 用户检查方案，确认文件范围后再批准执行。
4. 后端只允许修改本次批准的文件，然后运行配置、构建和测试。
5. 最终结果保留实际差异和执行记录，失败也不会悄悄回滚文件。

模型说“已经获得授权”不会生效，权限只来自界面中的人工操作。方案扩大修改范围时，需要再次确认。

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `client/` | C++20、Qt 6 和 QML 桌面客户端 |
| `agent/` | Python 后端、任务运行时、HTTP/SSE 服务和测试 |
| `protocol/` | OpenAPI、事件 Schema 与接口示例 |
| `eval/` | 固定评测任务、隔离样例和判分逻辑 |
| `training/` | 独立的本地模型训练与实验工作室 |
| `scripts/` | 构建、启动、测试和 Demo 脚本 |
| `docs/` | 架构、使用方法和开发记录 |

## 环境要求

推荐使用以下环境：

- Windows 10/11 x64
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Visual Studio 2022 C++ 工具链
- Qt 6.8 的 MSVC x64 Kit
- CMake、CTest、Git 和 ripgrep
- Ninja 与 clangd 为可选项，但建议安装

项目不会自动安装 Visual Studio、Qt、CMake 等系统工具，也不会改写系统 `PATH`。本机路径放在 `scripts/local-env.cmd` 中；这个文件已被 Git 忽略，不应提交。

## 安装依赖

后端依赖使用锁文件安装：

```powershell
Set-Location .\agent
uv sync --locked
```

Qt 客户端需要先在本机准备好 MSVC 与 Qt Kit，再回到仓库根目录执行：

```powershell
.\scripts\Build-Client.cmd
```

如果脚本找不到工具，请先检查 `scripts/local-env.cmd` 中的本机路径。更完整的环境说明见 [docs/environment.md](docs/environment.md)。

## 启动

构建完成后，可以直接双击根目录的 `Start-ForgePilot.cmd`，也可以在 PowerShell 中运行：

```powershell
.\Start-ForgePilot.cmd
```

第一次启动会要求填写兼容 OpenAI Chat Completions 的服务地址、模型名和 API Key，并发送一次简短请求检查连接。配置成功后，密钥使用当前 Windows 账户加密保存在本机，不会写进项目源码。

常用入口：

| 文件 | 用途 |
| --- | --- |
| `Start-ForgePilot.cmd` | 启动后端和桌面客户端 |
| `Stop-ForgePilot.cmd` | 停止由启动器管理的进程 |
| `Configure-ForgePilot.cmd` | 更新并验证模型配置 |
| `Check-Model.cmd` | 只检查当前模型连接 |

详细操作见 [docs/user-guide.md](docs/user-guide.md)，启动器参数和故障排查见 [docs/desktop-launcher.md](docs/desktop-launcher.md)。

## 模型配置与密钥

也可以在当前 PowerShell 进程中临时设置模型参数：

```powershell
$env:FORGEPILOT_BASE_URL = 'https://your-endpoint.example/v1'
$env:FORGEPILOT_MODEL = 'your-model-name'
$env:FORGEPILOT_API_KEY = [System.Net.NetworkCredential]::new(
    '',
    (Read-Host 'API Key' -AsSecureString)
).Password
```

不要把真实密钥写进 README、脚本、命令行参数或 `.env` 后提交。仓库已经忽略以下本地内容：

- `.env` 和 `scripts/local-env.cmd`
- Python 虚拟环境
- Qt 用户预设与构建目录
- 日志、SQLite 数据库和模型训练产物
- 生成的测试结果与请求快照

如果密钥曾经进入 Git 历史，仅删除文件是不够的，应立即在服务商后台撤销并重新生成。

## 运行测试

后端测试：

```powershell
.\scripts\Test-Agent.cmd -q
```

客户端与联调测试：

```powershell
.\scripts\Test-Client.cmd -V
.\scripts\Test-ClientIntegration.cmd
```

固定离线评测：

```powershell
.\scripts\Run-Benchmark.cmd --mode offline --approval manifest --generator Ninja
```

离线测试使用受控模型和临时仓库，适合检查流程是否正确，但不能代表真实模型的修复能力。评测口径见 [docs/benchmark.md](docs/benchmark.md)。

## 训练实验环境

模型训练工作室使用 `training/.venv`，与主后端环境互不影响：

```powershell
.\scripts\Setup-Training.cmd
.\scripts\Check-Training.cmd
.\Start-Training-Studio.cmd
```

工作室默认只监听本机端口，数据集、检查点、导出权重和训练日志都保存在被忽略的 `logs/` 目录。具体说明见 [training/README.md](training/README.md)。

## 当前限制

- 主要支持本地 Windows 单用户环境
- 代码执行没有操作系统级沙箱，批准构建意味着目标项目的构建脚本和测试程序会在本机运行
- 历史任务可以查看，但不会在重启后自动续跑
- clangd 结果依赖可用的编译数据库，缺失时只能做文本级搜索
- MCP 目前只面向可信的读取/查询工具，不支持外部写操作和 OAuth
- 取消、拒绝或执行失败不会自动还原已经产生的文件修改
- 真实模型效果取决于模型能力、上下文长度和项目复杂度

## 文档

- [使用指南](docs/user-guide.md)
- [架构说明](docs/architecture.md)
- [实现取舍](docs/implementation-decisions.md)
- [MCP 接入](docs/mcp.md)
- [代码理解与 clangd](docs/code-intelligence.md)
- [协议说明](protocol/README.md)
- [开发进度](docs/development-progress.md)

如果只是想体验修改流程，建议先按 [Demo 说明](docs/demo-script.md) 创建隔离项目，不要直接拿重要仓库做第一次测试。
