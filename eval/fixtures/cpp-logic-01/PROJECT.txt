# 闭区间截断

修复 clamp_level，应截断到闭区间 [0,10]，10 和大于 10 的输入都返回 10。

允许修改：src/subject.cpp。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
