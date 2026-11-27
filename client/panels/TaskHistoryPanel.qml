import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Ui.js" as Ui

Panel {
    id: root
    required property var controller
    title: "历史记录"; subtitle: controller.tasks.length + " / " + controller.historyTotal
    RowLayout {
        Layout.fillWidth: true
        Label { text: "持久化任务历史"; color: Theme.secondary; Layout.fillWidth: true }
        ActionButton { objectName: "refreshHistoryButton"; text: "刷新"; icon.source: Theme.icon("refresh"); enabled: !root.controller.historyLoading; onClicked: root.controller.refreshTasks() }
    }
    Label { visible: !root.controller.tasks.length; text: "暂无任务，或后端尚未连接。"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap }
    ListView {
        id: history; objectName: "historyList"; model: root.controller.tasks
        Layout.fillWidth: true; Layout.preferredHeight: count ? Math.min(320, count * 100) : 0; visible: count > 0; clip: true; spacing: Theme.spaceSm
        delegate: AppItemDelegate {
            id: entry
            required property var modelData
            padding: Theme.spaceMd
            Accessible.name: (modelData.prompt || modelData.task_id) + "，" + Ui.status(modelData.state)
            width: history.width - Theme.spaceMd; height: Math.max(94, card.implicitHeight + Theme.spaceMd * 2)
            highlighted: root.controller.taskId === modelData.task_id
            onClicked: root.controller.viewHistoryTask(modelData.task_id)
            contentItem: ColumnLayout {
                id: card
                spacing: Theme.spaceXs
                Label {
                    id: promptPreview
                    text: entry.modelData.prompt || entry.modelData.task_id; Layout.fillWidth: true
                    wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                    textFormat: Text.PlainText; color: Theme.text; font.bold: true
                }
                Label {
                    id: pathPreview
                    text: entry.modelData.repository_path || ""; visible: !!text; Layout.fillWidth: true
                    wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideMiddle
                    textFormat: Text.PlainText; color: Theme.muted; font.family: Theme.fontMono; font.pixelSize: Theme.fontSizeSm
                }
                RowLayout {
                    Layout.fillWidth: true
                    StatusBadge {
                        id: statePreview; status: entry.modelData.state
                        Layout.maximumWidth: Math.max(90, card.width * 0.5)
                        elide: Text.ElideRight
                    }
                    Label { text: Ui.time(entry.modelData.created_at); wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; font.pixelSize: Theme.fontSizeXs; color: Theme.muted }
                }
                LongText {
                    objectName: "taskHistoryFullInfo"
                    visible: promptPreview.truncated || pathPreview.truncated || statePreview.truncated
                    Layout.fillWidth: true; title: "完整任务信息"
                    text: "任务：" + (entry.modelData.prompt || "") + "\n项目：" + (entry.modelData.repository_path || "")
                        + "\n状态：" + (entry.modelData.state || "") + "\nID：" + (entry.modelData.task_id || "")
                    monospace: false; maximumHeight: 190
                }
            }
        }
        ScrollBar.vertical: AppScrollBar {}
    }
    ActionButton { objectName: "moreHistoryButton"; text: root.controller.historyLoading ? "加载中…" : "加载更多"; visible: root.controller.historyHasMore || root.controller.historyLoading; enabled: !root.controller.historyLoading; onClicked: root.controller.loadMoreTasks() }
    Label { text: "SQLite 历史 · 点击查看只读详情。服务重启后不恢复执行。"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSizeXs }
}
