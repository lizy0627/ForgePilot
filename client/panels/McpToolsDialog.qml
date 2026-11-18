import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

Dialog {
    id: root
    required property var conversations
    objectName: "mcpToolsDialog"; title: "工具调用与来源"; modal: true
    parent: Overlay.overlay; anchors.centerIn: parent
    width: Math.min(760, parent.width - 40); height: Math.min(650, parent.height - 40)
    standardButtons: Dialog.Close
    onClosed: conversations.closeMcpTools()
    Connections {
        target: root.conversations
        function onMcpToolsChanged() { if (!root.conversations.mcpToolsRequest && root.visible) root.close() }
    }
    function statusText(status) {
        return ({running: "调用中", succeeded: "成功", failed: "失败", cancelled: "已取消", interrupted: "服务中断"})[status] || status
    }
    contentItem: AppScrollView {
        id: scroll; contentWidth: availableWidth; clip: true
        ColumnLayout {
            width: scroll.availableWidth; spacing: Theme.spaceMd
            Label { visible: root.conversations.mcpToolsLoading; text: "正在读取工具记录…"; color: Theme.secondary }
            Label { objectName: "mcpToolsError"; text: root.conversations.mcpToolsError; visible: !!text; textFormat: Text.PlainText; color: Theme.danger; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight; Layout.fillWidth: true }
            LongText { visible: !!root.conversations.mcpToolsError; title: "完整工具记录错误"; text: root.conversations.mcpToolsError; monospace: false; maximumHeight: 160; Layout.fillWidth: true }
            ActionButton { visible: !!root.conversations.mcpToolsError; text: "重试"; enabled: !root.conversations.mcpToolsLoading; Layout.alignment: Qt.AlignLeft; onClicked: root.conversations.refreshMcpTools() }
            Label { visible: !root.conversations.mcpToolsLoading && !root.conversations.mcpToolsError && !root.conversations.mcpTools.length; text: "本轮没有 MCP 工具调用。"; color: Theme.secondary }
            Repeater {
                model: root.conversations.mcpTools
                delegate: Rectangle {
                    id: callRow; required property var modelData
                    Layout.fillWidth: true; implicitHeight: detail.implicitHeight + 24
                    color: Theme.raised; radius: Theme.radiusLg
                    ColumnLayout {
                        id: detail; anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.spaceMd } spacing: Theme.spaceSm
                        Label { text: callRow.modelData.server_name + " / " + callRow.modelData.tool_name; textFormat: Text.PlainText; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                        Label { text: root.statusText(callRow.modelData.status) + " · " + callRow.modelData.duration_ms + " ms" + (callRow.modelData.error_code ? " · " + callRow.modelData.error_code : ""); textFormat: Text.PlainText; color: callRow.modelData.status === "failed" ? Theme.danger : Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                        Label { text: "参数摘录"; color: Theme.secondary }
                        TextEdit { text: callRow.modelData.arguments_excerpt; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; color: Theme.text; Layout.fillWidth: true }
                        Label { text: "结果摘录"; color: Theme.secondary }
                        TextEdit { text: callRow.modelData.result_excerpt; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; color: Theme.text; Layout.fillWidth: true }
                        Repeater {
                            model: callRow.modelData.sources || []
                            delegate: ColumnLayout {
                                required property var modelData; Layout.fillWidth: true
                                Label { text: parent.modelData.title; textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                                ActionButton {
                                    objectName: "mcpSourceLink"; text: "打开来源 ↗"; flat: true
                                    onClicked: root.conversations.openMcpSource(parent.modelData.url)
                                }
                                Label { text: parent.modelData.url; textFormat: Text.PlainText; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; font.pixelSize: Theme.fontSizeSm }
                            }
                        }
                    }
                }
            }
        }
    }
}
