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
        delegate: RowLayout {
            required property var modelData
            Layout.fillWidth: true
            Label { text: W.icon(modelData.state || "pending"); color: W.color(modelData.state || "pending") }
            Label { text: modelData.event_type || "任务事件"; textFormat: Text.PlainText; Layout.fillWidth: true; elide: Text.ElideRight }
            Label { text: modelData.node_id || ""; color: Theme.secondary; elide: Text.ElideRight; Layout.maximumWidth: 150 }
            Label { text: modelData.timestamp ? String(modelData.timestamp).substring(11,19) : ""; color: Theme.muted }
        }
    }
}
