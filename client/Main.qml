import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import "panels"
import "panels/Ui.js" as Ui
import "panels/Theme.js" as Theme
import "workbench" as Workbench

ApplicationWindow {
    id: window
    required property var backendConnection
    required property var controller
    required property var conversations
    property var mcp: null
    property var rag: null
    property var missions: null
    property var runtimeConfig: null
    property bool showRuntimeConfiguration: false
    property bool configurationPrompted: false
    property bool developmentTest: false
    property bool detailsExpanded: false
    property bool sidebarExpanded: true
    property bool agentMode: false
    function openTaskDraft(request, repository, source) {
        if (!workflowLoader.item) return
        workflowLoader.item.openTaskDraft(request, repository, source)
        agentMode = true
        conversations.mode = "chat"
    }
    property string previousConversationMode: "chat"
    property bool previousHistoryReadOnly: false
    readonly property bool workMode: conversations.mode === "task"
    readonly property bool compactChat: width < 1180
    readonly property bool compactWorkDetails: detailsExpanded && width < 1240
    readonly property bool sidebarShown: !compactChat && sidebarExpanded && (!detailsExpanded || width >= 1280)
    objectName: "forgePilotWindow"
    width: Math.max(860, Math.min(1320, Screen.desktopAvailableWidth - Theme.spaceLg * 2))
    height: Math.max(640, Math.min(900, Screen.desktopAvailableHeight - Theme.spaceLg * 2))
    minimumWidth: 860; minimumHeight: 640
    visible: true; title: "ForgePilot"; color: Theme.background
    onClosing: function(close) { if (window.mcp && !window.mcp.canCloseEditor) close.accepted = false }
    font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeMd
    palette: DarkPalette {}
    Component.onCompleted: {
        conversations.refreshHistory()
        if (runtimeConfig) runtimeConfig.load()
        if (showRuntimeConfiguration) { configurationPrompted = true; runtimeConfigDialog.open() }
    }
    Connections {
        target: window.runtimeConfig
        function onChanged() {
            if (window.runtimeConfig.wizardRequired && !window.configurationPrompted) {
                window.configurationPrompted = true
                runtimeConfigDialog.open()
            }
        }
    }
    RuntimeConfigDialog { id: runtimeConfigDialog; controller: window.runtimeConfig }
    onCompactChatChanged: if (!compactChat) compactSidebarPopup.close()
    onAgentModeChanged: if (agentMode) compactSidebarPopup.close()
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
        function onCodeTaskRequested(prompt, repositoryPath) {
            window.openTaskDraft(prompt, repositoryPath, "来自旧工作入口；仅使用当前输入，请确认任务基线。")
        }
        function onSelectionChanged() { window.detailsExpanded = false; compactSidebarPopup.close() }
        function onChanged() {
            if (window.conversations.mode !== window.previousConversationMode) {
                window.previousConversationMode = window.conversations.mode
                if (window.conversations.mode === "chat") window.detailsExpanded = false
            }
        }
    }
    component IconAction: ActionButton {
        display: AbstractButton.IconOnly
        flat: true
        horizontalPadding: 0
        Layout.preferredWidth: 36
        Layout.preferredHeight: 36
        Accessible.name: text
        ToolTip.visible: hovered
        ToolTip.text: text
    }
    component Brand: RowLayout {
        spacing: Theme.spaceSm
        Image {
            source: Theme.brandMark
            Layout.preferredWidth: 30
            Layout.preferredHeight: 30
            sourceSize: Qt.size(60, 60)
        }
        Label {
            text: "ForgePilot"; color: Theme.text
            font.pixelSize: 20; font.weight: Font.DemiBold
        }
    }
    component ServiceStatus: RowLayout {
        id: service
        readonly property string status: window.conversations.serverStatus
        spacing: Theme.spaceSm
        Rectangle {
            implicitWidth: 7; implicitHeight: 7; radius: 4
            color: service.status === "connected" ? Theme.success : service.status === "checking" ? Theme.muted : Theme.warning
        }
        Label {
            text: service.status === "connected" ? "本机服务已连接" : service.status === "checking" ? "正在连接本机服务…" : "本机服务未连接"
            color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
        }
    }
    Rectangle {
        id: modeBar; objectName: "mainModeBar"
        visible: !!window.missions
        anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
        height: 60; color: Theme.panel; z: 2
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: Theme.spaceLg; anchors.rightMargin: Theme.spaceLg
            spacing: Theme.spaceXs
            RowLayout {
                Layout.preferredWidth: window.compactChat ? 166 : 220
                Layout.maximumWidth: Layout.preferredWidth
                Brand {}
                Item { Layout.fillWidth: true }
                IconAction {
                    id: historyToggle; objectName: "workflowHistoryToggle"
                    visible: window.agentMode && !!workflowLoader.item && typeof workflowLoader.item.toggleHistory === "function"
                    text: visible && workflowLoader.item.historyVisible ? "收起任务历史" : "展开任务历史"
                    icon.source: Theme.icon("sidebar")
                    onClicked: workflowLoader.item.toggleHistory()
                }
            }
            ActionButton {
                objectName: "freeChatModeButton"; text: "自由聊天"
                icon.source: Theme.icon("chat"); flat: true; checked: !window.agentMode
                Accessible.name: "自由聊天模式"; Accessible.checkable: true; Accessible.checked: checked
                onClicked: { window.agentMode = false; window.conversations.mode = "chat" }
            }
            ActionButton {
                objectName: "agentWorkbenchModeButton"; text: "Agent 工作台"
                icon.source: Theme.icon("flow"); flat: true; checked: window.agentMode
                Accessible.name: "Agent 工作台模式"; Accessible.checkable: true; Accessible.checked: checked
                onClicked: { window.agentMode = true; window.conversations.mode = "chat" }
            }
            Item { Layout.fillWidth: true }
            ServiceStatus { visible: !window.agentMode }
            ActionButton {
                objectName: "workbenchSkillLibraryButton"; text: "本机技能库"
                icon.source: Theme.icon("library"); flat: true
                visible: window.agentMode && !!workflowLoader.item
                onClicked: workflowLoader.item.openSkillLibrary()
            }
            ActionButton {
                objectName: "workbenchSettingsButton"; text: "设置"
                icon.source: Theme.icon("settings"); flat: true; visible: window.agentMode
                onClicked: settingsDialog.open()
            }
        }
    }
    RowLayout {
        visible: !window.agentMode
        anchors.top: modeBar.visible ? modeBar.bottom : parent.top; anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right; spacing: 0
        Rectangle {
            id: sidebar; objectName: "conversationSidebar"
            visible: window.sidebarShown
            Layout.preferredWidth: window.sidebarShown ? 232 : 0; Layout.minimumWidth: 0
            Layout.fillHeight: true; color: Theme.sidebar
            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: Theme.border }
            ColumnLayout {
                anchors.fill: parent; anchors.margins: Theme.spaceMd; spacing: Theme.spaceMd
                Brand { visible: !modeBar.visible; Layout.bottomMargin: Theme.spaceSm }
                ActionButton {
                    objectName: "newTaskButton"; text: "新对话"; icon.source: Theme.icon("add"); highlighted: true
                    Layout.fillWidth: true; Layout.preferredHeight: 42
                    enabled: !conversations.busy
                    onClicked: conversations.newConversation()
                }
                Loader {
                    Layout.fillWidth: true; Layout.fillHeight: true
                    active: !window.compactChat
                    sourceComponent: ConversationHistoryPanel { conversations: window.conversations }
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                RowLayout {
                    Layout.fillWidth: true; spacing: Theme.spaceXs
                    ActionButton { objectName: "settingsButton"; text: "设置"; icon.source: Theme.icon("settings"); flat: true; Layout.fillWidth: true; onClicked: settingsDialog.open() }
                    ActionButton { objectName: "memoryManagerButton"; text: "记忆"; icon.source: Theme.icon("brain"); flat: true; Layout.fillWidth: true; onClicked: memoryDialog.open() }
                }
                ServiceStatus { visible: !modeBar.visible; Layout.leftMargin: Theme.spaceSm }
            }
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.minimumWidth: 0; Layout.minimumHeight: 0; spacing: 0
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 58; color: Theme.background
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: Theme.spaceLg; anchors.rightMargin: Theme.spaceLg; spacing: Theme.spaceMd
                    IconAction {
                        objectName: "sidebarToggle"; icon.source: Theme.icon("sidebar")
                        text: window.compactChat ? "历史与设置" : window.sidebarShown ? "收起侧栏" : "展开侧栏"
                        onClicked: {
                            if (window.compactChat) {
                                if (compactSidebarPopup.opened) compactSidebarPopup.close()
                                else compactSidebarPopup.open()
                            } else {
                                var show = !window.sidebarShown
                                if (window.detailsExpanded && window.width < 1280) window.detailsExpanded = false
                                window.sidebarExpanded = show
                            }
                        }
                    }
                    Label {
                        id: conversationTitlePreview
                        text: conversations.title || "新对话"
                        textFormat: Text.PlainText; font.pixelSize: Theme.fontSizeLg; color: Theme.text
                        font.weight: Font.DemiBold; Layout.fillWidth: true; Layout.minimumWidth: 0
                        wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                    }
                    ActionButton {
                        objectName: "conversationTitleDetailsButton"
                        text: "标题全文"; icon.source: Theme.icon("document"); flat: true
                        visible: !!conversations.conversationId
                        onClicked: conversationTitleDialog.open()
                    }
                    ActionButton { objectName: "compactHistoryButton"; text: "历史"; flat: true; visible: window.compactChat; Layout.preferredWidth: 64; horizontalPadding: Theme.spaceSm; onClicked: compactSidebarPopup.open() }
                    ActionButton { objectName: "compactSettingsButton"; text: "设置"; flat: true; visible: window.compactChat; Layout.preferredWidth: 64; horizontalPadding: Theme.spaceSm; onClicked: settingsDialog.open() }
                    ActionButton { objectName: "compactMemoryButton"; text: "记忆"; flat: true; visible: window.compactChat; Layout.preferredWidth: 64; horizontalPadding: Theme.spaceSm; onClicked: memoryDialog.open() }
                    IconAction { objectName: "compactNewConversationButton"; text: "新对话"; icon.source: Theme.icon("add"); visible: !window.sidebarShown; enabled: !conversations.busy; onClicked: conversations.newConversation() }
                    ActionButton { objectName: "detailsToggle"; text: window.detailsExpanded ? "收起详情" : "工作详情"; flat: true; visible: window.workMode || conversations.activeMode === "task"; onClicked: window.detailsExpanded = !window.detailsExpanded }
                }
            }
            Rectangle {
                visible: !window.missions
                Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 184; Layout.preferredHeight: 44; Layout.bottomMargin: 10
                color: Theme.raised; radius: Theme.radiusLg
                Row {
                    anchors.centerIn: parent; spacing: Theme.spaceXs
                    Repeater {
                        model: [{mode: "chat", label: "问答"}, {mode: "task", label: "工作"}]
                        delegate: ActionButton {
                            id: modeButton; required property var modelData
                            objectName: modelData.mode === "chat" ? "chatModeButton" : "taskModeButton"
                            width: 84; height: 36; hoverEnabled: true
                            text: modelData.label; checked: window.conversations.mode === modelData.mode
                            enabled: !window.conversations.busy && !window.conversations.retryPending
                            Accessible.name: modelData.label + "模式"; Accessible.checkable: true; Accessible.checked: checked
                            onClicked: window.conversations.mode = modelData.mode
                            flat: true
                        }
                    }
                }
            }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: false; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg; spacing: Theme.spaceSm
                Label { visible: developmentTest; text: "开发测试 · 离线模型"; color: Theme.warning; font.pixelSize: Theme.fontSizeSm }
                RowLayout {
                    visible: !!window.conversations.error; Layout.fillWidth: true
                    Label { text: window.conversations.error; color: Theme.danger; textFormat: Text.PlainText; Layout.fillWidth: true; Layout.minimumWidth: 0; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight }
                    ActionButton { objectName: "conversationErrorDetailsButton"; text: "查看详情"; flat: true; onClicked: conversationErrorDialog.open() }
                    IconAction { text: "关闭错误提示"; icon.source: Theme.icon("dismiss"); onClicked: window.conversations.clearError() }
                }
                RowLayout {
                    visible: !!controller.error; Layout.fillWidth: true
                    Label { objectName: "errorMessage"; text: controller.error; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: Theme.danger; Layout.fillWidth: true; Layout.minimumWidth: 0; maximumLineCount: 2; elide: Text.ElideRight }
                    ActionButton { objectName: "errorDetailsButton"; text: "查看详情"; flat: true; onClicked: errorDialog.open() }
                    IconAction { text: "关闭错误提示"; icon.source: Theme.icon("dismiss"); onClicked: controller.clearError() }
                }
                RowLayout {
                    visible: !!controller.connectionWarning || controller.connectionState === "reconnecting"; Layout.fillWidth: true
                    Label { objectName: "connectionWarning"; text: "任务连接中断，可重连后继续查看。"; color: Theme.warning; Layout.fillWidth: true; Layout.minimumWidth: 0; wrapMode: Text.Wrap }
                    ActionButton { text: "重连"; onClicked: controller.reconnectEvents() }
                    ActionButton { text: "刷新状态"; onClicked: controller.refreshTask() }
                }
            }
            RowLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                Layout.minimumWidth: 0; Layout.minimumHeight: 0; spacing: 0
                ConversationPanel {
                    conversations: window.conversations; controller: window.controller
                    visible: !window.compactWorkDetails
                    Layout.fillWidth: true; Layout.fillHeight: true
                    Layout.minimumWidth: 0; Layout.minimumHeight: 0
                    onOpenWorkDetails: { window.conversations.showActiveTask(); window.detailsExpanded = true }
                    onConvertToAgentTask: function(selectedText, sourceLabel) {
                        window.openTaskDraft(selectedText, "", sourceLabel + "；仅发送所选文本，需另行指定仓库。")
                    }
                }
                Rectangle {
                    objectName: "workDetailsPanel"; visible: window.detailsExpanded
                    Layout.preferredWidth: window.compactWorkDetails ? -1 : Math.min(460, window.width * 0.43)
                    Layout.fillWidth: window.compactWorkDetails; Layout.fillHeight: true; Layout.minimumWidth: 0
                    color: Theme.sidebar; border.color: Theme.border
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: Theme.spaceMd; spacing: Theme.spaceSm
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: controller.canApprove ? "请确认本次工作方案" : "工作详情"; font.weight: Font.DemiBold; font.pixelSize: Theme.fontSizeLg; Layout.fillWidth: true }
                            IconAction { text: "关闭工作详情"; icon.source: Theme.icon("dismiss"); onClicked: window.detailsExpanded = false }
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
                                ColumnLayout { width: projectScroll.availableWidth; spacing: Theme.spaceMd; RepositoryPanel { controller: window.controller; Layout.fillWidth: true } TaskHistoryPanel { title: "工作记录"; controller: window.controller; Layout.fillWidth: true } }
                            }
                        }
                    }
                }
            }
        }
    }
    Popup {
        id: compactSidebarPopup; objectName: "compactSidebarPopup"
        parent: Overlay.overlay
        x: 0; y: modeBar.visible ? modeBar.height : 0
        width: Math.min(300, window.width - Theme.spaceLg * 2)
        height: window.height - y
        modal: true; focus: true; padding: Theme.spaceMd
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.sidebar; border.color: Theme.border }
        contentItem: ColumnLayout {
            spacing: Theme.spaceMd
            RowLayout {
                Layout.fillWidth: true
                Label { text: "历史与设置"; color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold; Layout.fillWidth: true }
                IconAction { text: "关闭历史与设置"; icon.source: Theme.icon("dismiss"); onClicked: compactSidebarPopup.close() }
            }
            ActionButton {
                objectName: "compactNewTaskButton"; text: "新对话"; icon.source: Theme.icon("add"); highlighted: true
                Layout.fillWidth: true; enabled: !window.conversations.busy
                onClicked: { compactSidebarPopup.close(); window.conversations.newConversation() }
            }
            Loader {
                Layout.fillWidth: true; Layout.fillHeight: true
                active: window.compactChat
                sourceComponent: ConversationHistoryPanel { conversations: window.conversations }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
            RowLayout {
                Layout.fillWidth: true; spacing: Theme.spaceXs
                ActionButton { text: "设置"; icon.source: Theme.icon("settings"); flat: true; Layout.fillWidth: true; onClicked: { compactSidebarPopup.close(); settingsDialog.open() } }
                ActionButton { text: "记忆"; icon.source: Theme.icon("brain"); flat: true; Layout.fillWidth: true; onClicked: { compactSidebarPopup.close(); memoryDialog.open() } }
            }
            ServiceStatus { visible: !modeBar.visible }
        }
    }
    Loader {
        id: workflowLoader
        active: !!window.missions
        visible: window.agentMode
        anchors.top: modeBar.bottom; anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
        sourceComponent: Component {
            Workbench.WorkflowPage {
                mission: window.missions
                mcp: window.mcp; rag: window.rag
            }
        }
    }
    AppDialog {
        id: settingsDialog; objectName: "settingsDialog"; title: "设置"; modal: true; anchors.centerIn: parent
        closePolicy: !window.mcp || window.mcp.canCloseEditor ? Popup.CloseOnEscape | Popup.CloseOnPressOutside : Popup.NoAutoClose
        width: Math.min(800, window.width - 48); height: Math.min(640, window.height - 48)
        onAboutToShow: {
            if (window.rag) {
                const target = window.agentMode ? workflowLoader.item.indexRepositoryPath : window.conversations.repositoryPath
                if (target === window.rag.repositoryPath) window.rag.refresh()
                else window.rag.repositoryPath = target
            }
        }
        onOpened: if (window.mcp) window.mcp.open()
        onClosed: { if (window.mcp) window.mcp.close() }
        contentItem: SettingsPanel {
            id: settings; objectName: "settingsPanel"
            backendConnection: window.backendConnection
            serverStatus: window.conversations.serverStatus
            mcp: window.mcp; rag: window.rag; runtimeConfig: window.runtimeConfig
            onConfigureRuntimeRequested: { settingsDialog.close(); runtimeConfigDialog.open() }
        }
        footer: AppDialogButtonBox { ActionButton { objectName: "closeSettingsButton"; text: "完成"; highlighted: true; enabled: !window.mcp || window.mcp.canCloseEditor; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole } }
    }
    Loader { active: !!window.mcp; sourceComponent: Component { McpServerDialog { mcp: window.mcp } } }
    AppDialog {
        id: conversationTitleDialog; objectName: "conversationTitleDialog"; title: "对话标题全文"
        modal: true; anchors.centerIn: parent
        width: Math.min(window.width - 80, 800); height: Math.min(window.height - 100, 360)
        contentItem: AppScrollView {
            clip: true
            AppTextArea {
                id: conversationTitleFullText; objectName: "conversationTitleFullText"
                text: window.conversations.title; textFormat: TextEdit.PlainText
                readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere
            }
        }
        footer: RowLayout {
            ActionButton { objectName: "conversationTitleCopyButton"; text: "复制全文"; icon.source: Theme.icon("copy"); onClicked: { conversationTitleFullText.selectAll(); conversationTitleFullText.copy(); conversationTitleFullText.deselect() } }
            Item { Layout.fillWidth: true }
            ActionButton { text: "关闭"; onClicked: conversationTitleDialog.close() }
        }
    }
    AppDialog {
        id: errorDialog; objectName: "errorDialog"; title: "错误详情"; modal: true; anchors.centerIn: parent
        width: Math.min(window.width - 80, 800); height: Math.min(window.height - 100, 500)
        contentItem: AppScrollView { clip: true; AppTextArea { id: taskErrorFullText; objectName: "taskErrorFullText"; text: controller.error; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere } }
        footer: RowLayout {
            ActionButton { objectName: "taskErrorCopyButton"; text: "复制全文"; icon.source: Theme.icon("copy"); onClicked: { taskErrorFullText.selectAll(); taskErrorFullText.copy(); taskErrorFullText.deselect() } }
            Item { Layout.fillWidth: true }
            ActionButton { text: "关闭"; onClicked: errorDialog.close() }
        }
    }
    AppDialog {
        id: conversationErrorDialog; objectName: "conversationErrorDialog"; title: "对话错误详情"; modal: true; anchors.centerIn: parent
        width: Math.min(window.width - 80, 800); height: Math.min(window.height - 100, 500)
        contentItem: AppScrollView { clip: true; AppTextArea { id: conversationErrorFullText; objectName: "conversationErrorContent"; text: window.conversations.error; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere } }
        footer: RowLayout {
            ActionButton { objectName: "conversationErrorCopyButton"; text: "复制全文"; icon.source: Theme.icon("copy"); onClicked: { conversationErrorFullText.selectAll(); conversationErrorFullText.copy(); conversationErrorFullText.deselect() } }
            Item { Layout.fillWidth: true }
            ActionButton { text: "关闭"; onClicked: conversationErrorDialog.close() }
        }
    }
    MemoryDialog { id: memoryDialog; conversations: window.conversations }
}
