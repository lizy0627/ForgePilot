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
    property string conversionRequest: ""
    property string conversionSource: ""
    property string conversionRepository: ""
    property bool detailsExpanded: width >= 1120
    property var activityEvents: []
    onWidthChanged: { if (width < 1060) detailsExpanded = false }
    onConversionRequestChanged: { if (conversionRequest) { detailsExpanded = true; detailTabs.currentIndex = 0 } }
    Connections {
        target: root.mission
        function onMissionChanged() {
            if (!root.mission.taskId || root.mission.taskId !== root.currentTaskId) {
                root.activityEvents = [];
                root.currentTaskId = root.mission.taskId;
            }
            if (root.mission.status === "awaiting_human") { detailTabs.currentIndex = 3; root.detailsExpanded = true }
        }
        function onEventReceived(id, event) {
            if (event.task_id !== root.mission.taskId) return;
            var next = root.activityEvents.slice(-99);
            next.push({event_type:event.event_type, node_id:event.node_id, timestamp:event.timestamp, state:event.state});
            root.activityEvents = next;
        }
    }
    property string currentTaskId: ""
    RowLayout {
        anchors.fill: parent; spacing: 0
        TaskCenter { mission: root.mission; seedRequest: root.conversionRequest; selectedContext: root.conversionSource; seedRepository: root.conversionRepository; Layout.preferredWidth: 244; Layout.fillHeight: true }
        ScrollView {
            id: centerScroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            ScrollBar.vertical: Controls.AppScrollBar {}
            ColumnLayout {
                width: centerScroll.availableWidth; spacing: 12
                anchors.margins: 14
                MissionHeader { mission: root.mission; Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14; Layout.topMargin: 14 }
                RowLayout { Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14
                    Label { text: "工作台"; font.pixelSize: 17; font.weight: Font.DemiBold; Layout.fillWidth: true }
                    Controls.ActionButton { objectName: "workbenchDetailsToggle"; text: root.detailsExpanded ? "收起详情" : "展开详情"; onClicked: root.detailsExpanded = !root.detailsExpanded }
                }
                Rectangle { visible: root.mission.connectionState === "reconnecting" || root.mission.connectionState === "recovering" || root.mission.connectionState === "error"; Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14; implicitHeight: 40; radius: 6; color: Theme.dangerFill
                    Label { anchors.fill: parent; anchors.margins: 8; text: "事件连接已中断；正在重新同步任务，审批与交付暂不可用。"; color: Theme.warning; wrapMode: Text.Wrap; verticalAlignment: Text.AlignVCenter }
                }
                TaskGraphView { mission: root.mission; Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14 }
                ActivityTimeline { mission: root.mission; events: root.activityEvents; Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14 }
                Controls.Panel {
                    title: "恢复任务"; visible: !!root.mission.taskId && (root.mission.status === "interrupted" || root.mission.snapshot.recovery_required || !!root.mission.recovery.token)
                    Layout.fillWidth: true; Layout.leftMargin: 14; Layout.rightMargin: 14
                    Label { text: "任务已中断。先生成恢复预览，核对检查和动作后再确认。"; Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.warning }
                    Controls.ActionButton { objectName: "missionRecoveryButton"; text: "检查恢复方案"; enabled: root.mission.synchronized && !root.mission.busy; onClicked: root.mission.prepareRecovery() }
                    Label { visible: !!root.mission.recovery.revision; text: "修订 " + (root.mission.recovery.revision || "") + " · 检查：" + W.lines(root.mission.recovery.checks) + "\n阻塞原因：" + (root.mission.recovery.blocked_reason || "无")
                        + "\n动作：" + (root.mission.recovery.actions || []).map(function(a) { return a.action || a.kind || a.type || "检查" }).join("、"); Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
                    Controls.ActionButton { text: "确认恢复"; visible: !!root.mission.recovery.token; enabled: root.mission.synchronized && !root.mission.busy && !root.mission.recovery.blocked_reason; onClicked: root.mission.confirmRecovery() }
                }
                Item { Layout.preferredHeight: 12 }
            }
        }
        Rectangle {
            id: details; objectName: "workbenchDetails"; visible: root.detailsExpanded
            Layout.preferredWidth: Math.min(420, Math.max(340, root.width * 0.34)); Layout.fillHeight: true
            color: Theme.sidebar
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 10; spacing: 8
                Label { text: "详情与操作"; font.pixelSize: 16; font.weight: Font.DemiBold }
                ScrollView { Layout.fillWidth: true; Layout.preferredHeight: 42; contentWidth: detailTabs.implicitWidth; ScrollBar.horizontal: Controls.AppScrollBar {}
                    TabBar { id: detailTabs; objectName: "workbenchDetailTabs"; width: implicitWidth
                        Controls.NavigationTab { text: "任务" }
                        Controls.NavigationTab { text: "证据" }
                        Controls.NavigationTab { text: "计划" }
                        Controls.NavigationTab { text: "审批" }
                        Controls.NavigationTab { text: "Diff" }
                        Controls.NavigationTab { text: "验证" }
                        Controls.NavigationTab { text: "审查" }
                        Controls.NavigationTab { text: "交付" }
                    }
                }
                ScrollView { id: detailScroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; ScrollBar.vertical: Controls.AppScrollBar {}
                    Loader {
                        width: detailScroll.availableWidth
                        sourceComponent: [specComponent, evidenceComponent, planComponent, approvalComponent, diffComponent, validationComponent, reviewComponent, deliveryComponent][detailTabs.currentIndex]
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
