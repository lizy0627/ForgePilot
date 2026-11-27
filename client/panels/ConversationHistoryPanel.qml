import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

ColumnLayout {
    id: root
    required property var conversations
    property string actionId: ""
    property string viewedTitle: ""
    property string viewedId: ""
    spacing: Theme.spaceSm
    function showFullTitle(id, title) {
        viewedId = id
        viewedTitle = title
        fullTitle.open()
    }
    RowLayout {
        Layout.fillWidth: true
        Layout.topMargin: Theme.spaceMd
        Layout.leftMargin: Theme.spaceSm
        Label {
            text: "最近对话"; color: Theme.secondary; font.weight: Font.DemiBold
            font.pixelSize: Theme.fontSizeSm; Layout.fillWidth: true
        }
        ActionButton {
            objectName: "refreshConversationHistoryButton"
            text: "刷新对话列表"; display: AbstractButton.IconOnly; icon.source: Theme.icon("refresh")
            flat: true; Layout.preferredWidth: 32; Layout.preferredHeight: 32
            enabled: !root.conversations.historyLoading
            onClicked: root.conversations.refreshHistory()
        }
    }
    Label {
        text: root.conversations.historyLoading ? "正在加载对话…" : "暂无历史对话"
        visible: !history.count; color: Theme.muted; font.pixelSize: Theme.fontSizeSm
        Layout.leftMargin: Theme.spaceSm; Layout.fillWidth: true; wrapMode: Text.Wrap
    }
    ListView {
        id: history; objectName: "conversationHistory"; model: root.conversations.history
        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: Theme.spaceXs
        delegate: ItemDelegate {
            id: entry; required property var modelData
            objectName: "conversationHistoryEntry_" + modelData.conversation_id
            width: history.width; height: Math.max(42, titleLabel.implicitHeight + Theme.spaceSm * 2)
            hoverEnabled: true; focusPolicy: Qt.StrongFocus
            highlighted: root.conversations.conversationId === modelData.conversation_id
            Accessible.name: modelData.title || "新对话"
            Accessible.role: Accessible.ListItem; Accessible.selected: highlighted
            leftPadding: Theme.spaceMd; rightPadding: Theme.spaceXs
            onClicked: root.conversations.selectConversation(modelData.conversation_id)
            background: Rectangle {
                radius: Theme.radiusMd
                color: Theme.buttonFill(entry, entry.highlighted ? "selected" : "flat")
                border.color: Theme.buttonBorder(entry, entry.highlighted ? "selected" : "flat")
                border.width: entry.visualFocus || entry.down ? 2 : 1
                Rectangle {
                    visible: entry.highlighted; width: 3; height: 15; radius: 2
                    x: 1; anchors.verticalCenter: parent.verticalCenter; color: Theme.accent
                }
            }
            contentItem: RowLayout {
                spacing: Theme.spaceXs
                Label {
                    id: titleLabel
                    text: entry.modelData.title || "新对话"; textFormat: Text.PlainText
                    wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                    font.pixelSize: Theme.fontSizeMd
                    verticalAlignment: Text.AlignVCenter; color: entry.highlighted ? Theme.text : Theme.secondary
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                }
                ActionButton {
                    id: moreButton
                    objectName: "conversationMore_" + entry.modelData.conversation_id
                    text: "对话选项"; icon.source: Theme.icon("more"); display: AbstractButton.IconOnly
                    flat: true; Layout.preferredWidth: 30; Layout.preferredHeight: 30
                    enabled: !root.conversations.deleting
                    onClicked: more.popup(moreButton, 0, moreButton.height)
                    Menu {
                        id: more; width: 162; padding: Theme.spaceXs
                        background: Rectangle { color: Theme.panel; radius: Theme.radiusMd; border.color: Theme.border }
                        AppMenuItem {
                            objectName: "viewConversationTitle_" + entry.modelData.conversation_id
                            text: "查看完整标题"
                            onTriggered: root.showFullTitle(entry.modelData.conversation_id, entry.modelData.title)
                        }
                        AppMenuItem {
                            objectName: "renameConversation_" + entry.modelData.conversation_id
                            text: "重命名"
                            onTriggered: { root.actionId = entry.modelData.conversation_id; nameInput.text = entry.modelData.title; rename.open() }
                        }
                        AppMenuItem {
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
    AppDialog {
        id: fullTitle; objectName: "historyConversationTitleDialog"
        title: "对话完整信息"; modal: true
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(520, parent.width - 40)
        contentItem: ColumnLayout {
            spacing: Theme.spaceSm
            LongText { objectName: "conversationTitleDetail"; Layout.fillWidth: true; title: "对话标题"; text: root.viewedTitle; expanded: true; monospace: false; maximumHeight: 210 }
            LongText { Layout.fillWidth: true; title: "对话 ID"; text: root.viewedId; maximumHeight: 100 }
        }
        footer: AppDialogButtonBox { ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole } }
    }
    AppDialog {
        id: rename; objectName: "renameConversationDialog"; title: "重命名对话"; modal: true; width: 360
        parent: Overlay.overlay; anchors.centerIn: parent
        contentItem: AppTextField { id: nameInput; objectName: "renameConversationInput"; maximumLength: 80; placeholderText: "输入对话名称"; selectByMouse: true }
        footer: AppDialogButtonBox {
            ActionButton { objectName: "saveConversationNameButton"; text: "保存"; highlighted: true; enabled: !!nameInput.text.trim(); DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole }
            ActionButton { text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
        onAccepted: root.conversations.renameConversationById(root.actionId, nameInput.text)
    }
    Connections {
        target: root.conversations
        function onBackendChanged() {
            confirm.close(); rename.close(); fullTitle.close()
            nameInput.clear(); root.actionId = ""; root.viewedId = ""; root.viewedTitle = ""
        }
        function onConversationDeleted(id) {
            if (id === root.actionId) { confirm.close(); rename.close(); nameInput.clear(); root.actionId = "" }
        }
    }
    AppDialog {
        id: confirm; objectName: "deleteConversationDialog"
        parent: Overlay.overlay; anchors.centerIn: parent
        title: "删除这段对话？"; modal: true; width: Math.min(480, parent.width - 40)
        height: Math.min(480, parent.height - 40)
        closePolicy: root.conversations.deleting ? Popup.NoAutoClose : Popup.CloseOnEscape
        contentItem: AppScrollView {
            id: deleteScroll; clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: deleteScroll.availableWidth; spacing: Theme.spaceMd
                Label { text: root.conversations.deleteTitle; textFormat: Text.PlainText; font.bold: true; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                LongText { Layout.fillWidth: true; title: "复制对话标题"; text: root.conversations.deleteTitle; monospace: false; maximumHeight: 150 }
                Label { text: "将删除对话、摘要、来源于此对话的长期记忆和相关执行记录，无法恢复。已修改的项目文件不会还原。"; wrapMode: Text.Wrap; color: Theme.text; Layout.fillWidth: true }
                Label { visible: root.conversations.deleteLoading || root.conversations.deleting || root.conversations.deleteActive; text: root.conversations.deleting ? "正在停止执行、清理资源并删除，请稍候…" : root.conversations.deleteLoading ? "正在确认运行状态…" : "本对话仍有回答或任务运行，将先停止并等待资源清理。"; wrapMode: Text.Wrap; color: Theme.warning; Layout.fillWidth: true }
                Label { visible: !!text; text: root.conversations.deleteError; textFormat: Text.PlainText; color: Theme.danger; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                LongText { visible: !!root.conversations.deleteError; Layout.fillWidth: true; title: "完整错误"; text: root.conversations.deleteError; monospace: false; maximumHeight: 150 }
            }
        }
        footer: RowLayout {
            spacing: Theme.spaceSm
            Item { Layout.fillWidth: true }
            ActionButton { objectName: "keepConversationButton"; text: "保留对话"; enabled: !root.conversations.deleting; onClicked: confirm.reject(); Layout.bottomMargin: Theme.spaceSm }
            ActionButton { objectName: "confirmDeleteConversationButton"; text: root.conversations.deleteActive ? "停止任务并删除" : "删除对话"; danger: true; enabled: !root.conversations.deleteLoading && !root.conversations.deleting; onClicked: root.conversations.confirmDelete(); Layout.rightMargin: Theme.spaceSm; Layout.bottomMargin: Theme.spaceSm }
        }
        onClosed: root.conversations.cancelDelete()
    }
}
