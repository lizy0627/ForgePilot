# 补齐库的实现源文件

修复 CMake subject 静态库遗漏实现文件引起的链接错误。只修改 cmake/Subject.cmake，保持源码和测试。

允许修改：cmake/Subject.cmake。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
