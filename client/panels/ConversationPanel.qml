import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "Theme.js" as Theme

Item {
    id: root
    objectName: "conversationView"
    required property var conversations
    required property var controller
    property var drafts: ({})
    property string draftId: ""
    property bool followLatest: true
    property var projectDraft: null
    readonly property bool workMode: conversations.mode === "task"
    readonly property real columnWidth: Math.max(240, Math.min(780, width - 48))
    signal openWorkDetails()
    function chooseProject(url) {
        if (!conversations.conversationId) projectDraft = prompt.text
        conversations.chooseRepository(url)
    }
    function submit() { if (sendButton.enabled) conversations.sendMessage(prompt.text) }
    function showTools(requestId) { conversations.showMcpTools(requestId); toolsDialog.open() }
    function enterPressed(event) {
        if ((event.modifiers & Qt.ShiftModifier) === 0 && !prompt.inputMethodComposing) {
            event.accepted = true; submit()
        } else event.accepted = false
    }
    Connections {
        target: root.conversations
        function onSelectionChanged() {
            root.drafts[root.draftId] = prompt.text
            root.draftId = root.conversations.conversationId
            root.followLatest = true
            prompt.text = root.projectDraft !== null ? root.projectDraft : root.drafts[root.draftId] || ""
            root.projectDraft = null
        }
        function onChanged() { if (!root.conversations.busy && !root.conversations.conversationId) root.projectDraft = null }
        function onSubmitted(id, sentPrompt) {
            if (id === root.conversations.conversationId && prompt.text === sentPrompt) prompt.clear()
            if (root.drafts[id] === sentPrompt) root.drafts[id] = ""
        }
        function onConversationDeleted(id) { delete root.drafts[id] }
    }
    FolderDialog {
        id: folder; objectName: "conversationFolderDialog"; title: "选择工作项目"; options: FolderDialog.DontUseNativeDialog
        acceptLabel: "选择项目"; rejectLabel: "取消"
        onAccepted: root.chooseProject(selectedFolder)
    }
    McpToolsDialog { id: toolsDialog; conversations: root.conversations }
    ColumnLayout {
        anchors.fill: parent; anchors.topMargin: 12; anchors.bottomMargin: 18; spacing: 12
        RowLayout {
            visible: root.conversations.hasMoreMessages || !root.followLatest
            Layout.preferredWidth: root.columnWidth; Layout.maximumWidth: root.columnWidth; Layout.minimumWidth: 0; Layout.alignment: Qt.AlignHCenter; Layout.fillHeight: false
            ActionButton { text: root.conversations.loading ? "加载中…" : "查看更早消息"; flat: true; visible: root.conversations.hasMoreMessages; enabled: !root.conversations.loading; onClicked: { root.followLatest = false; root.conversations.refresh(true) } }
            Item { Layout.fillWidth: true }
            ActionButton { text: "回到最新 ↓"; flat: true; visible: !root.followLatest; onClicked: { root.followLatest = true; messages.positionViewAtEnd() } }
        }
        Item {
            visible: !messages.count; Layout.fillWidth: true; Layout.fillHeight: true
            ColumnLayout {
                anchors.centerIn: parent; width: root.columnWidth; spacing: 16
                Label {
                    text: root.workMode ? "今天想完成什么？" : "今天想聊些什么？"
                    font.pixelSize: root.width < 480 ? 25 : 30; font.weight: Font.DemiBold
                    Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                }
                Label {
                    text: root.workMode ? "选择一个项目，告诉我你想完成的事。" : "提问、梳理思路，或探索一个新想法。"
                    font.pixelSize: 14; color: Theme.secondary; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                }
            }
        }
        ListView {
            id: messages; objectName: "conversationMessages"
            visible: count > 0; Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter; Layout.fillHeight: true
            clip: true; spacing: 26; topMargin: 12; bottomMargin: 16
            model: root.conversations.messages
            delegate: Item {
                id: row; required property var record
                readonly property bool fromUser: record.role === "user"
                width: messages.width; height: bubble.height
                TextMetrics { id: measure; text: row.record.content || ""; font.family: "Microsoft YaHei UI"; font.pixelSize: 15 }
                Rectangle {
                    id: bubble
                    anchors.right: row.fromUser ? parent.right : undefined
                    width: row.fromUser ? Math.min(row.width * 0.85, Math.max(100, measure.advanceWidth + 52)) : row.width
                    height: body.implicitHeight + (row.fromUser ? 24 : 8)
                    radius: 20; color: row.fromUser ? Theme.raised : "transparent"
                    Column {
                        id: body; width: parent.width - (row.fromUser ? 36 : 8)
                        x: row.fromUser ? 18 : 4; y: row.fromUser ? 12 : 4; spacing: 8
                        Label { visible: !row.fromUser; text: "ForgePilot"; color: Theme.text; font.pixelSize: 13; font.weight: Font.DemiBold }
                        TextEdit {
                            width: parent.width; text: row.record.content || ""; textFormat: TextEdit.PlainText
                            readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; color: Theme.text
                            selectionColor: Theme.selection; selectedTextColor: Theme.text; font.family: "Microsoft YaHei UI"; font.pixelSize: 15
                        }
                        ActionButton {
                            objectName: "messageMcpToolsButton"; visible: !row.fromUser
                            text: "工具调用与来源"; flat: true
                            onClicked: root.showTools(row.record.request_id)
                        }
                    }
                }
            }
            function follow() { if (root.followLatest) messages.positionViewAtEnd() }
            onCountChanged: Qt.callLater(follow)
            onContentHeightChanged: Qt.callLater(follow)
            onHeightChanged: Qt.callLater(follow)
            onMovementStarted: root.followLatest = false
            ScrollBar.vertical: AppScrollBar {}
        }
        RowLayout {
            visible: root.conversations.active || root.conversations.busy || root.conversations.retryPending
            Layout.preferredWidth: root.columnWidth; Layout.maximumWidth: root.columnWidth; Layout.minimumWidth: 0; Layout.alignment: Qt.AlignHCenter; Layout.fillHeight: false
            Label { text: root.conversations.active ? (root.conversations.activeMode === "chat" ? "正在思考…" : "正在处理工作…") : root.conversations.retryPending ? "发送状态待确认" : "正在发送…"; color: Theme.secondary; Layout.fillWidth: true }
            ActionButton { text: root.controller.canApprove ? "查看并确认方案" : "查看进度"; flat: true; visible: root.conversations.activeMode === "task"; onClicked: root.openWorkDetails() }
            ActionButton { objectName: "retryMessageButton"; text: "重试"; visible: root.conversations.retryPending; enabled: !root.conversations.busy; onClicked: root.conversations.retrySend() }
        }
        Label { text: root.conversations.contextNotice; visible: !!text; Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter; wrapMode: Text.Wrap; color: Theme.warning; font.pixelSize: 12; maximumLineCount: 2; elide: Text.ElideRight }
        RowLayout {
            visible: !!root.conversations.toolActivity.status
            Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter; Layout.fillHeight: false
            Label {
                objectName: "mcpToolActivity"; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText; color: Theme.secondary
                text: (root.conversations.toolActivity.status === "running" ? "正在调用 · " : root.conversations.toolActivity.status === "succeeded" ? "调用完成 · " : "调用未完成 · ") +
                      (root.conversations.toolActivity.server_name || "") + " / " + (root.conversations.toolActivity.tool_name || "")
            }
            ActionButton { objectName: "activeMcpToolsButton"; text: "查看"; flat: true; onClicked: root.showTools(root.conversations.toolActivity.request_id) }
        }
        Rectangle {
            id: composer; objectName: "messageComposer"
            Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter
            implicitHeight: composerLayout.implicitHeight + 24
            color: Theme.input; radius: 24; border.color: prompt.activeFocus ? "#d4d4d7" : Theme.border
            ColumnLayout {
                id: composerLayout; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 6
                RowLayout {
                    visible: root.workMode || !!root.conversations.repositoryPath; Layout.fillWidth: true
                    ActionButton {
                        objectName: "chooseConversationRepositoryButton"; flat: true; horizontalPadding: 10
                        text: root.conversations.repositoryPath ? "项目 · " + root.conversations.repositoryPath.replace(/\\/g, "/").split("/").pop() : "＋  选择项目"
                        Layout.maximumWidth: composer.width - 80
                        enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending
                        ToolTip.visible: hovered; ToolTip.text: root.conversations.repositoryPath || "选择一个本地项目"
                        onClicked: folder.open()
                    }
                    ActionButton { text: "×"; Accessible.name: "取消关联项目"; flat: true; horizontalPadding: 4; visible: !!root.conversations.repositoryPath; enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending; onClicked: root.conversations.clearRepository() }
                    Item { Layout.fillWidth: true }
                }
                AppScrollView {
                    Layout.fillWidth: true; Layout.preferredHeight: Math.max(56, Math.min(132, prompt.contentHeight + 16)); clip: true
                    TextArea {
                        id: prompt; objectName: "conversationPrompt"; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText
                        color: Theme.text; font.pixelSize: 15; padding: 8
                        placeholderText: root.workMode ? "描述你想完成的工作…" : "向 ForgePilot 提问…"
                        background: Rectangle { color: Theme.input; radius: 12 }
                        Keys.onReturnPressed: event => root.enterPressed(event)
                        Keys.onEnterPressed: event => root.enterPressed(event)
                        TextMenu { id: menu; objectName: "conversationPromptMenu"; editor: prompt }
                        TapHandler { acceptedButtons: Qt.RightButton; onTapped: menu.popup() }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    ActionButton { text: "＋ 项目"; flat: true; horizontalPadding: 8; visible: !root.workMode && !root.conversations.repositoryPath; enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending; onClicked: folder.open(); ToolTip.visible: hovered; ToolTip.text: "关联项目后可询问代码" }
                    Label { text: root.workMode ? (root.conversations.repositoryPath ? "修改前会请你确认方案" : "先选择要工作的项目") : root.conversations.repositoryPath ? "仅阅读和分析项目" : ""; color: Theme.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                    ActionButton { objectName: "stopAnswerButton"; text: "■"; Accessible.name: "停止回答"; highlighted: true; cornerRadius: 20; horizontalPadding: 0; Layout.preferredWidth: 40; Layout.preferredHeight: 40; visible: root.conversations.activeMode === "chat"; enabled: !root.conversations.busy; onClicked: root.conversations.stopAnswer(); ToolTip.visible: hovered; ToolTip.text: "停止回答" }
                    ActionButton { objectName: "stopTaskButton"; text: "停止工作"; visible: root.conversations.activeMode === "task"; enabled: root.controller.canCancel; onClicked: root.controller.cancelTask() }
                    ActionButton {
                        id: sendButton; objectName: "sendMessageButton"; text: "↑"; font.pixelSize: 24
                        Accessible.name: root.workMode ? "开始工作" : "发送消息"
                        highlighted: true; cornerRadius: 20; horizontalPadding: 0; Layout.preferredWidth: 40; Layout.preferredHeight: 40
                        visible: !root.conversations.active
                        enabled: !root.conversations.busy && !root.conversations.loading && !root.conversations.active && !root.conversations.retryPending && !!prompt.text.trim() && (!root.workMode || !!root.conversations.repositoryPath)
                        onClicked: root.submit(); ToolTip.visible: hovered; ToolTip.text: root.workMode ? "开始工作" : "发送消息"
                    }
                }
            }
        }
        Label { text: "Enter 发送 · Shift + Enter 换行"; color: Theme.muted; font.pixelSize: 11; Layout.alignment: Qt.AlignHCenter }
    }
}
