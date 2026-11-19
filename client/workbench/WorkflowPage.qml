import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "WorkflowTheme.js" as Theme
import "WorkbenchUi.js" as W
import "WorkflowUi.js" as Flow

Item {
    id: root
    objectName: "workflowPage"
    required property var mission
    property var mcp: null
    property var rag: null
    property bool taskCenterExpanded: false
    property string selectedNodeId: ""
    property bool selectionPinned: false
    property bool detailsVisible: false
    property string currentTaskId: ""
    property var stages: []
    property var nodeRecords: []
    property var nodeArtifacts: []
    property var selectedNode: ({})
    readonly property bool hasTask: !!mission.taskId
    readonly property bool demandCollapsed: hasTask && !taskCenterExpanded
    readonly property string indexRepositoryPath: demandCollapsed ? (mission.snapshot.repository_path || "") : taskCenter.draftRepositoryPath
    readonly property bool compactLayout: width < 1100 || (taskCenterExpanded && width < 1420)
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
            selectionPinned = false; selectedNodeId = ""; detailsVisible = false
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
    onSelectedNodeIdChanged: updateDetails()
    function openTaskDraft(request, repository, source) {
        taskCenter.fillDraft(request, repository, source)
        taskCenterExpanded = true
        detailsVisible = false
    }
    Component.onCompleted: refreshView()
    Connections { target: root.mission; function onMissionChanged() { Qt.callLater(root.refreshView) } }
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
    ColumnLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: header.implicitHeight + Theme.spaceLg * 2
            color: Theme.sidebar
            ColumnLayout {
                id: header
                anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Theme.spaceLg
                spacing: Theme.spaceSm
                RowLayout {
                    Layout.fillWidth: true; spacing: Theme.spaceSm
                    Label { text: root.hasTask ? "任务工作流" : "创建代码任务"; color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold }
                    Rectangle {
                        visible: root.hasTask; radius: Theme.radiusMd
                        implicitWidth: statusText.implicitWidth + 22; implicitHeight: 26
                        color: Theme.raised; border.color: W.color(root.mission.status)
                        Label { id: statusText; anchors.centerIn: parent; text: Flow.statusLabels[root.mission.status] || W.label(root.mission.status); color: W.color(root.mission.status); font.pixelSize: Theme.fontSizeXs }
                    }
                    Item { Layout.fillWidth: true }
                    Controls.ActionButton {
                        objectName: "workflowFollowButton"; visible: root.hasTask && root.selectionPinned
                        text: root.width < 700 ? "跟随" : "跟随进度"; flat: true
                        onClicked: root.followProgress()
                    }
                    Controls.ActionButton {
                        objectName: "workflowDemandToggle"; visible: root.hasTask
                        text: root.width < 700 ? "任务" : "新建 / 切换"; flat: true; checked: root.taskCenterExpanded
                        onClicked: { root.taskCenterExpanded = !root.taskCenterExpanded; if (root.taskCenterExpanded && root.compactLayout) root.detailsVisible = false }
                    }
                    Controls.ActionButton { objectName: "workflowMoreButton"; text: "···"; visible: root.hasTask; flat: true; Accessible.name: "任务操作"; onClicked: taskMenu.open()
                        Menu { id: taskMenu; y: parent.height
                            MenuItem { text: "刷新任务"; enabled: !root.mission.busy; onTriggered: root.mission.refresh() }
                            MenuItem { text: "取消任务"; visible: ["succeeded", "failed", "cancelled"].indexOf(root.mission.status) < 0; enabled: root.mission.synchronized && !root.mission.busy; onTriggered: cancelDialog.open() }
                        }
                    }
                }
                Label {
                    visible: root.hasTask; Layout.fillWidth: true
                    text: root.mission.snapshot.request || "正在读取任务…"
                    textFormat: Text.PlainText; color: Theme.secondary
                    font.pixelSize: Theme.fontSizeSm; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                }
                Label {
                    id: toolPreview; objectName: "missionToolContext"; visible: root.hasTask; Layout.fillWidth: true
                    text: root.toolSummary(); textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 6; elide: Text.ElideRight
                    color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                }
                Controls.LongText {
                    objectName: "missionToolContextFull"; visible: root.hasTask && toolPreview.truncated
                    Layout.fillWidth: true; title: "本次检索与 MCP 工具"; text: toolPreview.text
                    monospace: false; maximumHeight: 150
                }
            }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
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
            objectName: "workflowAttention"; visible: root.needsAttention
            Layout.fillWidth: true; implicitHeight: attentionRow.implicitHeight + Theme.spaceSm * 2; color: Theme.accentSubtle
            RowLayout { id: attentionRow; anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.margins: Theme.spaceLg
                Label { Layout.fillWidth: true; text: root.mission.status === "awaiting_human" && root.attentionNodeId === "execution_approval" && !root.mission.snapshot.recovery_required ? "方案已准备好，请查看并确认后继续。" : "任务需要处理，请查看对应步骤。"; color: Theme.warning; wrapMode: Text.WrapAnywhere }
                Controls.ActionButton { objectName: "workflowAttentionButton"; text: "查看"; onClicked: root.selectNode(root.attentionNodeId) }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0; spacing: 0
            TaskCenter {
                id: taskCenter
                mission: root.mission; mcp: root.mcp; rag: root.rag
                visible: !root.demandCollapsed
                Layout.preferredWidth: root.hasTask ? 300 : Math.min(420, root.width * 0.48)
                Layout.fillWidth: !root.hasTask && root.compactLayout
                Layout.fillHeight: true; Layout.minimumWidth: 0
            }
            ColumnLayout {
                visible: (!root.compactLayout || !root.detailsVisible) && (root.hasTask || !root.compactLayout)
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; spacing: 0
                Label {
                    visible: root.hasTask && root.stages.length > 0
                    Layout.fillWidth: true; Layout.margins: Theme.spaceLg
                    text: root.selectionPinned ? "正在查看所选步骤 · 可随时返回跟随进度" : "跟随任务进度 · 点击阶段查看详情，展开可查看内部步骤"
                    color: Theme.muted; font.pixelSize: Theme.fontSizeSm; wrapMode: Text.WrapAnywhere
                }
                WorkflowGraph {
                    id: graph
                    visible: root.hasTask && root.stages.length > 0
                    Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.minimumHeight: 0
                    stages: root.stages; selectedNodeId: root.selectedNodeId; artifactModel: root.mission.artifacts
                    onNodeSelected: function(nodeId) { root.selectNode(nodeId) }
                }
                Item {
                    visible: !root.hasTask || !root.stages.length
                    Layout.fillWidth: true; Layout.fillHeight: true
                    ColumnLayout {
                        anchors.centerIn: parent; width: Math.min(440, parent.width - Theme.spaceXl * 2); spacing: Theme.spaceLg
                        Label { text: root.hasTask ? "正在生成任务流程" : "从一个代码任务开始"; color: Theme.text; font.pixelSize: Theme.fontSizeXl; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.Wrap }
                        Label { text: root.hasTask ? "分析开始后，这里会显示每个阶段的实时状态。" : "选择本地仓库并描述需求。提交后可在流程图中查看分析、实现、验证和交付进度。"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap }
                    }
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
    Dialog {
        id: errorDialog; title: "任务连接信息"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(640, root.width - Theme.spaceLg * 2); height: Math.min(420, root.height - Theme.spaceLg * 2)
        background: Rectangle { color: Theme.panel; radius: Theme.radiusLg; border.color: Theme.border }
        contentItem: Controls.AppScrollView {
            clip: true
            TextArea { id: errorText; text: root.mission.error; color: Theme.text; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; background: Rectangle { color: Theme.input } }
        }
        footer: DialogButtonBox {
            Controls.ActionButton { text: "复制"; onClicked: { errorText.selectAll(); errorText.copy(); errorText.deselect() } }
            Controls.ActionButton { text: "关闭"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
    }
    Dialog {
        id: cancelDialog; title: "取消当前任务"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(420, root.width - Theme.spaceLg * 2)
        padding: Theme.spaceLg
        background: Rectangle { color: Theme.panel; radius: Theme.radiusLg; border.color: Theme.border }
        contentItem: Label { text: "取消后停止继续执行，隔离工作区保留已完成的改动。"; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
        footer: DialogButtonBox {
            Controls.ActionButton { text: "返回"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            Controls.ActionButton { objectName: "workflowCancelConfirm"; text: "确认取消"; danger: true; enabled: root.mission.synchronized && !root.mission.busy; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: root.mission.cancel() }
        }
    }
}
