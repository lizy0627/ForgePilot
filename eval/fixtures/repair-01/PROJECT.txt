# 编译后根据失败测试修复边界

分两阶段修复 positive_part：首轮只修复语法以便编译，保留现有计算表达式并结束本轮修改，让 Runtime 实际运行测试；然后依据失败日志在自动 Repair 中修复负数边界（负数返回0，非负数保持原值）。必须保留测试。

允许修改：src/subject.cpp。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
