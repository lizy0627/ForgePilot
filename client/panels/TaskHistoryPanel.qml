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
        ActionButton { objectName: "refreshHistoryButton"; text: "刷新"; enabled: !root.controller.historyLoading; onClicked: root.controller.refreshTasks() }
    }
    Label { visible: !root.controller.tasks.length; text: "暂无任务，或后端尚未连接。"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap }
    ListView {
        id: history; objectName: "historyList"; model: root.controller.tasks
        Layout.fillWidth: true; Layout.preferredHeight: count ? Math.min(320, count * 100) : 0; visible: count > 0; clip: true; spacing: 6
        delegate: ItemDelegate {
            hoverEnabled: true
            background: Rectangle { radius: 4; color: parent.highlighted ? Theme.selection : parent.down ? Theme.pressed : parent.hovered ? Theme.hover : Theme.input }
            id: entry
            required property var modelData
            width: history.width; height: 94
            highlighted: root.controller.taskId === modelData.task_id
            onClicked: root.controller.viewHistoryTask(modelData.task_id)
            contentItem: ColumnLayout {
                spacing: 3
                Label { text: entry.modelData.prompt || entry.modelData.task_id; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText; color: Theme.text; font.bold: true }
                Label { text: entry.modelData.repository_path || ""; Layout.fillWidth: true; elide: Text.ElideMiddle; textFormat: Text.PlainText; color: Theme.muted; font.pixelSize: 11 }
                RowLayout {
                    StatusBadge { status: entry.modelData.state }
                    Label { text: Ui.time(entry.modelData.created_at); elide: Text.ElideRight; Layout.fillWidth: true; font.pixelSize: 10; color: Theme.muted }
                }
            }
        }
        ScrollBar.vertical: AppScrollBar {}
    }
    ActionButton { objectName: "moreHistoryButton"; text: root.controller.historyLoading ? "加载中…" : "加载更多"; visible: root.controller.historyHasMore || root.controller.historyLoading; enabled: !root.controller.historyLoading; onClicked: root.controller.loadMoreTasks() }
    Label { text: "SQLite 历史 · 点击查看只读详情。服务重启后不恢复执行。"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11 }
}
