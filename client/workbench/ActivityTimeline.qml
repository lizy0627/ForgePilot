import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    property var events: []
    title: "活动时间线"
    subtitle: "最近 " + events.length + " 条"
    objectName: "activityTimeline"
    StateNotice { mission: root.mission; hasData: root.events.length > 0; emptyText: "尚无实时活动。打开任务后会显示新事件。"; Layout.fillWidth: true }
    Repeater {
        model: root.events.slice(Math.max(0, root.events.length - 20)).reverse()
        delegate: ColumnLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: Theme.spaceXs
            Label { text: W.icon(modelData.state || "pending") + " " + W.label(modelData.state || "pending"); color: W.color(modelData.state || "pending"); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
            Label {
                id: eventPreview
                text: modelData.event_type || "任务事件"; textFormat: Text.PlainText
                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                maximumLineCount: 2; elide: Text.ElideRight
            }
            Controls.LongText { visible: eventPreview.truncated || eventPreview.text.length > 80; Layout.fillWidth: true; title: "事件类型"; text: eventPreview.text; monospace: false }
            Label {
                id: nodePreview
                text: modelData.node_id || ""; color: Theme.secondary; textFormat: Text.PlainText
                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                maximumLineCount: 2; elide: Text.ElideRight
            }
            Controls.LongText { visible: nodePreview.truncated || nodePreview.text.length > 80; Layout.fillWidth: true; title: "节点标识"; text: nodePreview.text }
            Label { text: modelData.timestamp ? String(modelData.timestamp) : ""; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        }
    }
}
