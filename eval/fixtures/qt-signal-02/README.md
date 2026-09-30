# 避免重复连接触发两次

修复 Counter 每次 click 触发两次更新的 Signal/Slot 问题。两次连续点击应得到 value=2、两次 changed，保留正常信号参数。

允许修改：src/subject.cpp。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
