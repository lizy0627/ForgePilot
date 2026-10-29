import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    title: "任务图"
    subtitle: mission.graph.count + " 个节点"
    objectName: "taskGraphView"
    StateNotice { mission: root.mission; hasData: root.mission.graph.count > 0; emptyText: "任务图尚未生成。"; Layout.fillWidth: true }
    Repeater {
        model: root.mission.graph
        delegate: Rectangle {
            required property var record
            Layout.fillWidth: true; implicitHeight: nodeLayout.implicitHeight + 18
            color: Theme.raised; radius: 8
            Accessible.role: Accessible.StaticText
            Accessible.name: record.nodeId + "，" + W.label(record.status) + "，执行者 " + (record.agentId || "系统")
            ColumnLayout {
                id: nodeLayout; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 9; spacing: 4
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: record.nodeId; font.weight: Font.DemiBold; textFormat: Text.PlainText; Layout.fillWidth: true; elide: Text.ElideRight }
                    NodeStatusBadge { status: record.status }
                }
                Label { text: "执行者：" + (record.agentId || "系统") + " · 第 " + record.attempt + " 次"; color: Theme.secondary; font.pixelSize: 11 }
                Label { visible: !!record.dependencies.length; text: "依赖：" + W.lines(record.dependencies); color: Theme.muted; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideRight }
                Label { visible: !!record.result && !!record.result.decision_summary; text: record.result ? (record.result.decision_summary || "") : ""; color: Theme.secondary; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
                Label { text: "耗时 " + (record.metrics.duration_ms || 0) + " ms · 工具 " + (record.metrics.tool_calls || 0) + " · Token " + (record.metrics.total_tokens || 0); color: Theme.muted; font.pixelSize: 11 }
                Controls.ActionButton { text: "重试节点"; visible: record.actions && record.actions.indexOf("retry") >= 0; enabled: root.mission.synchronized && !root.mission.busy; onClicked: root.mission.retry(record.nodeId) }
            }
        }
    }
}
