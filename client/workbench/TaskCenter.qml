import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Rectangle {
    id: root
    required property var mission
    property string seedRequest: ""
    property string selectedContext: ""
    color: Theme.sidebar
    implicitWidth: 244
    onSeedRequestChanged: { if (seedRequest) requestInput.text = seedRequest }
    FolderDialog { id: folder; title: "选择任务仓库"; options: FolderDialog.DontUseNativeDialog; onAccepted: repositoryInput.text = root.mission.localPath(selectedFolder) }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 12; spacing: 10
        Label { text: "任务中心"; font.pixelSize: 17; font.weight: Font.DemiBold }
        Label { text: root.selectedContext || "创建任务仅使用下方文本与仓库路径。"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11 }
        TextArea { id: requestInput; objectName: "missionRequestInput"; Layout.fillWidth: true; Layout.preferredHeight: 94; wrapMode: TextEdit.Wrap; placeholderText: "描述 Agent 任务"; textFormat: TextEdit.PlainText }
        TextField { id: repositoryInput; objectName: "missionRepositoryInput"; Layout.fillWidth: true; placeholderText: "本地仓库绝对路径"; text: "" }
        Controls.ActionButton { text: "选择仓库…"; Layout.fillWidth: true; onClicked: folder.open() }
        ComboBox { id: kind; Layout.fillWidth: true; model: ["自动判断", "修复缺陷", "实现功能"]; Accessible.name: "任务类型" }
        Controls.ActionButton {
            objectName: "missionCreateButton"; text: root.mission.busy ? "创建中…" : "创建 Agent 任务"; highlighted: true; Layout.fillWidth: true
            enabled: !root.mission.busy && !!requestInput.text.trim() && !!repositoryInput.text.trim()
            onClicked: root.mission.createMission({request: requestInput.text.trim(), repository_path: repositoryInput.text.trim(), task_type: ["auto","bug_fix","feature"][kind.currentIndex]})
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "打开任务 ID"; color: Theme.secondary }
        RowLayout { Layout.fillWidth: true
            TextField { id: taskIdInput; objectName: "missionIdInput"; Layout.fillWidth: true; placeholderText: "UUID" }
            Controls.ActionButton { text: "打开"; enabled: !!taskIdInput.text.trim(); onClicked: root.mission.selectMission(taskIdInput.text.trim()) }
        }
        Label { text: "本次会话的任务"; font.weight: Font.DemiBold; Layout.topMargin: 8 }
        StateNotice { mission: root.mission; hasData: root.mission.missions.count > 0; emptyText: "暂无任务。"; Layout.fillWidth: true }
        ListView {
            id: taskList; objectName: "missionTaskList"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            model: root.mission.missions; spacing: 5
            delegate: Controls.ActionButton {
                required property var record
                property string taskId: record.taskId
                objectName: "missionTaskRow"
                width: taskList.width; height: 62; checked: record.taskId === root.mission.taskId
                Accessible.name: "选择任务：" + (record.request || record.taskId) + "，" + W.label(record.status)
                onClicked: root.mission.selectMission(record.taskId)
                contentItem: Column {
                    spacing: 3
                    Label { width: parent.width; text: record.request || record.taskId; elide: Text.ElideRight; textFormat: Text.PlainText; font.weight: Font.DemiBold }
                    Label { width: parent.width; text: W.icon(record.status) + " " + W.label(record.status) + " · " + (record.repositoryPath || ""); elide: Text.ElideMiddle; color: W.color(record.status); font.pixelSize: 11 }
                }
            }
            ScrollBar.vertical: Controls.AppScrollBar {}
        }
        Label { text: "任务列表仅包含本次会话创建或打开的任务。"; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 10; color: Theme.muted }
    }
}
