import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Ui.js" as Ui

AppScrollView {
    id: root
    required property var controller
    property bool testsOnly: false
    clip: true; contentWidth: availableWidth
    ColumnLayout {
        width: root.availableWidth; spacing: Theme.spaceMd
        Label { visible: !root.controller.buildReports.length; text: root.testsOnly ? "测试 · 未运行" : "配置 · 未运行    /    构建 · 未运行"; color: Theme.muted; padding: Theme.spaceLg }
        Repeater {
            model: root.controller.buildReports
            delegate: Panel {
                id: report
                required property var modelData
                Layout.fillWidth: true; title: modelData.label
                subtitle: root.testsOnly ? "CTest" : "CMake 验证"
                Label { visible: root.testsOnly; text: "通过 " + (report.modelData.tests.passed || 0) + "  ·  失败 " + (report.modelData.tests.failed || 0) + "  ·  未运行 " + (report.modelData.tests.not_run || 0); color: Theme.secondary }
                Repeater {
                    model: Ui.array(report.modelData.stages).filter(function(s) { return root.testsOnly ? s.phase === "run_tests" : s.phase !== "run_tests"; })
                    delegate: ColumnLayout {
                        id: stage
                        required property var modelData
                        Layout.fillWidth: true; spacing: Theme.spaceXs
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: stage.modelData.phase === "cmake_configure" ? "配置" : stage.modelData.phase === "cmake_build" ? "构建" : "测试"; font.bold: true; color: Theme.text }
                            StatusBadge { status: stage.modelData.status }
                            Label { text: "退出码 " + (stage.modelData.exitCode === undefined || stage.modelData.exitCode === null ? "—" : stage.modelData.exitCode) + "  ·  " + Ui.duration(stage.modelData.durationMs); color: Theme.secondary; Layout.fillWidth: true; horizontalAlignment: Text.AlignRight }
                        }
                        Label { text: stage.modelData.truncated ? "日志已由后端截断" : ""; visible: text.length > 0; color: Theme.warning }
                        LongText {
                            objectName: "log_" + stage.modelData.phase
                            Layout.fillWidth: true; title: "执行命令与日志"; maximumHeight: 240
                            text: Ui.array(stage.modelData.command).join(" ") + "\n\n标准输出（原文）\n" + (stage.modelData.stdout || "—") + "\n\n标准错误（原文）\n" + (stage.modelData.stderr || "—")
                        }
                    }
                }
                Label { text: report.modelData.error; visible: text.length > 0; color: Theme.danger; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
            }
        }
    }
}
