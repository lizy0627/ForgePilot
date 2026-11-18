# 传播公共 include 目录

修复 subject 库消费者找不到 subject.h 的 CMake 使用要求，include 目录应公开传播。只修改 cmake/Subject.cmake。

允许修改：cmake/Subject.cmake。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
