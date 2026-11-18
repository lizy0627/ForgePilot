import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme

Controls.Panel {
    id: root
    required property var mission
    property int selectedCommand: -1
    property int offset: 0
    readonly property var command: selectedCommand >= 0 ? mission.validation.get(selectedCommand) : ({})
    readonly property string output: command.output || ""
    readonly property var report: mission.validation.latest.content || ({})
    title: "Validation · 构建与测试"
    subtitle: mission.validation.status || "未运行"
    objectName: "validationPanel"
    StateNotice { mission: root.mission; hasData: !!root.mission.validation.latest.artifact_id; emptyText: "验证尚未运行。"; Layout.fillWidth: true }
    Label { visible: !!root.report.summary; text: root.report.summary || ""; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Label {
        visible: !!root.report.status
        text: "结果：" + root.report.status + " · 通过 " + (root.report.tests_passed || 0) + " · 失败 " + (root.report.tests_failed || 0)
        color: root.report.status === "passed" ? Theme.success : Theme.warning
        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
    }
    ListView {
        id: commands; objectName: "missionValidationCommands"; visible: root.mission.validation.count > 0
        Layout.fillWidth: true; Layout.preferredHeight: Math.min(210, contentHeight + 2); clip: true; spacing: Theme.spaceXs
        model: root.mission.validation
        delegate: ColumnLayout {
            id: commandEntry
            required property var record
            required property int index
            width: commands.width; spacing: Theme.spaceXs
            Controls.ActionButton {
                id: commandButton
                Layout.fillWidth: true
                implicitHeight: Math.max(Theme.controlHeight, commandPreview.implicitHeight + Theme.spaceSm * 2)
                text: (commandEntry.record.exitCode === 0 ? "✓ " : "! ") + commandEntry.record.command
                flat: true; checked: root.selectedCommand === commandEntry.index
                Accessible.name: "查看命令结果 " + commandEntry.record.command
                onClicked: { root.selectedCommand = commandEntry.index; root.offset = 0 }
                contentItem: Label {
                    id: commandPreview
                    text: commandButton.text; color: commandButton.checked ? Theme.onAccent : Theme.text
                    font: commandButton.font; verticalAlignment: Text.AlignVCenter
                    textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                }
            }
            Controls.LongText {
                objectName: "missionValidationCommandFull"
                visible: commandPreview.truncated || commandEntry.record.command.length > 90
                Layout.fillWidth: true; title: "完整验证命令"; text: commandEntry.record.command
                maximumHeight: 160
            }
        }
        ScrollBar.vertical: Controls.AppScrollBar {}
    }
    Label {
        visible: root.selectedCommand >= 0; text: "退出码：" + root.command.exitCode
        color: root.command.exitCode === 0 ? Theme.success : Theme.danger
        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
    }
    Controls.AppScrollView {
        id: validationScroll
        objectName: "missionValidationScroll"
        visible: root.selectedCommand >= 0
        Layout.fillWidth: true; Layout.preferredHeight: 260; clip: true
        Controls.AppTextArea {
            id: outputText
            objectName: "missionValidationOutput"
            readOnly: true; selectByMouse: true; wrapMode: TextEdit.NoWrap; textFormat: TextEdit.PlainText
            text: root.output.substring(root.offset, root.offset + 12000)
            font.family: Theme.fontMono; font.pixelSize: Theme.fontSizeSm
        }
    }
    ColumnLayout { visible: root.selectedCommand >= 0; Layout.fillWidth: true
        Label {
            text: root.output.length ? "日志 " + (root.offset + 1) + "–" + Math.min(root.output.length, root.offset + 12000) + " / " + root.output.length : "无输出摘要"
            color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
        }
        RowLayout { Layout.fillWidth: true
            Controls.ActionButton {
                objectName: "missionValidationCopyButton"; text: "复制本段"; enabled: root.output.length > 0
                onClicked: { outputText.selectAll(); outputText.copy(); outputText.deselect() }
            }
            Controls.ActionButton { text: "上一段"; enabled: root.offset > 0; onClicked: root.offset = Math.max(0, root.offset - 12000) }
            Controls.ActionButton { text: "下一段"; enabled: root.offset + 12000 < root.output.length; onClicked: root.offset += 12000 }
        }
    }
}
