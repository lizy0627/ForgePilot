# ForgePilot 模型研发环境

本目录使用独立的 Python 3.12 / Windows x64 环境，为自定义网络、从零训练和本地模型服务准备依赖。现有 `agent.venv` 继续负责 Qt 后端与外部模型调用。

2026-09-26 已完成安装及真实 GPU/HTTP 验证，见 [配置结果与证据](../docs/training-environment-setup-2026-09-26.md)。

`pyproject.toml` 固定直接依赖，`uv.lock` 锁定完整依赖。torch 来自官方 CUDA 12.6 索引，普通依赖来自 PyPI。环境目录 `.venv` 已由仓库 `.gitignore` 忽略。

## 使用

**第一次使用：双击根目录 `Start-Training-Studio.cmd`。** 中文界面提供准备资料 → 从零训练 → 实时曲线 → 暂停恢复 → 试用导出，支持上传文本、源码、PDF、DOCX 后预览确认；可在基础模型上继续学习新资料，也可在界面直接填写问答后训练。详细步骤见 [小白操作指南](../docs/training-studio-guide.md)。

退出用页面「退出工作室」或根目录 `Stop-Training-Studio.cmd`。工作室仅监听本机 8787，不修改原 Agent 的 8765 服务或外部模型配置。

在 ForgePilot 根目录执行：

```powershell
# 按锁文件安装/修复训练环境，不更新版本，不重建已有 Agent 环境。
& .\scripts\Setup-Training.cmd

# 实际运行短 GPU 优化、GQA 注意力、权重与数据读写、临时 HTTP 服务检查。
& .\scripts\Check-Training.cmd

# 在当前 PowerShell 中选择训练环境及缓存目录，不改变系统 PATH。
. .\scripts\training-env.ps1
& 'D:\uv\uv.exe' run --project .\training --no-sync --locked python -c "import torch; print(torch.__version__, torch.cuda.get_device_name(0))"
```

Setup 脚本复用可信的 `scripts/local-env.cmd` 中的 Python 和 uv 路径。运行检查不需要外部模型 API Key，也不会调用外部模型。脚本产生唯一 JSON 报告，位于 `logs/training-environment/check-*.json`。

载入 `training-env.ps1` 后，目录约定为：

| 用途 | 路径 |
| --- | --- |
| 训练解释器 | `training/.venv/Scripts/python.exe` |
| 数据 | `logs/model-training/data/` |
| 训练 checkpoint | `logs/model-training/checkpoints/` |
| 导出权重与 tokenizer | `logs/model-training/exports/` |
| 训练日志 | `logs/model-training/runs/` |
| PyTorch 缓存 | `logs/model-training/cache/torch/`，由 `TORCH_HOME` 指定 |
| Hugging Face 缓存 | `logs/model-training/cache/huggingface/`，由 `HF_HOME` 指定 |

这些数据目录已由 `logs/` 忽略规则覆盖，不进入 Git。工作室目前使用独立的 `logs/model-training/studio/`，其下包含 datasets、runs 和 requests；可通过 `studio.py --storage` 指定其他位置。上表为命令行研究环境的通用目录约定。缓存变量仅对当前 PowerShell 及子进程生效。返回 Agent 开发可开新终端或载入原 `scripts/agent-env.ps1`。

## 验证范围

`check_environment.py` 在实际 CUDA 设备运行小型 MLP 的 24 次优化，检查 BF16、有限梯度和损失下降；另验证 causal GQA SDPA 正反向、activation checkpointing、safetensors 权重重载、优化器状态加载、自训 byte-level BPE 的中文/代码往返、NumPy mmap、YAML 和 TensorBoard。

服务检查临时监听随机 loopback 端口，真实请求 FastAPI `/health`，随后停止。它不实现 Chat Completions、不提供自研模型推理，不占用正式 Agent 的 8765 端口。临时文件只在检查报告目录下创建并清理。

上述环境检查之外，`studio_model.py`、`studio_worker.py` 和 `studio.py` 现已实现自定义因果语言模型、独立训练工作进程和可视化工作室。`check_studio.py` 可实际验证 GPU 训练、暂停恢复、问答训练、文本推理、导出和进程退出。模型结构源码随模型保存。

当前本地接口仅支持非流式文本，尚未验证工具调用；没有改动原 ForgePilot 的主模型配置，也没有实现原客户端内的双模型切换。后续接入边界见 [环境与接入方案](../docs/model-environment-audit-2026-09-26.md)。

资料上传增强增加了独立依赖 `pypdf==6.19.0`；训练环境共 47 个依赖包（锁文件 48 条含项目自身），不改变原 Agent 环境。上传解析进程有内存和超时限制，模型不会执行上传的源码。
