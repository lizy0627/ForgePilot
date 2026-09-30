import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

ColumnLayout {
    id: root
    required property var conversations
    property string actionId: ""
    spacing: 8
    RowLayout {
        Layout.fillWidth: true
        Label { text: "最近对话"; color: Theme.muted; font.pixelSize: 11; Layout.fillWidth: true; Layout.leftMargin: 10 }
        ActionButton { text: "↻"; flat: true; horizontalPadding: 4; Layout.preferredWidth: 30; Accessible.name: "刷新对话列表"; enabled: !root.conversations.historyLoading; onClicked: root.conversations.refreshHistory(); ToolTip.visible: hovered; ToolTip.text: "刷新对话列表" }
    }
    Label { text: root.conversations.historyLoading ? "正在加载…" : "暂无对话，发送问题即可开始。"; visible: !history.count; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
    ListView {
        id: history; objectName: "conversationHistory"; model: root.conversations.history
        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 3
        delegate: ItemDelegate {
            id: entry; required property var modelData
            width: history.width; height: 42; hoverEnabled: true
            highlighted: root.conversations.conversationId === modelData.conversation_id
            onClicked: root.conversations.selectConversation(modelData.conversation_id)
            background: Rectangle { radius: 8; color: entry.highlighted ? Theme.selection : entry.down ? Theme.pressed : entry.hovered ? Theme.hover : "transparent" }
            contentItem: RowLayout {
                Label { text: entry.modelData.title; textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; color: Theme.text; Layout.fillWidth: true }
                ActionButton {
                    id: moreButton
                    objectName: "conversationMore_" + entry.modelData.conversation_id
                    text: "⋯"; flat: true; horizontalPadding: 4; Layout.preferredWidth: 28; Accessible.name: "对话选项"
                    enabled: !root.conversations.deleting
                    onClicked: more.popup(moreButton, 0, moreButton.height)
                    Menu {
                        id: more; width: 154
                        component HistoryAction: MenuItem {
                            id: action; property bool danger: false; hoverEnabled: true
                            contentItem: Label { text: action.text; color: !action.enabled ? Theme.disabledText : action.danger ? Theme.danger : Theme.text; verticalAlignment: Text.AlignVCenter }
                            background: Rectangle { color: action.highlighted || action.hovered ? Theme.selection : Theme.panel }
                        }
                        HistoryAction {
                            objectName: "renameConversation_" + entry.modelData.conversation_id
                            text: "重命名"
                            onTriggered: { root.actionId = entry.modelData.conversation_id; nameInput.text = entry.modelData.title; rename.open() }
                        }
                        HistoryAction {
                            objectName: "deleteConversation_" + entry.modelData.conversation_id
                            text: "删除对话"; danger: true
                            onTriggered: {
                                root.actionId = entry.modelData.conversation_id
                                root.conversations.prepareDelete(root.actionId, entry.modelData.title)
                                confirm.open()
                            }
                        }
                    }
                }
            }
        }
        ScrollBar.vertical: AppScrollBar {}
    }
    ActionButton { text: "更多对话"; visible: root.conversations.historyHasMore; enabled: !root.conversations.historyLoading; onClicked: root.conversations.refreshHistory(true) }
    Dialog {
        id: rename; objectName: "renameConversationDialog"; title: "重命名对话"; modal: true; width: 360
        parent: Overlay.overlay; anchors.centerIn: parent
        contentItem: TextField { id: nameInput; objectName: "renameConversationInput"; maximumLength: 80; placeholderText: "输入对话名称"; selectByMouse: true }
        footer: DialogButtonBox {
            ActionButton { objectName: "saveConversationNameButton"; text: "保存"; enabled: !!nameInput.text.trim(); DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole }
            ActionButton { text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
        onAccepted: root.conversations.renameConversationById(root.actionId, nameInput.text)
    }
    Connections {
        target: root.conversations
        function onConversationDeleted(id) {
            if (id === root.actionId) { confirm.close(); rename.close(); nameInput.clear(); root.actionId = "" }
        }
    }
    Dialog {
        id: confirm; objectName: "deleteConversationDialog"
        parent: Overlay.overlay; anchors.centerIn: parent
        title: "删除这段对话？"; modal: true; width: Math.min(480, parent.width - 40)
        closePolicy: root.conversations.deleting ? Popup.NoAutoClose : Popup.CloseOnEscape
        contentItem: ColumnLayout {
            spacing: 12
            Label { text: root.conversations.deleteTitle; textFormat: Text.PlainText; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
            Label { text: "将删除对话、摘要、来源于此对话的长期记忆和相关执行记录，无法恢复。已修改的项目文件不会还原。"; wrapMode: Text.Wrap; color: Theme.text; Layout.fillWidth: true }
            Label { visible: root.conversations.deleteLoading || root.conversations.deleting || root.conversations.deleteActive; text: root.conversations.deleting ? "正在停止执行、清理资源并删除，请稍候…" : root.conversations.deleteLoading ? "正在确认运行状态…" : "本对话仍有回答或任务运行，将先停止并等待资源清理。"; wrapMode: Text.Wrap; color: Theme.warning; Layout.fillWidth: true }
            Label { visible: !!text; text: root.conversations.deleteError; textFormat: Text.PlainText; color: Theme.danger; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
        }
        footer: RowLayout {
            spacing: 6
            Item { Layout.fillWidth: true }
            ActionButton { objectName: "keepConversationButton"; text: "保留对话"; enabled: !root.conversations.deleting; onClicked: confirm.reject(); Layout.bottomMargin: 6 }
            ActionButton { objectName: "confirmDeleteConversationButton"; text: root.conversations.deleteActive ? "停止任务并删除" : "删除对话"; danger: true; enabled: !root.conversations.deleteLoading && !root.conversations.deleting; onClicked: root.conversations.confirmDelete(); Layout.rightMargin: 6; Layout.bottomMargin: 6 }
        }
        onClosed: root.conversations.cancelDelete()
    }
}
