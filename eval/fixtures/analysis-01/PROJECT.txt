# 说明点击到状态更新的关系

只读分析从 main 到计数更新的调用链，不修改文件。最终仅输出 JSON，字段为 files（相关相对路径数组）、symbols（相关完全限定符号数组）、relations（直接调用边数组，每项 caller 和 callee）。包含 main、UI入口、Controller 和 Counter；不要把注释或旁路辅助函数当成调用边。

允许修改：无（只读）。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
