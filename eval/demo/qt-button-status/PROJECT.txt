# Button status demo

Qt6 / CMake / C++20 project. Run ButtonStatusDemo, click Activate, and observe the label. Expected: Ready becomes Active once. Current bug: the label remains Ready. The QObject controller, its slot, the signal and the widget connection are in src/.

User request: 修复点击按钮以后界面状态没有更新的问题。

Use the prepared demo / demo-build / demo-test presets. Keep tests and build definitions intact. The test performs an actual QPushButton click and verifies the displayed label, emitted signal argument, count and repeated-click behavior. A passing build alone is not success.
