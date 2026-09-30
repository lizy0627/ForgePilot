# ForgePilot Benchmark v1

15 个固定任务的定义见 `tasks.json`；`fixtures/` 是只读基线，`oracles/` 是副本外的受保护可执行判分，`offline-solutions.json` 仅用于离线评测器验证，不提供给真实模型。

入口为 `scripts/Run-Benchmark.cmd`，详细运行方式、审批边界和指标定义见 `docs/benchmark.md`。每次运行保留独立临时仓库及结果，不清理或恢复用户工作区。`offline` 报告不代表真实模型成绩。
