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
    Component.onCompleted: { if (!mission.historyLoaded && !mission.historyLoading) mission.refreshMissions() }
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
        RowLayout {
            Layout.fillWidth: true; Layout.topMargin: 8
            Label { text: "任务历史"; color: Theme.text; font.weight: Font.DemiBold; Layout.fillWidth: true }
            Controls.ActionButton {
                objectName: "missionHistoryRefresh"; text: "刷新"
                enabled: !root.mission.historyLoading; onClicked: root.mission.refreshMissions()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            TextField {
                id: repositoryFilterInput; objectName: "missionRepositoryFilter"
                Layout.fillWidth: true; placeholderText: "按仓库路径筛选（留空为全部）"
                text: root.mission.repositoryFilter
                onAccepted: root.mission.repositoryFilter = text
            }
            Controls.ActionButton { objectName: "missionApplyRepositoryFilter"; text: "筛选"; onClicked: root.mission.repositoryFilter = repositoryFilterInput.text }
        }
        ComboBox {
            id: statusFilterInput; objectName: "missionStatusFilter"; Layout.fillWidth: true
            property var values: ["", "pending", "ready", "running", "awaiting_human", "interrupted", "retryable", "succeeded", "failed", "cancelled", "skipped"]
            model: ["全部状态", "待执行", "已就绪", "执行中", "等待确认", "已中断", "可重试", "已成功", "失败", "已取消", "已跳过"]
            Accessible.name: "按任务状态筛选"
            currentIndex: Math.max(0, values.indexOf(root.mission.statusFilter))
            onActivated: root.mission.statusFilter = values[currentIndex]
        }
        Label {
            objectName: "missionHistoryError"; visible: !!root.mission.historyError
            text: root.mission.historyError; color: Theme.danger; textFormat: Text.PlainText
            Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
        }
        Label {
            objectName: "missionHistoryStatus"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap
            text: root.mission.historyLoading ? "正在读取任务历史…"
                : root.mission.missions.count ? "已显示 " + root.mission.missions.count + " / " + root.mission.historyTotal + " 个任务"
                : root.mission.historyError ? "读取失败，请刷新重试。" : "暂无符合条件的任务。"
        }
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
        Controls.ActionButton {
            objectName: "missionHistoryLoadMore"; text: root.mission.historyLoading ? "读取中…" : "加载更多"
            visible: root.mission.historyHasMore; enabled: !root.mission.historyLoading
            Layout.fillWidth: true; onClicked: root.mission.loadMoreMissions()
        }
        Label { text: "打开历史只查看状态；中断任务需确认恢复计划后才能继续。"; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 10; color: Theme.muted }
    }
}
