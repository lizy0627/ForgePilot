import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W
import "WorkflowUi.js" as Flow

Item {
    id: root
    objectName: "workflowPage"
    required property var mission
    property var mcp: null
    property var rag: null
    property bool taskCenterExpanded: false
    property bool historyCollapsed: false
    property bool toolsExpanded: false
    property string selectedNodeId: ""
    property bool selectionPinned: false
    property bool detailsVisible: false
    property string currentTaskId: ""
    property var stages: []
    property var nodeRecords: []
    property var activityEvents: []
    readonly property var activeRecord: Flow.currentRecord(nodeRecords) || ({})
    readonly property int completedStages: stages.filter(function(stage) { return stage.status === "succeeded" }).length
    property var nodeArtifacts: []
    property var selectedNode: ({})
    readonly property bool hasTask: !!mission.taskId
    readonly property bool demandCollapsed: hasTask && !taskCenterExpanded
    readonly property string indexRepositoryPath: demandCollapsed ? (mission.snapshot.repository_path || "") : taskCenter.draftRepositoryPath
    readonly property bool compactHistory: width < 1080
    readonly property bool historyVisible: compactHistory ? historyDrawer.visible : !historyCollapsed
    readonly property bool compactLayout: workspace.width < 960
    readonly property string repositoryPath: mission.snapshot.repository_path || ""
    readonly property bool terminalTask: ["succeeded", "completed", "failed", "cancelled", "skipped"].indexOf(mission.status) >= 0
    function openSkillLibrary() { draftPanel.showLibrary() }
    function toggleHistory() {
        if (compactHistory) {
            if (historyDrawer.visible) historyDrawer.close()
            else historyDrawer.open()
        } else {
            historyCollapsed = !historyCollapsed
        }
    }
    onCompactHistoryChanged: if (!compactHistory && historyDrawer) historyDrawer.close()
    onVisibleChanged: if (!visible) {
        if (historyDrawer) historyDrawer.close()
        if (skillHistory) skillHistory.close()
        if (activityDialog) activityDialog.close()
        if (draftPanel) draftPanel.close()
    }
    readonly property string attentionNodeId: {
        for (var i = 0; i < nodeRecords.length; ++i)
            if (nodeRecords[i].status === "awaiting_human") return nodeRecords[i].nodeId
        return Flow.activeNodeId(mission.graph)
    }
    readonly property bool needsAttention: hasTask && (mission.status === "awaiting_human" || mission.status === "interrupted"
        || mission.status === "failed" || mission.snapshot.recovery_required || nodeRecords.some(function(n) { return ["retryable", "interrupted", "failed"].indexOf(n.status) >= 0 }))
    function fallbackText(reasons) {
        var labels = {embedding_unavailable: "Embedding 未配置或不可用", embedding_failed: "Embedding 查询失败",
            embedding_policy_denied: "任务未授权 Embedding 数据外发", vector_unavailable: "向量检索不可用",
            index_missing: "主仓尚未建立索引", index_stale: "索引已过期，已退回当前源码检索",
            stale_candidates: "索引候选与任务源码不一致", clangd_unavailable: "clangd 不可用",
            compile_database_missing: "缺少归属正确的编译数据库", compilation_database_missing: "缺少归属正确的编译数据库",
            mcp_unavailable: "选定 MCP 服务不可用", mcp_disconnected: "选定 MCP 服务已断开", mcp_timeout: "MCP 调用超时"}
        return reasons.map(function(reason) { return labels[reason] ? labels[reason] + "（" + reason + "）" : reason }).join("、")
    }
    function toolSummary() {
        var context = root.mission.snapshot.tool_context
        if (!context) return "此历史任务未记录工具选择。"
        var ragState = context.rag || {}, clangdState = context.clangd || {}, mcpState = context.mcp || {}
        var parts = ["RAG：" + (!ragState.requested ? "未请求" : ragState.used ? "已使用" : ragState.enabled ? "已请求，尚未使用" : "已请求，当前不可用")]
        if ((ragState.fallback_reasons || []).length) parts.push("RAG 降级：" + fallbackText(ragState.fallback_reasons))
        parts.push("clangd：" + (clangdState.used ? "已使用" : (clangdState.fallback_reasons || []).length ? "文本检索" : clangdState.enabled ? "已启用，尚未使用" : "文本检索"))
        if ((clangdState.fallback_reasons || []).length) parts.push("clangd 降级：" + fallbackText(clangdState.fallback_reasons))
        var servers = mcpState.servers || []
        parts.push("MCP：" + (!servers.length ? "未选择" : (mcpState.used ? "已调用 " : "已选 ") + servers.map(function(server) { return server.name + " [" + (server.tools || []).join("、") + "]" }).join("；")))
        if ((mcpState.fallback_reasons || []).length) parts.push("MCP 降级：" + fallbackText(mcpState.fallback_reasons))
        return parts.join("\n")
    }
    function refreshView() {
        if (currentTaskId !== mission.taskId) {
            currentTaskId = mission.taskId
            activityEvents = []
            activityDialog.close()
            skillHistory.close()
            selectionPinned = false; selectedNodeId = ""; detailsVisible = false
            toolsExpanded = false
            if (hasTask) taskCenterExpanded = false
            graph.expandedStages = ({})
        }
        nodeRecords = Flow.nodes(mission.graph)
        var nextStages = Flow.buildStages(mission.graph, mission.artifacts)
        if (JSON.stringify(stages) !== JSON.stringify(nextStages)) stages = nextStages
        if (!selectionPinned || !nodeRecords.some(function(n) { return n.nodeId === selectedNodeId }))
            selectedNodeId = Flow.activeNodeId(mission.graph)
        updateDetails()
        if (!selectionPinned) Qt.callLater(function() { graph.revealNode(root.selectedNodeId) })
    }
    function updateDetails() {
        selectedNode = nodeRecords.filter(function(n) { return n.nodeId === root.selectedNodeId })[0] || ({})
        var nextArtifacts = Flow.artifactsForNode(mission.artifacts, selectedNode)
        // Keep the reader's command/file selection and scroll position during
        // unrelated node updates instead of rebuilding its detail delegates.
        if (!Flow.sameArtifacts(nodeArtifacts, nextArtifacts)) nodeArtifacts = nextArtifacts
    }
    function selectNode(nodeId) {
        if (!nodeId) return
        selectedNodeId = nodeId; selectionPinned = true; detailsVisible = true
        if (compactLayout) taskCenterExpanded = false
        Qt.callLater(function() { detailView.focusHeader() })
    }
    function closeDetails() {
        detailsVisible = false
        Qt.callLater(function() { graph.focusNode(root.selectedNodeId) })
    }
    function followProgress() {
        selectionPinned = false
        refreshView()
    }
    function skillHistoryText() {
        if (mission.missionSkillsLoading) return "正在读取任务技能记录…"
        if (mission.missionSkillsError) return mission.missionSkillsError
        var history = mission.missionSkills
        if (history.task_id !== mission.taskId) return ""
        if (!history.selected_skills.length) return "创建此任务时未选择技能。"
        return history.selected_skills.map(function(skill) {
            var records = history.injections.filter(function(entry) {
                return entry.skill_id === skill.skill_id && entry.version === skill.version
                    && entry.content_sha256 === skill.content_sha256
            })
            var lines = [skill.skill_id + " · v" + skill.version, "SHA-256  " + skill.content_sha256]
            if (!records.length) lines.push("尚无提供给模型的记录。")
            else records.forEach(function(entry) {
                lines.push("已提供给模型 · " + Flow.nodeTitle(entry) + " · 第 " + entry.attempt + " 次尝试")
            })
            return lines.join("\n")
        }).join("\n\n")
    }
    onSelectedNodeIdChanged: updateDetails()
    function openTaskDraft(request, repository, source) {
        taskCenter.fillDraft(request, repository, source)
        taskCenterExpanded = true
        detailsVisible = false
        historyDrawer.close()
    }
    Component.onCompleted: refreshView()
    Connections {
        target: root.mission
        function onMissionChanged() { Qt.callLater(root.refreshView) }
        function onEventReceived(id, event) {
            if (event.task_id !== root.mission.taskId) return
            root.activityEvents = W.appendActivity(root.activityEvents, event)
        }
    }
    Connections {
        target: root.mission.approval
        function onScopeExpansionApproved(taskId) {
            if (taskId === root.mission.taskId) root.followProgress()
        }
    }
    Connections {
        target: root.mission.graph
        function onSnapshotChanged() { Qt.callLater(root.refreshView) }
        function onDataChanged() { Qt.callLater(root.refreshView) }
        function onCountChanged() { Qt.callLater(root.refreshView) }
    }
    Connections {
        target: root.mission.artifacts
        function onDataChanged() { Qt.callLater(root.refreshView) }
        function onCountChanged() { Qt.callLater(root.refreshView) }
    }
    Rectangle { anchors.fill: parent; color: Theme.background }
    Drawer {
        id: historyDrawer
        edge: Qt.LeftEdge
        width: 268
        y: root.Window.window ? root.Window.window.height - root.height : 0
        height: root.height
        padding: 0
        interactive: root.visible && root.compactHistory
        background: Rectangle { color: Theme.sidebar }
    }
    Rectangle {
        id: historyPanel
        objectName: "workflowHistorySidebar"
        parent: root.compactHistory ? historyDrawer.contentItem : root
        anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.left: parent.left
        width: root.compactHistory ? historyDrawer.width : 244
        visible: root.historyVisible
        color: Theme.sidebar
        MissionHistory {
            anchors.fill: parent
            anchors.margins: Theme.spaceMd
            mission: root.mission
            navMode: true
            onTaskOpened: {
                historyDrawer.close()
                if (!root.mission.error) root.taskCenterExpanded = false
            }
        }
        Rectangle {
            anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom
            width: 1; color: Theme.border
        }
    }
    ColumnLayout {
        id: workspace
        anchors.fill: parent
        anchors.leftMargin: root.historyVisible && !root.compactHistory ? historyPanel.width : 0
        spacing: 0
        RowLayout {
            visible: root.hasTask
            Layout.fillWidth: true
            Layout.leftMargin: Theme.spaceXl; Layout.rightMargin: Theme.spaceLg
            Layout.topMargin: Theme.spaceMd; Layout.bottomMargin: Theme.spaceMd
            spacing: Theme.spaceSm
            ColumnLayout {
                Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 3
                visible: root.demandCollapsed
                RowLayout {
                    Layout.fillWidth: true; spacing: Theme.spaceSm
                    Label {
                        text: root.repositoryPath.replace(/\\/g, "/").split("/").filter(Boolean).pop() || "仓库"
                        textFormat: Text.PlainText; elide: Text.ElideMiddle
                        Layout.maximumWidth: Math.max(100, workspace.width * 0.32)
                        color: Theme.muted; font.pixelSize: Theme.fontSizeSm
                        ToolTip.visible: repoHover.hovered; ToolTip.text: root.repositoryPath
                        HoverHandler { id: repoHover }
                    }
                    Label { text: "·"; color: Theme.muted }
                    Label {
                        text: (root.mission.status === "awaiting_human" ? "等待你处理" : Flow.statusLabels[root.mission.status] || W.label(root.mission.status))
                            + (root.activeRecord.nodeId && ["running", "awaiting_human"].indexOf(root.mission.status) >= 0 ? " · " + Flow.nodeTitle(root.activeRecord) : "")
                        Layout.fillWidth: true; Layout.minimumWidth: 0; elide: Text.ElideRight
                        color: W.color(root.mission.status); font.pixelSize: Theme.fontSizeSm
                    }
                }
                Label {
                    text: root.mission.snapshot.request || "正在读取任务…"
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                    textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                    color: Theme.text; font.pixelSize: Theme.fontSizeXl; font.weight: Font.DemiBold
                    ToolTip.visible: truncated && titleHover.hovered; ToolTip.text: text
                    HoverHandler { id: titleHover }
                }
            }
            Item { visible: !root.demandCollapsed; Layout.fillWidth: true }
            Controls.ActionButton {
                objectName: "workflowFollowButton"; visible: root.demandCollapsed && root.selectionPinned
                text: "跟随进度"; flat: true; onClicked: root.followProgress()
            }
            Controls.ActionButton {
                objectName: "workflowToolContextToggle"; visible: root.demandCollapsed
                text: "工具"; flat: true; checked: root.toolsExpanded
                Accessible.name: "任务工具详情"
                onClicked: root.toolsExpanded = !root.toolsExpanded
            }
            Controls.ActionButton {
                objectName: "workflowSkillHistoryButton"; visible: root.demandCollapsed
                text: "技能"; flat: true; horizontalPadding: Theme.spaceSm
                Accessible.name: "查看已选择的技能"
                onClicked: skillHistory.open()
            }
            Controls.ActionButton {
                objectName: "workflowDemandToggle"
                text: root.demandCollapsed ? "新建任务" : "返回当前任务"
                icon.source: Theme.icon(root.demandCollapsed ? "add" : "flow")
                onClicked: {
                    root.detailsVisible = false
                    root.taskCenterExpanded = !root.taskCenterExpanded
                }
            }
            Controls.ActionButton {
                objectName: "workflowMoreButton"; visible: root.demandCollapsed
                text: "任务操作"; icon.source: Theme.icon("more"); display: AbstractButton.IconOnly; flat: true; Layout.preferredWidth: Theme.controlHeight
                horizontalPadding: 0; Accessible.name: "任务操作"
                onClicked: taskMenu.open()
                Menu {
                    id: taskMenu
                    y: parent.height
                    padding: Theme.spaceXs
                    background: Rectangle { radius: Theme.radiusMd; color: Theme.raised; border.color: Theme.controlEdge }
                    Controls.AppMenuItem { text: "刷新任务"; enabled: !root.mission.busy; onTriggered: root.mission.refresh() }
                    Controls.AppMenuItem {
                        text: "取消任务"
                        visible: ["succeeded", "failed", "cancelled"].indexOf(root.mission.status) < 0
                        enabled: visible && root.mission.synchronized && !root.mission.busy
                        onTriggered: cancelDialog.open()
                    }
                }
            }
        }
        Label {
            id: toolPreview; objectName: "missionToolContext"
            visible: root.demandCollapsed && root.toolsExpanded
            Layout.fillWidth: true; Layout.leftMargin: Theme.spaceXl; Layout.rightMargin: Theme.spaceXl
            Layout.bottomMargin: Theme.spaceMd
            text: root.toolSummary(); textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
            maximumLineCount: 5; elide: Text.ElideRight; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
        }
        Controls.LongText {
            objectName: "missionToolContextFull"; visible: toolPreview.visible && toolPreview.truncated
            Layout.fillWidth: true; Layout.leftMargin: Theme.spaceXl; Layout.rightMargin: Theme.spaceXl
            title: "工具详情全文"; text: toolPreview.text; monospace: false; maximumHeight: 120
        }
        Rectangle { visible: root.demandCollapsed; Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        RowLayout {
            visible: root.demandCollapsed && root.terminalTask
            Layout.fillWidth: true
            Layout.leftMargin: Theme.spaceXl; Layout.rightMargin: Theme.spaceLg
            Layout.topMargin: Theme.spaceSm; Layout.bottomMargin: Theme.spaceSm
            Label {
                Layout.fillWidth: true
                text: "任务结果 · " + (Flow.statusLabels[root.mission.status] || W.label(root.mission.status))
                color: Theme.text; font.weight: Font.DemiBold
            }
            Controls.ActionButton {
                objectName: "workflowExtractSkillButton"; text: "提炼为技能"
                onClicked: draftPanel.showSource()
            }
        }
        Rectangle {
            visible: !!root.mission.error || (root.hasTask && !root.mission.synchronized)
            Layout.fillWidth: true; implicitHeight: connectionRow.implicitHeight + Theme.spaceSm * 2; color: Theme.dangerFill
            RowLayout { id: connectionRow; anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.margins: Theme.spaceLg
                Label { Layout.fillWidth: true; text: root.mission.error || "正在同步任务，完成后可继续操作。"; color: Theme.warning; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; maximumLineCount: 3; elide: Text.ElideRight }
                Controls.ActionButton { text: "详情"; flat: true; visible: root.mission.error.length > 160; onClicked: errorDialog.open() }
                Controls.ActionButton { text: "刷新"; flat: true; enabled: !root.mission.busy; onClicked: root.mission.refresh() }
            }
        }
        Rectangle {
            objectName: "workflowAttention"; visible: root.demandCollapsed && root.needsAttention
            Layout.fillWidth: true; implicitHeight: attentionRow.implicitHeight + Theme.spaceSm * 2; color: Theme.accentSubtle
            RowLayout { id: attentionRow; anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.margins: Theme.spaceLg
                Label { Layout.fillWidth: true; text: root.mission.status === "awaiting_human" && root.attentionNodeId === "execution_approval" && !root.mission.snapshot.recovery_required ? "方案已准备好，请查看并确认后继续。" : "任务需要处理，请查看对应步骤。"; color: Theme.warning; wrapMode: Text.WrapAnywhere }
                Controls.ActionButton { objectName: "workflowAttentionButton"; text: root.mission.status === "awaiting_human" ? "查看待确认事项" : "查看待处理步骤"; highlighted: true; onClicked: root.selectNode(root.attentionNodeId) }
            }
        }
        TaskCenter {
            id: taskCenter
            visible: !root.demandCollapsed
            composerMode: true
            mission: root.mission; mcp: root.mcp; rag: root.rag
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.minimumWidth: 0; Layout.minimumHeight: 0
        }
        RowLayout {
            visible: root.demandCollapsed
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.minimumWidth: 0; Layout.minimumHeight: 0; spacing: 0
            Item {
                visible: !root.compactLayout || !root.detailsVisible
                Layout.fillWidth: true; Layout.fillHeight: true
                Layout.minimumWidth: 0; Layout.minimumHeight: 0
                RowLayout {
                    id: graphHeading
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    anchors.leftMargin: Theme.spaceXl; anchors.rightMargin: Theme.spaceLg
                    height: Theme.controlHeight + Theme.spaceMd
                    Label {
                        text: "任务流程"
                        font.weight: Font.DemiBold; font.pixelSize: Theme.fontSizeLg
                        color: Theme.text
                    }
                    Label {
                        Layout.fillWidth: true; Layout.minimumWidth: 0
                        text: root.stages.length ? root.completedStages + " / " + root.stages.length + " 阶段完成" : ""
                        color: Theme.muted; elide: Text.ElideRight
                        font.pixelSize: Theme.fontSizeSm
                    }
                    Controls.ActionButton {
                        text: "活动"; icon.source: Theme.icon("history"); flat: true
                        Accessible.name: "查看活动时间线"
                        onClicked: activityDialog.open()
                    }
                }
                WorkflowGraph {
                    id: graph
                    visible: root.stages.length > 0
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: graphHeading.bottom; anchors.bottom: parent.bottom
                    stages: root.stages; selectedNodeId: root.selectedNodeId; artifactModel: root.mission.artifacts
                    onNodeSelected: function(nodeId) { root.selectNode(nodeId) }
                }
                Label {
                    visible: !root.stages.length
                    anchors.centerIn: parent
                    text: "正在读取任务流程…"; color: Theme.muted
                }
            }
            Rectangle {
                visible: root.detailsVisible && root.hasTask
                Layout.preferredWidth: root.compactLayout ? -1 : 440
                Layout.fillWidth: root.compactLayout; Layout.fillHeight: true; Layout.minimumWidth: 0
                color: Theme.sidebar; border.color: Theme.border
                WorkflowDetails {
                    id: detailView
                    objectName: "workflowDetails"
                    anchors.fill: parent
                    mission: root.mission; node: root.selectedNode; artifacts: root.nodeArtifacts; compact: root.compactLayout
                    onCloseRequested: root.closeDetails()
                    onCancelRequested: cancelDialog.open()
                }
            }
        }
    }
    Shortcut {
        sequence: "Escape"
        enabled: root.visible && root.hasTask && (root.detailsVisible || root.taskCenterExpanded)
        onActivated: { if (root.detailsVisible) root.closeDetails(); else root.taskCenterExpanded = false }
    }
    SkillDraftPanel {
        id: draftPanel; mission: root.mission
        width: Math.min(740, root.width - Theme.spaceLg * 2)
        height: Math.min(800, root.height - Theme.spaceLg * 2)
    }
    Controls.AppDialog {
        id: activityDialog
        title: "任务活动"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(540, root.width - Theme.spaceXl * 2)
        height: Math.min(560, root.height - Theme.spaceXl * 2)
        contentItem: Controls.AppScrollView {
            id: activityScroll
            contentWidth: availableWidth
            clip: true
            Loader {
                width: activityScroll.availableWidth
                active: activityDialog.visible
                sourceComponent: ActivityTimeline {
                    mission: root.mission
                    events: root.activityEvents
                }
            }
        }
        footer: Controls.AppDialogButtonBox {
            Controls.ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
    }
    Controls.AppDialog {
        id: skillHistory; objectName: "missionSkillHistoryDialog"
        title: "已选择的技能"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(620, root.width - 2 * Theme.spaceLg)
        height: Math.min(360, root.height - 2 * Theme.spaceLg)
        onAboutToShow: root.mission.loadMissionSkills()
        contentItem: Controls.AppScrollView {
            clip: true
            Controls.AppTextArea {
                objectName: "missionSkillHistoryText"; readOnly: true; selectByMouse: true
                textFormat: TextEdit.PlainText; wrapMode: TextEdit.WrapAnywhere
                text: root.skillHistoryText()
            }
        }
        footer: Controls.AppDialogButtonBox {
            Controls.ActionButton {
                text: "刷新"; flat: true; enabled: !root.mission.missionSkillsLoading
                onClicked: root.mission.loadMissionSkills()
            }
            Controls.ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
    }
    Controls.AppDialog {
        id: errorDialog; title: "任务连接信息"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(640, root.width - Theme.spaceLg * 2); height: Math.min(420, root.height - Theme.spaceLg * 2)
        contentItem: Controls.AppScrollView {
            clip: true
            Controls.AppTextArea { id: errorText; text: root.mission.error; color: Theme.text; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText }
        }
        footer: Controls.AppDialogButtonBox {
            Controls.ActionButton { text: "复制"; onClicked: { errorText.selectAll(); errorText.copy(); errorText.deselect() } }
            Controls.ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
    }
    Controls.AppDialog {
        id: cancelDialog; title: "取消当前任务"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(420, root.width - Theme.spaceLg * 2)
        padding: Theme.spaceLg
        contentItem: Label { text: "取消后停止继续执行，隔离工作区保留已完成的改动。"; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
        footer: Controls.AppDialogButtonBox {
            Controls.ActionButton { text: "返回"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            Controls.ActionButton { objectName: "workflowCancelConfirm"; text: "确认取消"; danger: true; enabled: root.mission.synchronized && !root.mission.busy; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: root.mission.cancel() }
        }
    }
}
