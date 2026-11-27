import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Item {
    id: root
    objectName: "agentWorkbenchPage"
    required property var mission
    property var mcp: null
    property var rag: null
    readonly property string indexRepositoryPath: mission.taskId ? (mission.snapshot.repository_path || "") : taskCenter.draftRepositoryPath
    property bool detailsExpanded: width >= 1300
    readonly property bool compactDetails: detailsExpanded && width < 1300
    readonly property bool compactSidebar: width < 1080
    property bool taskCenterExpanded: false
    property var activityEvents: []
    onCompactSidebarChanged: if (compactSidebar) taskCenterExpanded = false
    property bool draftLayoutPending: false
    onWidthChanged: if (draftLayoutPending) Qt.callLater(showDraft)
    function openTaskDraft(request, repository, source) {
        taskCenter.fillDraft(request, repository, source)
        // A native resize can reach the window before its full-width Loader.
        draftLayoutPending = Window.window && width !== Window.window.width
        if (!draftLayoutPending) showDraft()
    }
    function showDraft() {
        draftLayoutPending = false
        if (compactSidebar) taskCenterExpanded = true
        detailsExpanded = width >= 1300
        detailTabs.currentIndex = 0
    }
    Connections {
        target: root.mission
        function onMissionChanged() {
            var changedTask = !root.mission.taskId || root.mission.taskId !== root.currentTaskId
            if (changedTask) {
                root.activityEvents = [];
                root.currentTaskId = root.mission.taskId;
            }
            if (root.mission.status === "awaiting_human") { detailTabs.currentIndex = 3; root.detailsExpanded = true }
            else if (changedTask && root.width < 1300) root.detailsExpanded = false
        }
        function onEventReceived(id, event) {
            if (event.task_id !== root.mission.taskId) return;
            root.activityEvents = W.appendActivity(root.activityEvents, event)
        }
    }
    property string currentTaskId: ""
    Rectangle { anchors.fill: parent; color: Theme.background }
    ColumnLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            objectName: "workbenchCompactBar"
            visible: root.compactSidebar
            Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight + Theme.spaceSm * 2
            color: Theme.panel; border.color: Theme.border
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: Theme.spaceMd; anchors.rightMargin: Theme.spaceMd
                Controls.ActionButton {
                    objectName: "workbenchTaskCenterToggle"
                    text: root.taskCenterExpanded ? "收起任务中心" : "任务中心"
                    checked: root.taskCenterExpanded; flat: true
                    Accessible.name: text
                    onClicked: root.taskCenterExpanded = !root.taskCenterExpanded
                }
                Item { Layout.fillWidth: true }
                Label {
                    text: root.compactDetails ? "详情与操作" : "任务图"
                    color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.minimumWidth: 0; Layout.minimumHeight: 0; spacing: 0
        TaskCenter {
            id: taskCenter
            mission: root.mission
            mcp: root.mcp; rag: root.rag
            visible: !root.compactSidebar || root.taskCenterExpanded
            Layout.preferredWidth: visible ? 244 : 0
            Layout.minimumWidth: 0; Layout.fillHeight: true
        }
        ScrollView {
            id: centerScroll; objectName: "workbenchCenterScroll"; visible: !root.compactDetails
            Layout.fillWidth: true; Layout.fillHeight: true
            Layout.minimumWidth: 0; Layout.minimumHeight: 0; clip: true
            ScrollBar.vertical: Controls.AppScrollBar {}
            ColumnLayout {
                width: centerScroll.availableWidth; spacing: Theme.spaceMd
                anchors.margins: Theme.spaceLg
                MissionHeader { mission: root.mission; Layout.fillWidth: true; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg; Layout.topMargin: Theme.spaceLg }
                RowLayout { Layout.fillWidth: true; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg
                    Label { text: "任务进度"; color: Theme.text; font.pixelSize: Theme.fontSizeXl; font.weight: Font.DemiBold; Layout.fillWidth: true }
                    Controls.ActionButton { objectName: "workbenchDetailsToggle"; text: root.detailsExpanded ? "收起详情" : "展开详情"; onClicked: root.detailsExpanded = !root.detailsExpanded }
                }
                Rectangle { visible: root.mission.connectionState === "reconnecting" || root.mission.connectionState === "recovering" || root.mission.connectionState === "error"; Layout.fillWidth: true; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg; implicitHeight: Math.max(40, connectionNotice.implicitHeight + Theme.spaceSm * 2); radius: Theme.radiusMd; color: Theme.dangerFill; border.color: Theme.warning
                    Label { id: connectionNotice; anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.leftMargin: Theme.spaceSm; anchors.rightMargin: Theme.spaceSm; text: "事件连接已中断；正在重新同步任务，审批与交付暂不可用。"; color: Theme.warning; wrapMode: Text.WrapAnywhere; verticalAlignment: Text.AlignVCenter }
                }
                TaskGraphView { mission: root.mission; Layout.fillWidth: true; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg }
                ActivityTimeline { mission: root.mission; events: root.activityEvents; Layout.fillWidth: true; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg }
                Controls.Panel {
                    title: "恢复任务"; visible: !!root.mission.taskId && (root.mission.graph.hasResumeAction || root.mission.snapshot.recovery_required || !!root.mission.recovery.token)
                    Layout.fillWidth: true; Layout.leftMargin: Theme.spaceLg; Layout.rightMargin: Theme.spaceLg
                    Label { text: "先生成恢复预览，核对检查和动作后再确认。"; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: Theme.warning }
                    Controls.ActionButton { objectName: "missionRecoveryButton"; text: "检查恢复方案"; enabled: root.mission.synchronized && !root.mission.busy; onClicked: root.mission.prepareRecovery() }
                    Label { visible: !!root.mission.recovery.revision; text: "修订 " + (root.mission.recovery.revision || "") + " · 检查：" + W.lines(root.mission.recovery.checks) + "\n阻塞原因：" + (root.mission.recovery.blocked_reason || "无")
                        + "\n动作：" + (root.mission.recovery.actions || []).map(function(a) { return a.action || a.kind || a.type || "检查" }).join("、"); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
                    Controls.ActionButton { text: "确认恢复"; visible: !!root.mission.recovery.token; enabled: root.mission.synchronized && !root.mission.busy && !root.mission.recovery.blocked_reason; onClicked: root.mission.confirmRecovery() }
                }
                Item { Layout.preferredHeight: Theme.spaceMd }
            }
        }
        Rectangle {
            id: details; objectName: "workbenchDetails"; visible: root.detailsExpanded
            Layout.preferredWidth: root.compactDetails ? -1 : Math.min(420, Math.max(340, root.width * 0.34))
            Layout.fillWidth: root.compactDetails; Layout.fillHeight: true
            Layout.minimumWidth: 0; Layout.minimumHeight: 0
            color: Theme.sidebar; border.width: 0
            ColumnLayout {
                anchors.fill: parent; anchors.margins: Theme.spaceMd; spacing: Theme.spaceSm
                RowLayout { Layout.fillWidth: true
                    Label { text: "详情与操作"; color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold; Layout.fillWidth: true }
                    Controls.ActionButton { objectName: "workbenchReturnButton"; text: "返回工作台"; flat: true; visible: root.compactDetails; onClicked: root.detailsExpanded = false }
                }
                Flickable {
                    id: tabStripScroll
                    objectName: "workbenchTabStripScroll"
                    Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight + Theme.spaceMd
                    clip: true; contentWidth: detailTabs.width; contentHeight: detailTabs.height
                    flickableDirection: Flickable.HorizontalFlick
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.horizontal: Controls.AppScrollBar {}
                    function showCurrentTab() {
                        var tab = detailTabs.itemAt(detailTabs.currentIndex)
                        if (!tab || tabStripScroll.width <= 0) return
                        if (tab.x < tabStripScroll.contentX) tabStripScroll.contentX = tab.x
                        else if (tab.x + tab.width > tabStripScroll.contentX + tabStripScroll.width)
                            tabStripScroll.contentX = Math.min(detailTabs.width - tabStripScroll.width, tab.x + tab.width - tabStripScroll.width)
                    }
                    TabBar {
                        id: detailTabs; objectName: "workbenchDetailTabs"
                        readonly property int tabWidth: Theme.controlHeight * 2
                        spacing: Theme.spaceXs
                        width: count * tabWidth + Math.max(0, count - 1) * spacing
                        onCurrentIndexChanged: Qt.callLater(function() { tabStripScroll.showCurrentTab() })
                        background: Item {}
                        Controls.NavigationTab { text: "任务"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "证据"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "计划"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "审批"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "差异"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "验证"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "审查"; width: detailTabs.tabWidth }
                        Controls.NavigationTab { text: "交付"; width: detailTabs.tabWidth }
                    }
                }
                ScrollView { id: detailScroll; objectName: "workbenchDetailScroll"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; ScrollBar.vertical: Controls.AppScrollBar {}
                    Loader {
                        width: detailScroll.availableWidth
                        sourceComponent: [specComponent, evidenceComponent, planComponent, approvalComponent, diffComponent, validationComponent, reviewComponent, deliveryComponent][detailTabs.currentIndex]
                    }
                }
            }
        }
        }
    }
    Component { id: specComponent; TaskSpecPanel { mission: root.mission } }
    Component { id: evidenceComponent; EvidencePanel { mission: root.mission } }
    Component { id: planComponent; ExecutionPlanPanel { mission: root.mission } }
    Component { id: approvalComponent; ApprovalPanel { mission: root.mission } }
    Component { id: diffComponent; MissionDiffPanel { mission: root.mission } }
    Component { id: validationComponent; ValidationPanel { mission: root.mission } }
    Component { id: reviewComponent; ReviewPanel { mission: root.mission } }
    Component { id: deliveryComponent; DeliveryPanel { mission: root.mission } }
}
