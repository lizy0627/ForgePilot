import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Ui.js" as Ui

Panel {
    id: root
    required property var controller
    title: "执行记录"; subtitle: controller.traces.count + " 次工具调用"
    property bool showStored: false
    Connections { target: root.controller; function onTaskChanged() { if (!root.controller.taskId) root.showStored = false } }
    Label { visible: !root.controller.traces.count; text: "工具开始执行后显示执行记录。"; color: Theme.muted }
    ListView {
        id: traces; objectName: "traceList"; model: root.controller.traces
        Layout.fillWidth: true; Layout.preferredHeight: count ? 230 : 0; clip: true; spacing: Theme.spaceSm
        delegate: Rectangle {
            id: row
            required property var record
            width: traces.width - 12; height: body.implicitHeight + 16; radius: Theme.radiusSm; color: Theme.raised
            ColumnLayout {
                id: body; anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.spaceSm } spacing: Theme.spaceXs
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: row.record.name; font.bold: true; color: Theme.text; textFormat: Text.PlainText }
                    StatusBadge { status: row.record.status }
                    Label { text: Ui.duration(row.record.durationMs); color: Theme.muted; Layout.fillWidth: true; horizontalAlignment: Text.AlignRight }
                }
                Label {
                    id: argumentsPreview
                    text: row.record.argumentsSummary || "无参数"; textFormat: Text.PlainText
                    Layout.fillWidth: true; color: Theme.secondary; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                }
                LongText { visible: argumentsPreview.truncated || argumentsPreview.text.length > 100; Layout.fillWidth: true; title: "参数摘要"; text: argumentsPreview.text; maximumHeight: 180 }
                Label { text: "开始 " + Ui.time(row.record.startedAt) + "  /  结束 " + Ui.time(row.record.finishedAt); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; font.pixelSize: Theme.fontSizeXs; color: Theme.muted }
                Label { text: row.record.error || ""; visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; color: Theme.danger }
                LongText { Layout.fillWidth: true; title: "参数与结果"; text: "参数\n" + (row.record.arguments || "{}") + "\n\n结果\n" + (row.record.output || "尚无结果") + "\n\n错误\n" + (row.record.error || "无"); maximumHeight: 220 }
            }
        }
        ScrollBar.vertical: AppScrollBar {}
    }
    RowLayout {
        Layout.fillWidth: true
        ActionButton { objectName: "refreshTraceButton"; text: root.controller.traceLoading ? "加载中…" : "完整执行记录"; enabled: root.controller.taskId.length > 0 && !root.controller.traceLoading; onClicked: { root.showStored=true; root.controller.refreshTrace() } }
        Label { text: "任务 / 模型 / 工具 / 构建 / 测试 / 修复"; color: Theme.muted; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: Theme.fontSizeXs }
    }
    ListView {
        id: stored; objectName: "storedTraceList"; visible: root.showStored
        Layout.fillWidth: true; Layout.preferredHeight: visible && count ? 160 : 0; clip: true
        model: root.controller.storedTrace
        delegate: ItemDelegate {
            hoverEnabled: true
            background: Rectangle { radius: Theme.radiusSm; color: parent.highlighted ? Theme.selection : parent.down ? Theme.pressed : parent.hovered ? Theme.hover : Theme.input }
            id: traceItem
            required property var record
            width: stored.width; height: Math.max(58, contentItem.implicitHeight + Theme.spaceSm * 2)
            onClicked: root.controller.loadTraceDetail(record.sequence)
            contentItem: ColumnLayout {
                Label { text: "#" + traceItem.record.trace_sequence + " " + Ui.eventKind(traceItem.record.kind) + "  ·  " + Ui.time(traceItem.record.timestamp); textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: Theme.text }
                Label { text: traceItem.record.preview; textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: Theme.muted; font.pixelSize: Theme.fontSizeXs }
                Label { text: "点击查看完整记录与复制全文"; Layout.fillWidth: true; color: Theme.accent; font.pixelSize: Theme.fontSizeXs }
            }
        }
        ScrollBar.vertical: AppScrollBar {}
    }
    ActionButton { visible: root.showStored && root.controller.traceHasMore; text: "更多执行记录"; enabled: !root.controller.traceLoading; onClicked: root.controller.loadMoreTrace() }
    Label { visible: root.showStored && !root.controller.traceLoading && !stored.count; text: "暂无执行记录。"; color: Theme.muted }
    LongText { objectName: "storedTraceDetail"; visible: root.controller.traceDetailText.length > 0; Layout.fillWidth: true; title: "选中事件完整详情"; text: root.controller.traceDetailText; expanded: true }
}
