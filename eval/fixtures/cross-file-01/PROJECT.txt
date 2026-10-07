# 为总价 API 增加数量参数

跨文件将 subtotal API 改为接受 unit 和 quantity 两参数，计算乘积，并更新 order_total 调用方。同步修改头文件、定义和调用处，数量0应返回0。

允许修改：src/subject.h、src/subject.cpp、src/app.cpp。
现有 tests/public_test.cpp 和评测逻辑不允许修改。
