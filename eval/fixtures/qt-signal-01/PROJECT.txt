# 恢复缺失的连接

修复 Counter 点击后数值不更新的问题。每次 click 应让 value 增加1并且只发出一次 changed(int)，保留 Qt signal/slot 机制。

允许修改：src/subject.cpp。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
