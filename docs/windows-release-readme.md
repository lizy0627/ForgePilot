# ForgePilot Windows x64 发布包

本 ZIP 含 ForgePilot 的 Qt 客户端、Python 后端冻结后的 `bin\ForgePilotAgent.exe` 和运行所需的依赖。目标 Windows x64 机器无需安装 Python、uv 或 Qt SDK。普通聊天不需要 Visual Studio/CMake；执行 C++/Qt 代码任务仍需可用的 CMake、CTest 与相应编译工具链。模型连接需要用户自己的 API 配置和网络访问。

1. 将 ZIP 完整解压到可写目录，保留目录结构。
2. 双击 `scripts\Check-Release.cmd`，运行 Qt/QML 客户端加载与后端 OpenAPI 的离线自检。后端使用占位密钥和本机回环地址，不会调用真实模型或公网 MCP。
3. 双击 `Configure-ForgePilot.cmd`，在 GUI 中配置模型地址、模型名称和 API Key。保存配置不发送模型请求；显式点击连接检测或运行 `Check-Model.cmd` 才发送真实请求，可能产生费用。配置与加密密钥仅保存在当前 Windows 用户的本机目录，受控重启后生效。
4. 双击 `Start-ForgePilot.cmd` 启动；使用 `Stop-ForgePilot.cmd` 关闭。`Check-Model.cmd` 使用已保存的配置和密钥再次检查各模型，仅在准备好消耗模型额度时运行。

Qt 客户端依赖 Microsoft Visual C++ Runtime；本包附有 `client\build\Release\vc_redist.x64.exe`，仅在目标机器缺少运行库时需要安装。Windows 安全策略仍可能阻止未知发行者的可执行文件，此时请检查 Windows 应用控制事件。

目录中不应有 API Key、模型配置、任务历史、数据库或日志。运行后生成的数据位于用户本机配置目录及解压目录的 `logs` 下。
