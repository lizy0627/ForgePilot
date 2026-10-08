# ForgePilot

ForgePilot 是一个面向本地 CMake、C++ 和 Qt 仓库的 Windows 桌面研发助手。它提供普通问答、多轮历史和记忆，也能在用户选定仓库后读取代码、生成计划，并在明确批准后修改文件、构建和运行测试。

项目目前是开发中的个人项目，不是成熟的自动修复产品。离线流程、桌面生命周期、构建和回归测试已经覆盖主要路径，但真实任务效果仍取决于模型、目标仓库和本机工具链。首次使用请选择可丢弃的测试仓库，并在执行前自行备份重要内容。

## 支持环境

当前支持范围以仓库中的实际约束为准：

| 组件 | 支持范围 |
| --- | --- |
| 操作系统 | Windows 10/11 x64；其他系统未适配或验证 |
| Python | CPython 3.12.x；后端声明为 `>=3.12,<3.13` |
| Visual Studio | Visual Studio 2022，MSVC v143，x64 C++ 工具链 |
| Qt | Qt 6.8.x；CMake 配置的最低要求为 6.8，当前实际验证版本为 6.8.3，不对其他版本作兼容性承诺；需要 MSVC x64 Kit，以及 Network、Quick、Qml、QuickControls2、QuickDialogs2、Concurrent 和 Test 组件 |
| CMake | 3.21 或更高 |
| uv | 需要支持 `uv sync --locked --python` 的 Windows 版本；本仓库实际验证过 uv 0.12.15，不声明更旧版本兼容性 |

Git 是克隆仓库和工作模式的必需工具。CTest 随 CMake 安装；Ninja、ripgrep 和 clangd 只在相应工作流中需要。

## 从全新克隆安装

以下命令在 PowerShell 中从父目录执行。uv 必须已经位于 `PATH`；安装脚本不会下载 Python，也不会修改系统 `PATH`。

```powershell
git clone https://github.com/lizy0627/ForgePilot.git
Set-Location .\ForgePilot

Copy-Item .\scripts\local-env.example.cmd .\scripts\local-env.cmd
notepad .\scripts\local-env.cmd

.\scripts\Setup-Agent.cmd
.\scripts\Check-Environment.cmd
.\scripts\Build-Client.cmd
.\Configure-ForgePilot.cmd
.\Start-ForgePilot.cmd
```

`Setup-Agent.cmd` 使用 `agent/uv.lock` 执行锁定安装，并把环境固定创建在仓库根目录的 `agent.venv`。Python 候选顺序为：有效的 `FORGEPILOT_PYTHON`、已有的根 `agent.venv`、PATH 中能够验证为 3.12 的 Python。脚本设置 `UV_PYTHON_DOWNLOADS=never`，因此缺少本机 Python 3.12 时会明确失败。旧的 `agent/.venv` 不受支持，发现时只提示而不自动删除。

## 本机工具配置

`scripts/local-env.example.cmd` 是公开模板。复制后的 `scripts/local-env.cmd` 已被 Git 忽略，只能保存本机工具路径，不要放模型密钥、令牌或其他凭据。

| 变量 | 含义 | 何时需要 |
| --- | --- | --- |
| `FORGEPILOT_VS_ROOT` | Visual Studio 2022 安装根目录；其下必须存在 `Common7\Tools\VsDevCmd.bat` | 构建客户端、集成测试和需要 MSVC 的开发流程 |
| `FORGEPILOT_QT_KIT` | Qt 6.8.x MSVC x64 Kit 根目录；其下必须存在 `bin\windeployqt.exe` | 构建、部署和运行客户端测试 |
| `FORGEPILOT_CMAKE` | `cmake.exe` 的绝对路径；CTest 默认取同目录的 `ctest.exe` | 客户端构建、客户端测试和工作模式构建 |
| `FORGEPILOT_PYTHON` | 可选的 Python 3.12 解释器覆盖；留空时使用根 `agent.venv` | 自定义 Python 安装或安装阶段 |
| `FORGEPILOT_UV` | `uv.exe` 的绝对路径 | 评测和部分开发脚本；首次安装仍要求 uv 位于 PATH |
| `FORGEPILOT_NINJA` | 可选的 `ninja.exe` 绝对路径 | 选择 Ninja 生成器的构建或测试 |
| `FORGEPILOT_RG` | 可选的 `rg.exe` 绝对路径 | 更快的仓库文本搜索 |
| `FORGEPILOT_CLANGD` | 可选的 `clangd.exe` 绝对路径 | C/C++ 定义和引用的语义查询 |

检查完整开发环境：

```powershell
.\scripts\Check-Environment.cmd
```

普通问答只检查 Python 运行环境，不要求开发工具：

```powershell
.\scripts\Check-Environment.cmd runtime
```

客户端构建检查会严格验证 Visual Studio、Qt Kit、`windeployqt.exe` 和 CMake：

```powershell
.\scripts\Check-Environment.cmd build
```

## 构建客户端

公开的 `client/CMakePresets.json` 提供 `msvc2022-x64` configure preset 和绑定它的 `release` build preset。前者固定使用 Visual Studio 2022、x64、v143，并从 `FORGEPILOT_QT_KIT` 获取 Qt 路径；后者使用 Release 配置。个人可以保留被忽略的 `client/CMakeUserPresets.json`，但标准构建不依赖它。

```powershell
.\scripts\Build-Client.cmd
```

构建结果位于 `client/build/Release/`。启动器要求客户端已经构建，不会在普通启动过程中隐式安装开发工具或调用 MSVC。

可直接查看公开 preset：

```powershell
cmake --list-presets -S client
```

## 预构建 Windows 包

本地打包入口会复用已完成的 Release 客户端构建，并生成 `artifacts/ForgePilot-<version>-windows-x64.zip` 和对应的 `.sha256` 校验文件：

```powershell
.\scripts\Build-Client.cmd
.\scripts\Package-Windows.cmd -Version 0.1.0 -Verify
```

发布包包含预编译的 `ForgePilotClient.exe`、Qt 运行库和插件、Python 后端源码与锁文件、四个根启动入口、安装和自检脚本，以及 `local-env.example.cmd`。它不包含模型配置、API Key、DPAPI 密钥、日志、数据库、测试缓存或编译中间文件。

该 ZIP 不是完全自包含包。目标 Windows x64 机器仍需预先安装 CPython 3.12.x，并把 uv 放入 `PATH`；首次解压后运行：

```powershell
.\scripts\Setup-Agent.cmd
.\scripts\Check-Release.cmd
.\Configure-ForgePilot.cmd
.\Start-ForgePilot.cmd
```

普通问答无需安装 Visual Studio、Qt SDK 或 CMake。Qt 运行库已随包部署；包内带有 `vc_redist.x64.exe`，但目标机器缺少 Microsoft Visual C++ Runtime 时仍需由用户安装该运行库。`Check-Release.cmd` 只使用占位模型配置在本机回环地址启动后端并读取 OpenAPI，不会调用真实模型或公网 MCP。

## 首次模型配置

运行以下命令进入交互配置：

```powershell
.\Configure-ForgePilot.cmd
```

需要提供：

- Base URL：兼容 OpenAI Chat Completions 的服务根地址。
- model：服务端可用的模型标识。
- API Key：在隐藏输入框中输入；配置程序会发送一次最多等待 30 秒的简短请求验证连接。

也可以在当前 PowerShell 进程中设置 `FORGEPILOT_BASE_URL`、`FORGEPILOT_MODEL` 和 `FORGEPILOT_API_KEY`，作为交互配置的默认值或密钥来源。不要把 API Key 写入 README、`local-env.cmd`、命令行参数、源码或可提交的 `.env`。

非秘密配置保存在：

```text
%LOCALAPPDATA%\ForgePilot\launcher\<repository-hash>\config.json
```

API Key 单独保存在同目录的 `api-key.dpapi`，使用当前 Windows 账户的 DPAPI 加密。密钥不会写入仓库、普通日志或子进程命令参数；客户端进程也不会继承该密钥。更换 Windows 账户或 DPAPI 数据损坏后，需要重新配置。

## 启动与管理

```powershell
# 启动后端和桌面客户端
.\Start-ForgePilot.cmd

# 请求当前 ForgePilot 实例正常停止
.\Stop-ForgePilot.cmd

# 重新填写并验证 Base URL、model 和 API Key
.\Configure-ForgePilot.cmd

# 使用已保存配置和密钥检查模型连接
.\Check-Model.cmd
```

默认后端只监听本机回环地址的 8765 端口。停止脚本只操作由当前仓库启动器管理的进程，不会根据陈旧 PID 杀死无关进程。

## 普通问答与工作模式

普通问答不要求选择仓库，也不要求 Visual Studio、Qt、CMake、CTest、Ninja、ripgrep 或 clangd。它使用已保存的模型连接，支持连续对话、历史记录和记忆。

工作模式面向本地 Git 仓库，流程是：

1. 只读检查仓库并生成分析和计划。
2. 在界面中展示文件范围、步骤和风险。
3. 只有界面中的人工批准才允许进入修改阶段；模型声称获得授权不算批准。
4. 修改范围扩大时需要重新确认。
5. 配置了构建工具时，按批准的选择运行 CMake、构建和 CTest，并保留真实命令结果。

缺少构建工具时普通问答仍可用，但工作任务不会伪造构建成功；它会返回明确的工具不可用或构建配置错误。取消、拒绝或失败不会自动还原已经写入的文件。

## 可选能力与降级

- MCP：可在客户端设置中连接服务器、发现目录并选择兼容的只读工具。未配置、连接失败或凭据不可用时，MCP 状态会显示不可用，内置功能仍可使用。外部 MCP 指令和结果被视为不可信参考数据。
- clangd：存在有效编译数据库和 clangd 时，可做 C/C++ 定义与引用查询。clangd、编译数据库或语义查询不可用时，会退回文本候选，结果标记为非语义，并附带实际 `fallback_reason`；不会偷偷运行 CMake 来生成数据库。
- ripgrep：存在时用于更快的文本搜索；缺少时需要仓库搜索的工作功能会明确不可用，普通聊天不受影响。
- CMake/CTest：只在客户端构建、测试和获批的工作任务中使用。缺失时相关操作失败并报告不可用，不影响普通聊天。
- Ninja：仅在选择 Ninja 生成器时需要；Visual Studio preset 不依赖它。

## 测试

先完成 Python 环境和 `local-env.cmd` 配置。客户端与集成测试还要求已完成 Release 构建。

```powershell
# 后端测试
.\scripts\Test-Agent.cmd -q

# CTest 客户端测试；-V 输出详细测试过程
.\scripts\Test-Client.cmd -V

# 后端与客户端集成测试
.\scripts\Test-ClientIntegration.cmd
```

固定离线评测是额外的开发入口：

```powershell
.\scripts\Run-Benchmark.cmd --mode offline --approval manifest --generator Ninja
```

离线评测使用受控任务和样例，不代表真实模型在任意仓库中的修复能力。

`.github/workflows/ci.yml` 在 Windows Server 2022、Python 3.12 和固定的 uv 0.12.15 上，从干净 checkout 运行锁定安装，检查根 `agent.venv`、锁文件、Python 编译、离线核心 Agent 测试和协议重新导出。当前 CI 不构建 Qt 客户端；Qt 构建和 CTest 仍由上述本地命令验证。

## 常见问题

### 缺少 `agent.venv`

从仓库根目录重新运行：

```powershell
.\scripts\Setup-Agent.cmd
```

确认 uv 位于 PATH，并且 `FORGEPILOT_PYTHON`、已有根环境或 PATH 中至少有一个可验证的 Python 3.12。不要把旧 `agent/.venv` 当作当前环境，也不要依赖脚本自动删除它。

### 缺少 `scripts/local-env.cmd`

普通问答可以只使用根 Python 环境；构建和完整开发检查需要本机配置：

```powershell
Copy-Item .\scripts\local-env.example.cmd .\scripts\local-env.cmd
notepad .\scripts\local-env.cmd
.\scripts\Check-Environment.cmd
```

### Qt 或 CMake preset 不可用

确认 `FORGEPILOT_QT_KIT` 指向 Qt 6.8.x 的 MSVC x64 Kit，`FORGEPILOT_CMAKE` 指向存在的 `cmake.exe`，然后运行：

```powershell
.\scripts\Check-Environment.cmd build
cmake --list-presets -S client
.\scripts\Build-Client.cmd
```

输出应包含 `msvc2022-x64`。标准构建不需要 `CMakeUserPresets.json`；如果个人覆盖文件定义了冲突的同名 preset，请改用不同名称或暂时移开个人覆盖文件。

### 模型返回 401 或 403

401 通常表示密钥无效、过期或目标地址不接受该认证；403 通常表示账户、模型或端点权限不足。运行 `Configure-ForgePilot.cmd` 重新输入 Base URL、model 和 API Key，再运行 `Check-Model.cmd`。不要把密钥粘贴到日志、Issue 或聊天记录；怀疑泄漏时应立即在服务商处撤销并换新。

### 8765 端口被占用

先运行：

```powershell
.\Stop-ForgePilot.cmd
```

如果仍提示端口占用，说明可能有其他程序正在监听。检查本机端口占用和 `%LOCALAPPDATA%\ForgePilot\launcher\<repository-hash>\runtime.json`，不要根据其中保存的旧 PID 强制终止进程。也可以在本机 `config.json` 中选择未占用端口后重新启动。

## 安全边界

- 后端只接受本机回环访问，并限制 Host 和跨源请求；这不是操作系统级沙箱。
- 工作模式只允许写入本次批准的文件范围，但获批的构建会执行目标仓库自己的 CMake、编译和测试程序。只对可信仓库批准执行。
- 文件修改、失败的构建和取消操作不会自动回滚。开始前请提交当前工作、制作备份，或使用隔离克隆。
- 模型输出、仓库文本和 MCP 数据都可能不可靠。批准前应核对修改范围、命令和风险。
- 不要把密钥、令牌、私钥或本机秘密写入仓库。若秘密曾进入 Git 历史，仅删除工作区文件不够，必须撤销并更换。
- 日志和本地数据库可能包含项目内容与任务历史，应按敏感开发数据保护。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `agent/` | Python 后端、任务运行时、HTTP/SSE API、存储与测试 |
| `client/` | C++20、Qt 6/QML 桌面客户端、公开 CMake presets 与客户端测试 |
| `eval/` | 固定离线评测任务、样例、判分器和运行器 |
| `protocol/` | OpenAPI、事件 Schema 和接口示例 JSON |
| `scripts/` | 安装、环境检查、构建、启动、测试和评测入口 |

根目录的四个 `.cmd` 文件分别负责启动、停止、重新配置和连接检查。仓库不依赖额外的 Markdown 文档；本文件是唯一受版本控制的项目说明。
