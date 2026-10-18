import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "panels"
import "panels/Ui.js" as Ui
import "panels/Theme.js" as Theme

ApplicationWindow {
    id: window
    required property var controller
    required property var conversations
    property var mcp: null
    property var rag: null
    property bool developmentTest: false
    property bool detailsExpanded: false
    property bool sidebarExpanded: true
    property string previousConversationMode: "chat"
    property bool previousHistoryReadOnly: false
    readonly property bool workMode: conversations.mode === "task"
    readonly property bool sidebarShown: sidebarExpanded && (!detailsExpanded || width >= 1280)
    objectName: "forgePilotWindow"
    width: 1320; height: 900; minimumWidth: 860; minimumHeight: 640
    visible: true; title: "ForgePilot"; color: Theme.background
    onClosing: function(close) { if (window.mcp && !window.mcp.canCloseEditor) close.accepted = false }
    font.family: "Microsoft YaHei UI"; font.pixelSize: 13
    palette: DarkPalette {}
    Component.onCompleted: conversations.refreshHistory()
    Connections {
        target: controller
        function onTaskFinished(state) { controller.refreshTasks() }
        function onApprovalRequired() { detailTabs.currentIndex = 0; window.detailsExpanded = true }
        function onTaskChanged() {
            if (!controller.taskId) window.detailsExpanded = false
            else if (controller.historyReadOnly && !window.previousHistoryReadOnly) window.detailsExpanded = true
            window.previousHistoryReadOnly = controller.historyReadOnly
        }
    }
    Connections {
        target: window.conversations
        function onSelectionChanged() { window.detailsExpanded = false }
        function onChanged() {
            if (window.conversations.mode !== window.previousConversationMode) {
                window.previousConversationMode = window.conversations.mode
                if (window.conversations.mode === "chat") window.detailsExpanded = false
            }
        }
    }
    RowLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            id: sidebar; objectName: "conversationSidebar"
            visible: window.sidebarShown; Layout.preferredWidth: 224; Layout.fillHeight: true; color: Theme.sidebar
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 14; spacing: 12
                Label { text: "ForgePilot"; font.pixelSize: 19; font.weight: Font.DemiBold; Layout.topMargin: 7; Layout.leftMargin: 10; Layout.bottomMargin: 12 }
                ActionButton {
                    objectName: "newTaskButton"; text: "＋  新对话"; flat: true
                    Layout.fillWidth: true; Layout.preferredHeight: 42
                    enabled: !conversations.busy
                    onClicked: conversations.newConversation()
                }
                ConversationHistoryPanel {
                    conversations: window.conversations; Layout.fillWidth: true; Layout.fillHeight: true
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                RowLayout {
                    Layout.fillWidth: true; spacing: 4
                    ActionButton { objectName: "settingsButton"; text: "设置"; flat: true; Layout.fillWidth: true; onClicked: settingsDialog.open() }
                    ActionButton { objectName: "memoryManagerButton"; text: "记忆"; flat: true; Layout.fillWidth: true; onClicked: memoryDialog.open() }
                }
                Label { text: conversations.serverStatus === "connected" ? "●  已连接本机服务" : "○  本机服务未连接"; color: Theme.muted; font.pixelSize: 11; Layout.leftMargin: 10; Layout.bottomMargin: 2 }
            }
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: 72
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 22; spacing: 12
                    ActionButton {
                        objectName: "sidebarToggle"; text: "☰"; flat: true; horizontalPadding: 0; Layout.preferredWidth: 36; font.pixelSize: 18
                        Accessible.name: "切换侧边栏"; ToolTip.visible: hovered; ToolTip.text: "切换侧边栏"
                        onClicked: { var show = !window.sidebarShown; if (window.detailsExpanded && window.width < 1280) window.detailsExpanded = false; window.sidebarExpanded = show }
                    }
                    Label { text: conversations.conversationId ? conversations.title : "ForgePilot"; textFormat: Text.PlainText; font.pixelSize: 15; color: Theme.secondary; Layout.fillWidth: true; elide: Text.ElideRight }
                    ActionButton { objectName: "compactNewConversationButton"; text: "＋"; flat: true; visible: !window.sidebarShown; Accessible.name: "新对话"; Layout.preferredWidth: 36; horizontalPadding: 0; onClicked: conversations.newConversation() }
                    ActionButton { objectName: "detailsToggle"; text: window.detailsExpanded ? "收起详情" : "工作详情"; flat: true; visible: window.workMode || conversations.activeMode === "task"; onClicked: window.detailsExpanded = !window.detailsExpanded }
                }
            }
            Rectangle {
                Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 184; Layout.preferredHeight: 44; Layout.bottomMargin: 10
                color: Theme.raised; radius: 22
                Row {
                    anchors.centerIn: parent; spacing: 4
                    Repeater {
                        model: [{mode: "chat", label: "问答"}, {mode: "task", label: "工作"}]
                        delegate: Button {
                            id: modeButton; required property var modelData
                            objectName: modelData.mode === "chat" ? "chatModeButton" : "taskModeButton"
                            width: 84; height: 36; hoverEnabled: true
                            text: modelData.label; checked: window.conversations.mode === modelData.mode
                            enabled: !window.conversations.busy && !window.conversations.retryPending
                            Accessible.name: modelData.label + "模式"; Accessible.checkable: true; Accessible.checked: checked
                            onClicked: window.conversations.mode = modelData.mode
                            contentItem: Label { text: modeButton.text; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; color: modeButton.checked ? Theme.text : Theme.secondary; font.weight: modeButton.checked ? Font.DemiBold : Font.Normal }
                            background: Rectangle { radius: 18; color: modeButton.checked ? Theme.panel : modeButton.hovered ? Theme.hover : "transparent"; border.color: modeButton.visualFocus ? Theme.accent : "transparent" }
                        }
                    }
                }
            }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: false; Layout.leftMargin: 24; Layout.rightMargin: 24; spacing: 6
                Label { visible: developmentTest; text: "开发测试 · 离线模型"; color: Theme.warning; font.pixelSize: 11 }
                RowLayout {
                    visible: !!window.conversations.error; Layout.fillWidth: true
                    Label { text: window.conversations.error; color: Theme.danger; textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight }
                    ActionButton { objectName: "conversationErrorDetailsButton"; text: "查看详情"; flat: true; onClicked: conversationErrorDialog.open() }
                    ActionButton { text: "×"; flat: true; horizontalPadding: 8; onClicked: window.conversations.clearError() }
                }
                RowLayout {
                    visible: !!controller.error; Layout.fillWidth: true
                    Label { objectName: "errorMessage"; text: controller.error; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: Theme.danger; Layout.fillWidth: true; maximumLineCount: 2; elide: Text.ElideRight }
                    ActionButton { objectName: "errorDetailsButton"; text: "查看详情"; flat: true; onClicked: errorDialog.open() }
                    ActionButton { text: "×"; flat: true; horizontalPadding: 8; onClicked: controller.clearError() }
                }
                RowLayout {
                    visible: !!controller.connectionWarning || controller.connectionState === "reconnecting"; Layout.fillWidth: true
                    Label { objectName: "connectionWarning"; text: "任务连接中断，可重连后继续查看。"; color: Theme.warning; Layout.fillWidth: true; wrapMode: Text.Wrap }
                    ActionButton { text: "重连"; onClicked: controller.reconnectEvents() }
                    ActionButton { text: "刷新状态"; onClicked: controller.refreshTask() }
                }
            }
            RowLayout {
                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
                ConversationPanel {
                    conversations: window.conversations; controller: window.controller
                    Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 320
                    onOpenWorkDetails: { window.conversations.showActiveTask(); window.detailsExpanded = true }
                }
                Rectangle {
                    objectName: "workDetailsPanel"; visible: window.detailsExpanded
                    Layout.preferredWidth: Math.min(460, window.width * 0.43); Layout.fillHeight: true; color: Theme.sidebar
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 12; spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: controller.canApprove ? "请确认本次工作方案" : "工作详情"; font.weight: Font.DemiBold; font.pixelSize: 16; Layout.fillWidth: true }
                            ActionButton { text: "×"; flat: true; horizontalPadding: 6; Accessible.name: "关闭工作详情"; onClicked: window.detailsExpanded = false }
                        }
                        RowLayout {
                            visible: controller.historyReadOnly; Layout.fillWidth: true
                            Label { text: "历史工作记录"; color: Theme.secondary; Layout.fillWidth: true }
                            ActionButton { objectName: "executionViewButton"; text: "返回执行"; visible: !!controller.taskId && !Ui.terminal(controller.state) && !controller.snapshot.read_only; onClicked: controller.openExecutionView() }
                        }
                        TabBar {
                            id: detailTabs; objectName: "detailTabs"; Layout.fillWidth: true
                            NavigationTab { text: "方案" }
                            NavigationTab { text: "过程" }
                            NavigationTab { text: "结果" }
                            NavigationTab { text: "项目" }
                        }
                        StackLayout {
                            currentIndex: detailTabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
                            PlanPanel { controller: window.controller }
                            AppScrollView { id: traceScroll; objectName: "traceScroll"; clip: true; contentWidth: availableWidth; TracePanel { width: traceScroll.availableWidth; controller: window.controller } }
                            ColumnLayout {
                                TabBar {
                                    id: resultTabs; objectName: "resultTabs"; Layout.fillWidth: true
                                    NavigationTab { text: "文件差异" }
                                    NavigationTab { text: "构建" }
                                    NavigationTab { text: "测试" }
                                }
                                StackLayout {
                                    currentIndex: resultTabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
                                    DiffPanel { controller: window.controller }
                                    BuildPanel { controller: window.controller }
                                    BuildPanel { controller: window.controller; testsOnly: true }
                                }
                            }
                            AppScrollView {
                                id: projectScroll; clip: true; contentWidth: availableWidth
                                ColumnLayout { width: projectScroll.availableWidth; spacing: 12; RepositoryPanel { controller: window.controller; Layout.fillWidth: true } TaskHistoryPanel { title: "工作记录"; controller: window.controller; Layout.fillWidth: true } }
                            }
                        }
                    }
                }
            }
        }
    }
    Dialog {
        id: settingsDialog; objectName: "settingsDialog"; title: "设置"; modal: true; anchors.centerIn: parent
        closePolicy: !window.mcp || window.mcp.canCloseEditor ? Popup.CloseOnEscape | Popup.CloseOnPressOutside : Popup.NoAutoClose
        width: Math.min(800, window.width - 48); height: Math.min(640, window.height - 48)
        onOpened: { if (window.mcp) window.mcp.open(); if (window.rag) window.rag.refresh() }
        onClosed: { if (window.mcp) window.mcp.close() }
        contentItem: SettingsPanel { id: settings; objectName: "settingsPanel"; controller: window.controller; mcp: window.mcp; rag: window.rag }
        footer: DialogButtonBox { ActionButton { objectName: "closeSettingsButton"; text: "完成"; enabled: !window.mcp || window.mcp.canCloseEditor; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole } }
    }
    Loader { active: !!window.mcp; sourceComponent: Component { McpServerDialog { mcp: window.mcp } } }
    Dialog {
        id: errorDialog; objectName: "errorDialog"; title: "错误详情"; modal: true; anchors.centerIn: parent
        width: Math.min(window.width - 80, 800); height: Math.min(window.height - 100, 500)
        contentItem: AppScrollView { clip: true; TextArea { text: controller.error; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere } }
        footer: DialogButtonBox { ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole } }
    }
    Dialog {
        id: conversationErrorDialog; objectName: "conversationErrorDialog"; title: "对话错误详情"; modal: true; anchors.centerIn: parent
        width: Math.min(window.width - 80, 800); height: Math.min(window.height - 100, 500)
        contentItem: AppScrollView { clip: true; TextArea { objectName: "conversationErrorContent"; text: window.conversations.error; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere } }
        footer: DialogButtonBox { ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole } }
    }
    MemoryDialog { id: memoryDialog; conversations: window.conversations }
}
