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
    property string selectedDraftKey: ""
    property bool followLatest: true
    property var projectDraft: null
    readonly property bool workMode: conversations.mode === "task"
    readonly property bool empty: messages.count === 0
    readonly property real columnWidth: Math.max(0, Math.min(empty ? 680 : 780, width - Theme.spaceXl * 2))
    readonly property real welcomeRoom: height - composer.height - (statusScroll.visible ? statusScroll.height : 0)
    signal openWorkDetails()
    signal convertToAgentTask(string selectedText, string sourceLabel)
    function draftKey(id) { return conversations.baseUrl.toString() + "/" + id }
    Component.onCompleted: selectedDraftKey = draftKey(conversations.conversationId)
    function chooseProject(url) {
        if (!conversations.conversationId) projectDraft = prompt.text
        conversations.chooseRepository(url)
    }
    function submit() { if (sendButton.enabled) conversations.sendMessage(prompt.text) }
    function enterPressed(event) {
        if ((event.modifiers & Qt.ShiftModifier) === 0 && !prompt.inputMethodComposing) {
            event.accepted = true; submit()
        } else event.accepted = false
    }
    Connections {
        target: root.conversations
        function onBackendChanged() {
            root.projectDraft = null
            folder.close()
        }
        function onSelectionChanged() {
            root.drafts[root.selectedDraftKey] = prompt.text
            root.selectedDraftKey = root.draftKey(root.conversations.conversationId)
            root.followLatest = true
            prompt.text = root.projectDraft !== null ? root.projectDraft : root.drafts[root.selectedDraftKey] || ""
            root.projectDraft = null
        }
        function onChanged() { if (!root.conversations.busy && !root.conversations.conversationId) root.projectDraft = null }
        function onSubmitted(id, sentPrompt) {
            if (id === root.conversations.conversationId && prompt.text === sentPrompt) prompt.clear()
            const key = root.draftKey(id)
            if (root.drafts[key] === sentPrompt) root.drafts[key] = ""
        }
        function onConversationDeleted(id) { delete root.drafts[root.draftKey(id)] }
    }
    FolderDialog {
        id: folder; objectName: "conversationFolderDialog"; title: "选择工作项目"; options: FolderDialog.DontUseNativeDialog
        acceptLabel: "选择项目"; rejectLabel: "取消"
        onAccepted: root.chooseProject(selectedFolder)
    }
    ColumnLayout {
        anchors.fill: parent; anchors.topMargin: Theme.spaceMd; anchors.bottomMargin: Theme.spaceLg; spacing: Theme.spaceMd
        RowLayout {
            visible: root.conversations.hasMoreMessages || !root.followLatest
            Layout.preferredWidth: root.columnWidth; Layout.maximumWidth: root.columnWidth; Layout.minimumWidth: 0; Layout.alignment: Qt.AlignHCenter; Layout.fillHeight: false
            ActionButton { text: root.conversations.loading ? "加载中…" : "查看更早消息"; flat: true; visible: root.conversations.hasMoreMessages; enabled: !root.conversations.loading; onClicked: { root.followLatest = false; root.conversations.refresh(true) } }
            Item { Layout.fillWidth: true }
            ActionButton { text: "回到最新"; icon.source: Theme.icon("chevron-down"); flat: true; visible: !root.followLatest; onClicked: { root.followLatest = true; messages.positionViewAtEnd() } }
        }
        Item { visible: root.empty; Layout.fillHeight: true; Layout.minimumHeight: 0 }
        ColumnLayout {
            visible: root.empty && root.welcomeRoom >= 118
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredWidth: root.columnWidth
            Layout.bottomMargin: root.welcomeRoom > 220 ? Theme.spaceLg : 0
            spacing: Theme.spaceSm
            Image {
                visible: root.welcomeRoom > 215
                source: Theme.brandMark; sourceSize: Qt.size(128, 128)
                Layout.preferredWidth: 64; Layout.preferredHeight: 64
                Layout.alignment: Qt.AlignHCenter; Layout.bottomMargin: Theme.spaceSm
            }
            Label {
                text: root.conversations.loading ? "正在载入对话…" : root.workMode ? "准备开始一项工作" : "今天想聊些什么？"
                font.pixelSize: root.welcomeRoom > 215 ? 30 : Theme.fontSizeXl
                font.weight: Font.DemiBold; color: Theme.text
                Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter
            }
            Label {
                visible: !root.conversations.loading && root.welcomeRoom > 160
                text: root.workMode ? "选择一个本地项目，描述预期的改动。" : "直接提问，或关联本地项目来讨论代码。"
                font.pixelSize: Theme.fontSizeMd; color: Theme.secondary
                Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
            }
        }
        ListView {
            id: messages; objectName: "conversationMessages"
            visible: count > 0; Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter; Layout.fillHeight: true
            clip: true; spacing: Theme.spaceXl; topMargin: Theme.spaceMd; bottomMargin: Theme.spaceLg
            model: root.conversations.messages
            delegate: Item {
                id: row; required property var record
                readonly property bool fromUser: record.role === "user"
                visible: fromUser || !!record.content
                width: messages.width; height: visible ? bubble.height : 0
                TextMetrics { id: measure; text: row.record.content || ""; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeMd + 1 }
                Rectangle {
                    id: bubble
                    anchors.right: row.fromUser ? parent.right : undefined
                    width: row.fromUser ? Math.min(row.width * 0.85, Math.max(154, measure.advanceWidth + 52)) : row.width
                    height: body.implicitHeight + (row.fromUser ? Theme.spaceMd * 2 : Theme.spaceSm)
                    radius: Theme.radiusLg; color: row.fromUser ? Theme.panel : Theme.transparent
                    Column {
                        id: body; width: parent.width - (row.fromUser ? Theme.spaceLg * 2 : Theme.spaceSm)
                        x: row.fromUser ? Theme.spaceLg : Theme.spaceXs; y: row.fromUser ? Theme.spaceMd : Theme.spaceXs; spacing: Theme.spaceSm
                        Label { text: row.fromUser ? "你" : "ForgePilot"; color: row.fromUser ? Theme.secondary : Theme.accent; font.pixelSize: Theme.fontSizeSm; font.weight: Font.DemiBold }
                        Label {
                            visible: !row.fromUser && !!row.record.streaming && !!root.conversations.answerStatus
                            text: root.conversations.answerStatus
                            textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                            width: parent.width; wrapMode: Text.WrapAnywhere
                        }
                        TextEdit {
                            width: parent.width; text: row.record.content || ""; textFormat: TextEdit.PlainText
                            readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: Theme.text
                            selectionColor: Theme.selection; selectedTextColor: Theme.text; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeMd + 1
                        }
                        ActionButton {
                            visible: row.fromUser && !!row.record.content
                            text: "转为任务"; icon.source: Theme.icon("flow"); flat: true; font.pixelSize: Theme.fontSizeSm
                            Accessible.name: "仅将这条消息转换为 Agent 任务"
                            onClicked: root.convertToAgentTask(row.record.content, "已选择一条用户消息")
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
        AppScrollView {
            id: statusScroll; objectName: "chatStatusScroll"
            visible: activityRow.visible || contextNotice.visible
            Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter
            Layout.minimumHeight: 0
            Layout.preferredHeight: Math.min(statusColumn.implicitHeight, Math.max(80, root.height * 0.18))
            clip: true; contentWidth: availableWidth
            ColumnLayout {
                id: statusColumn; width: statusScroll.availableWidth; spacing: Theme.spaceSm
                RowLayout {
                    id: activityRow
                    visible: root.conversations.active || root.conversations.busy || root.conversations.retryPending
                    Layout.fillWidth: true
                    Label {
                        id: activityStatus
                        text: root.conversations.active ? (root.conversations.activeMode === "chat" ? root.conversations.answerStatus : "正在处理工作…") : root.conversations.retryPending ? "发送状态待确认" : "正在发送…"
                        color: Theme.secondary; Layout.fillWidth: true; Layout.minimumWidth: 0; textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                    }
                    ActionButton { text: root.controller.canApprove ? "查看并确认方案" : "查看进度"; flat: true; visible: root.conversations.activeMode === "task"; onClicked: root.openWorkDetails() }
                    ActionButton { objectName: "retryMessageButton"; text: "重试"; visible: root.conversations.retryPending; enabled: !root.conversations.busy; onClicked: root.conversations.retrySend() }
                }
                LongText {
                    objectName: "chatActivityDetail"
                    visible: activityRow.visible && activityStatus.truncated
                    Layout.fillWidth: true
                    title: "完整处理状态"; text: activityStatus.text; monospace: false; maximumHeight: 160
                }
                Label {
                    id: contextNotice
                    text: root.conversations.contextNotice; visible: !!text
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                    wrapMode: Text.WrapAnywhere; color: Theme.warning; font.pixelSize: Theme.fontSizeSm
                    maximumLineCount: 2; elide: Text.ElideRight
                }
                LongText {
                    objectName: "chatContextDetail"
                    visible: contextNotice.visible && contextNotice.truncated
                    Layout.fillWidth: true
                    title: "完整提示"; text: root.conversations.contextNotice; monospace: false; maximumHeight: 160
                }
            }
        }
        Rectangle {
            id: composer; objectName: "messageComposer"
            Layout.preferredWidth: root.columnWidth; Layout.alignment: Qt.AlignHCenter
            implicitHeight: composerLayout.implicitHeight + Theme.spaceXl
            color: Theme.panel; radius: Theme.radiusLg
            border.width: prompt.activeFocus ? 2 : 1
            border.color: prompt.activeFocus ? Theme.focusRing : composerHover.hovered ? Theme.accentBorder : Theme.controlEdge
            HoverHandler { id: composerHover }
            ColumnLayout {
                id: composerLayout; anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.spaceMd } spacing: Theme.spaceSm
                RowLayout {
                    visible: root.workMode || !!root.conversations.repositoryPath; Layout.fillWidth: true
                    ActionButton {
                        objectName: "chooseConversationRepositoryButton"; flat: true; horizontalPadding: Theme.spaceSm
                        icon.source: Theme.icon("folder")
                        text: root.conversations.repositoryPath ? "项目 · " + root.conversations.repositoryPath.replace(/\\/g, "/").split("/").pop() : "选择项目"
                        Layout.maximumWidth: composer.width - 80
                        enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending
                        ToolTip.visible: hovered; ToolTip.text: root.conversations.repositoryPath || "选择一个本地项目"
                        onClicked: folder.open()
                    }
                    ActionButton { text: "取消关联项目"; display: AbstractButton.IconOnly; icon.source: Theme.icon("dismiss"); flat: true; horizontalPadding: Theme.spaceXs; visible: !!root.conversations.repositoryPath; enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending; onClicked: root.conversations.clearRepository() }
                    Item { Layout.fillWidth: true }
                }
                LongText {
                    objectName: "chatProjectPathDetail"
                    visible: !!root.conversations.repositoryPath
                    Layout.fillWidth: true; title: "完整项目路径"
                    text: root.conversations.repositoryPath; maximumHeight: root.height < 560 ? 72 : 140
                }
                AppScrollView {
                    Layout.fillWidth: true; Layout.preferredHeight: Math.max(64, Math.min(root.height < 560 ? 96 : 132, prompt.contentHeight + 16)); clip: true
                    AppTextArea {
                        id: prompt; objectName: "conversationPrompt"; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText
                        implicitHeight: Theme.controlHeight + Theme.spaceLg
                        color: Theme.text; font.pixelSize: Theme.fontSizeMd + 1; padding: Theme.spaceSm
                        placeholderText: root.workMode ? "描述你想完成的工作…" : "向 ForgePilot 提问…"
                        background: Rectangle { color: Theme.panel; radius: Theme.radiusMd }
                        Keys.onReturnPressed: event => root.enterPressed(event)
                        Keys.onEnterPressed: event => root.enterPressed(event)
                        TextMenu { id: menu; objectName: "conversationPromptMenu"; editor: prompt }
                        TapHandler { acceptedButtons: Qt.RightButton; onTapped: menu.popup() }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: Theme.spaceSm
                    ActionButton { objectName: "convertDraftToAgentTaskButton"; text: "转为 Agent 任务"; icon.source: Theme.icon("flow"); display: root.columnWidth < 540 ? AbstractButton.IconOnly : AbstractButton.TextBesideIcon; flat: true; visible: !root.workMode && !!prompt.text.trim(); enabled: !root.conversations.busy
                        onClicked: root.convertToAgentTask(prompt.text.trim(), "已选择当前输入") }
                    ActionButton { text: "关联项目"; icon.source: Theme.icon("folder"); flat: true; horizontalPadding: Theme.spaceSm; visible: !root.workMode && !root.conversations.repositoryPath; enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending; onClicked: folder.open(); ToolTip.visible: hovered; ToolTip.text: "关联项目后可询问代码" }
                    AppComboBox {
                        objectName: "chatToolMode"; Accessible.name: "问答工具模式"; visible: !root.workMode; Layout.preferredWidth: 94
                        model: ["自动", "快速", "联网", "项目"]
                        currentIndex: ["auto", "fast", "web", "project"].indexOf(root.conversations.toolMode)
                        enabled: !root.conversations.active && !root.conversations.busy && !root.conversations.retryPending
                        onActivated: root.conversations.toolMode = ["auto", "fast", "web", "project"][currentIndex]
                        ToolTip.visible: hovered
                        ToolTip.text: currentIndex === 0 ? "保守判断是否需要工具" : currentIndex === 1 ? "不使用工具" : currentIndex === 2 ? "仅联网工具" : "仅项目只读工具"
                    }
                    Item { Layout.fillWidth: true }
                    ActionButton { objectName: "stopAnswerButton"; text: "停止回答"; danger: true; icon.source: Theme.icon("stop"); visible: root.conversations.activeMode === "chat"; enabled: !root.conversations.busy; onClicked: root.conversations.stopAnswer(); ToolTip.visible: hovered; ToolTip.text: "停止回答" }
                    ActionButton { objectName: "stopTaskButton"; text: "停止工作"; danger: true; icon.source: Theme.icon("stop"); visible: root.conversations.activeMode === "task"; enabled: root.controller.canCancel; onClicked: root.controller.cancelTask() }
                    ActionButton {
                        id: sendButton; objectName: "sendMessageButton"; text: root.workMode ? "开始工作" : "发送"
                        icon.source: Theme.icon("send")
                        Accessible.name: root.workMode ? "开始工作" : "发送消息"
                        highlighted: true
                        visible: !root.conversations.active
                        enabled: !root.conversations.busy && !root.conversations.loading && !root.conversations.active && !root.conversations.retryPending && !!prompt.text.trim() && (!root.workMode || !!root.conversations.repositoryPath)
                        onClicked: root.submit(); ToolTip.visible: hovered; ToolTip.text: root.workMode ? "开始工作" : "发送消息"
                    }
                }
                Label {
                    text: root.workMode ? (root.conversations.repositoryPath ? "修改前会请你确认方案" : "先选择要工作的项目") : root.conversations.repositoryPath ? "仅阅读和分析项目" : ""
                    visible: !!text; color: Theme.muted; font.pixelSize: Theme.fontSizeXs
                    Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                }
            }
        }
        Label { text: "Enter 发送   ·   Shift + Enter 换行"; color: Theme.muted; font.pixelSize: Theme.fontSizeXs; Layout.alignment: Qt.AlignHCenter }
        Item { visible: root.empty; Layout.fillHeight: true; Layout.minimumHeight: 0 }
    }
}
