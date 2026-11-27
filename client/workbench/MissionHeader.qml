import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    readonly property string agentSummary: mission.graph.currentAgents.length ? "当前 Agent：" + mission.graph.currentAgents.join("、") : "当前 Agent：无"
    readonly property string repositoryPath: mission.snapshot.repository_path || ""
    readonly property string connectionSummary: "连接：" + (mission.connectionState || "未连接")
    title: mission.snapshot.request || "选择或创建 Agent 任务"
    subtitle: mission.taskId ? "修订 " + mission.revision : ""
    objectName: "missionHeader"
    NodeStatusBadge { id: missionStatusBadge; status: root.mission.status || "pending" }
    Controls.LongText {
        objectName: "missionStatusFullText"; visible: missionStatusBadge.statusText.length > 32
        Layout.fillWidth: true; title: "任务状态"; text: missionStatusBadge.statusText; monospace: false
    }
    Label { text: root.mission.snapshot.task_type || ""; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Label {
        id: agentsPreview
        text: root.agentSummary; color: Theme.secondary; Layout.fillWidth: true
        wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
        maximumLineCount: 2; elide: Text.ElideRight
    }
    Controls.LongText {
        objectName: "missionAgentsFullText"; visible: agentsPreview.truncated || root.agentSummary.length > 80
        Layout.fillWidth: true; title: "当前 Agent"; text: root.agentSummary; monospace: false
    }
    Label {
        id: repositoryPreview
        visible: !!text; Layout.fillWidth: true
        text: root.repositoryPath; color: Theme.muted; wrapMode: Text.WrapAnywhere
        maximumLineCount: 2; elide: Text.ElideMiddle; textFormat: Text.PlainText
    }
    Controls.LongText {
        objectName: "missionRepositoryPathFullText"; visible: repositoryPreview.truncated || root.repositoryPath.length > 80
        Layout.fillWidth: true; title: "仓库路径"; text: root.repositoryPath
    }
    RowLayout {
        Layout.fillWidth: true
        Label {
            id: connectionPreview
            text: root.connectionSummary; color: root.mission.synchronized ? Theme.secondary : Theme.warning
            Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
            maximumLineCount: 2; elide: Text.ElideRight
        }
        Controls.ActionButton { objectName: "missionRefreshButton"; text: "刷新"; enabled: !!root.mission.taskId && !root.mission.busy; onClicked: root.mission.refresh() }
        Controls.ActionButton { text: "取消任务"; danger: true; visible: !!root.mission.taskId && ["succeeded","failed","cancelled"].indexOf(root.mission.status) < 0; enabled: root.mission.synchronized && !root.mission.busy; onClicked: cancelDialog.open() }
    }
    Controls.LongText {
        objectName: "missionConnectionFullText"; visible: connectionPreview.truncated || root.connectionSummary.length > 80
        Layout.fillWidth: true; title: "连接状态"; text: root.connectionSummary; monospace: false
    }
    StateNotice { mission: root.mission; hasData: !!root.mission.snapshot.request; emptyText: "选择左侧任务或创建新任务。"; Layout.fillWidth: true }
    Controls.AppDialog {
        id: cancelDialog; title: "取消当前任务"; modal: true; anchors.centerIn: Overlay.overlay; width: 420; implicitWidth: 420
        padding: Theme.spaceLg
        contentItem: Label { text: "将取消任务 " + root.mission.taskId + "。执行中的操作可能已经修改隔离工作区。"; wrapMode: Text.Wrap }
        footer: Controls.AppDialogButtonBox {
            Controls.ActionButton { text: "返回"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            Controls.ActionButton { text: "确认取消"; danger: true; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: root.mission.cancel() }
        }
    }
}
