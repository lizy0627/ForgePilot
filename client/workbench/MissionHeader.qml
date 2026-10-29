import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    title: mission.snapshot.request || "选择或创建 Agent 任务"
    subtitle: mission.taskId ? "修订 " + mission.revision : ""
    objectName: "missionHeader"
    RowLayout {
        Layout.fillWidth: true
        NodeStatusBadge { status: root.mission.status || "pending" }
        Label { text: root.mission.snapshot.task_type || ""; color: Theme.secondary }
        Label { text: root.mission.graph.currentAgents.length ? "当前 Agent：" + root.mission.graph.currentAgents.join("、") : "当前 Agent：无"; color: Theme.secondary; Layout.fillWidth: true; elide: Text.ElideRight }
    }
    Label { Layout.fillWidth: true; text: root.mission.snapshot.repository_path || ""; color: Theme.muted; elide: Text.ElideMiddle; textFormat: Text.PlainText }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "连接：" + (root.mission.connectionState || "未连接"); color: root.mission.synchronized ? Theme.secondary : Theme.warning; Layout.fillWidth: true }
        Controls.ActionButton { objectName: "missionRefreshButton"; text: "刷新"; enabled: !!root.mission.taskId && !root.mission.busy; onClicked: root.mission.refresh() }
        Controls.ActionButton { text: "取消任务"; danger: true; visible: !!root.mission.taskId && ["succeeded","failed","cancelled"].indexOf(root.mission.status) < 0; enabled: root.mission.synchronized && !root.mission.busy; onClicked: cancelDialog.open() }
    }
    StateNotice { mission: root.mission; hasData: !!root.mission.snapshot.request; emptyText: "选择左侧任务或创建新任务。"; Layout.fillWidth: true }
    Dialog {
        id: cancelDialog; title: "取消当前任务"; modal: true; anchors.centerIn: Overlay.overlay; width: 420; implicitWidth: 420
        standardButtons: Dialog.Cancel
        contentItem: Label { text: "将取消任务 " + root.mission.taskId + "。执行中的操作可能已经修改隔离工作区。"; wrapMode: Text.Wrap }
        footer: DialogButtonBox {
            Controls.ActionButton { text: "返回"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            Controls.ActionButton { text: "确认取消"; danger: true; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: root.mission.cancel() }
        }
    }
}
