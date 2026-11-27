import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

ColumnLayout {
    id: root
    required property var mission
    property bool navMode: false
    property bool filtersExpanded: false
    signal taskOpened()
    spacing: Theme.spaceSm

    function openTask(id) {
        root.mission.selectMission(id)
        root.taskOpened()
    }

    Component.onCompleted: {
        if (!mission.historyLoaded && !mission.historyLoading) mission.refreshMissions()
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spaceXs
        Label {
            text: "任务历史"
            Layout.fillWidth: true
            font.weight: Font.DemiBold
            color: root.navMode ? Theme.secondary : Theme.text
        }
        Controls.ActionButton {
            objectName: "missionHistoryFiltersToggle"
            text: "筛选"
            Accessible.name: "筛选历史或按任务 ID 打开"
            visible: root.navMode
            checked: root.filtersExpanded
            flat: true; implicitWidth: 42; horizontalPadding: 4
            onClicked: root.filtersExpanded = !root.filtersExpanded
        }
        Controls.ActionButton {
            objectName: "missionHistoryRefresh"
            text: "刷新"
            Accessible.name: "刷新任务历史"
            flat: root.navMode; implicitWidth: 42; horizontalPadding: 4
            enabled: !root.mission.historyLoading
            onClicked: root.mission.refreshMissions()
        }
    }
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Theme.spaceSm
        visible: !root.navMode || root.filtersExpanded
        RowLayout {
            Layout.fillWidth: true
            Controls.AppTextField {
                id: taskIdInput; objectName: "missionIdInput"
                Layout.fillWidth: true; Layout.minimumWidth: 0
                placeholderText: "输入任务 ID"
                onAccepted: if (text.trim()) root.openTask(text.trim())
            }
            Controls.ActionButton {
                objectName: "missionOpenById"
                text: "打开"; implicitWidth: 46; horizontalPadding: 4
                enabled: !!taskIdInput.text.trim()
                onClicked: root.openTask(taskIdInput.text.trim())
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Controls.AppTextField {
                id: repositoryFilterInput; objectName: "missionRepositoryFilter"
                Layout.fillWidth: true; Layout.minimumWidth: 0
                placeholderText: "按仓库路径筛选"
                text: root.mission.repositoryFilter
                onAccepted: root.mission.repositoryFilter = text
            }
            Controls.ActionButton {
                objectName: "missionApplyRepositoryFilter"
                text: "筛选"; implicitWidth: 46; horizontalPadding: 4
                onClicked: root.mission.repositoryFilter = repositoryFilterInput.text
            }
        }
        Controls.AppComboBox {
            id: statusFilterInput; objectName: "missionStatusFilter"; Layout.fillWidth: true
            property var values: ["", "pending", "ready", "running", "awaiting_human", "interrupted", "retryable", "succeeded", "failed", "cancelled", "skipped"]
            model: ["全部状态", "待执行", "已就绪", "执行中", "等待确认", "已中断", "可重试", "已成功", "失败", "已取消", "已跳过"]
            Accessible.name: "按任务状态筛选"
            currentIndex: Math.max(0, values.indexOf(root.mission.statusFilter))
            onActivated: root.mission.statusFilter = values[currentIndex]
        }
    }
    Label {
        objectName: "missionHistoryError"; visible: !!root.mission.historyError
        text: root.mission.historyError; color: Theme.danger; textFormat: Text.PlainText
        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
    }
    Label {
        objectName: "missionHistoryStatus"
        color: root.navMode ? Theme.muted : Theme.secondary
        Layout.fillWidth: true; wrapMode: Text.Wrap
        visible: !root.navMode || root.filtersExpanded || root.mission.historyLoading || !root.mission.missions.count
        text: root.mission.historyLoading ? "正在读取任务历史…"
            : root.mission.missions.count ? "已显示 " + root.mission.missions.count + " / " + root.mission.historyTotal + " 个任务"
            : root.mission.historyError ? "读取失败，请刷新重试。"
            : root.mission.repositoryFilter || root.mission.statusFilter ? "没有匹配的任务" : "暂无任务历史"
    }
    ListView {
        id: taskList; objectName: "missionTaskList"
        Layout.fillWidth: true
        Layout.fillHeight: root.navMode
        Layout.minimumHeight: 0
        Layout.preferredHeight: root.navMode ? 160 : Math.min(260, Math.max(100, contentHeight + Theme.spaceSm))
        clip: true; model: root.mission.missions; spacing: root.navMode ? 2 : Theme.spaceSm
        boundsBehavior: Flickable.StopAtBounds
        delegate: ColumnLayout {
            id: taskEntry
            required property var record
            property string taskId: record.taskId
            width: taskList.width; spacing: Theme.spaceXs
            Controls.ActionButton {
                id: selectTask
                objectName: "missionTaskRow"
                property string taskId: taskEntry.taskId
                Layout.fillWidth: true
                implicitHeight: Math.max(Theme.controlHeight, taskSummary.implicitHeight + taskMetadata.implicitHeight + Theme.spaceSm * 2 + Theme.spaceXs)
                checked: taskEntry.record.taskId === root.mission.taskId
                Accessible.name: "选择任务：" + (taskEntry.record.request || taskEntry.record.taskId) + "，" + W.label(taskEntry.record.status)
                flat: root.navMode
                horizontalPadding: root.navMode ? Theme.spaceSm : Theme.spaceLg
                onClicked: root.openTask(taskEntry.taskId)
                contentItem: Column {
                    spacing: Theme.spaceXs
                    Label {
                        id: taskSummary
                        objectName: "missionTaskPreview"
                        width: parent.width; text: taskEntry.record.request || taskEntry.record.taskId
                        wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                        textFormat: Text.PlainText; font.weight: Font.DemiBold; color: Theme.text
                    }
                    Label {
                        id: taskMetadata
                        objectName: "missionTaskMetadataPreview"
                        width: parent.width
                        text: W.label(taskEntry.record.status) + " · " + (taskEntry.record.repositoryPath || "")
                        wrapMode: Text.WrapAnywhere; maximumLineCount: root.navMode ? 1 : 2; elide: Text.ElideRight
                        textFormat: Text.PlainText; color: W.color(taskEntry.record.status); font.pixelSize: Theme.fontSizeSm
                    }
                }
            }
            Controls.LongText {
                objectName: "missionTaskFull"
                visible: (!root.navMode || selectTask.checked) && (taskSummary.truncated || taskMetadata.truncated
                    || (taskEntry.record.request || "").length > 60 || (taskEntry.record.repositoryPath || "").length > 30)
                Layout.fillWidth: true; title: "任务标题、状态和路径"
                text: "任务：" + (taskEntry.record.request || taskEntry.record.taskId)
                    + "\n状态：" + W.label(taskEntry.record.status)
                    + "\n仓库：" + (taskEntry.record.repositoryPath || "—")
                    + "\n任务 ID：" + taskEntry.record.taskId
                monospace: false; maximumHeight: 180
            }
        }
        ScrollBar.vertical: Controls.AppScrollBar {}
    }
    Controls.ActionButton {
        objectName: "missionHistoryLoadMore"; text: root.mission.historyLoading ? "读取中…" : "加载更多"
        visible: root.mission.historyHasMore; enabled: !root.mission.historyLoading
        Layout.fillWidth: true; onClicked: root.mission.loadMoreMissions()
    }
}
