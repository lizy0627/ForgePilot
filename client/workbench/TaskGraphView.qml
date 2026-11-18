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
            Layout.fillWidth: true; implicitHeight: nodeLayout.implicitHeight + Theme.spaceSm * 2
            color: Theme.raised; radius: Theme.radiusMd; border.color: Theme.border
            Accessible.role: Accessible.StaticText
            Accessible.name: record.nodeId + "，" + W.label(record.status) + "，执行者 " + (record.agentId || "系统")
            ColumnLayout {
                id: nodeLayout; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Theme.spaceSm; spacing: Theme.spaceXs
                Label {
                    id: nodeName
                    objectName: "missionGraphNodePreview"
                    text: record.nodeId; font.weight: Font.DemiBold; textFormat: Text.PlainText
                    Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                }
                Controls.LongText {
                    objectName: "missionGraphNodeFull"
                    visible: nodeName.truncated || record.nodeId.length > 80
                    Layout.fillWidth: true; title: "完整节点 ID"; text: record.nodeId
                    maximumHeight: 160
                }
                Label {
                    id: statusLabel
                    objectName: "missionGraphStatusPreview"
                    text: W.icon(record.status) + " " + W.label(record.status)
                    color: W.color(record.status); font.pixelSize: Theme.fontSizeSm; font.weight: Font.DemiBold
                    textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                }
                Controls.LongText {
                    objectName: "missionGraphStatusFull"
                    visible: statusLabel.truncated || W.label(record.status).length > 60
                    Layout.fillWidth: true; title: "完整节点状态"; text: W.label(record.status)
                    monospace: false; maximumHeight: 140
                }
                Label {
                    id: agentLabel
                    objectName: "missionGraphAgentPreview"
                    text: "执行者：" + (record.agentId || "系统") + " · 第 " + record.attempt + " 次"
                    color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                    textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                }
                Controls.LongText {
                    objectName: "missionGraphAgentFull"
                    visible: agentLabel.truncated || (record.agentId || "").length > 80
                    Layout.fillWidth: true; title: "完整执行者信息"; text: agentLabel.text
                    maximumHeight: 160
                }
                Label {
                    id: dependencyLabel
                    objectName: "missionGraphDependenciesPreview"
                    visible: !!record.dependencies.length
                    text: "依赖：" + W.lines(record.dependencies)
                    color: Theme.muted; font.pixelSize: Theme.fontSizeSm; textFormat: Text.PlainText
                    Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2; elide: Text.ElideRight
                }
                Controls.LongText {
                    objectName: "missionGraphDependenciesFull"
                    visible: dependencyLabel.visible && (dependencyLabel.truncated || dependencyLabel.text.length > 100)
                    Layout.fillWidth: true; title: "完整依赖列表"; text: dependencyLabel.text
                    maximumHeight: 160
                }
                Label {
                    visible: !!record.result && !!record.result.decision_summary
                    text: record.result ? (record.result.decision_summary || "") : ""
                    color: Theme.secondary; textFormat: Text.PlainText
                    wrapMode: Text.WrapAnywhere; Layout.fillWidth: true
                }
                Label {
                    text: "耗时 " + (record.metrics.duration_ms || 0) + " ms · 工具 " + (record.metrics.tool_calls || 0) + " · Token " + (record.metrics.total_tokens || 0)
                    color: Theme.muted; font.pixelSize: Theme.fontSizeSm; Layout.fillWidth: true
                    textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                }
                Controls.ActionButton { text: "重试节点"; visible: record.actions && record.actions.indexOf("retry") >= 0; enabled: root.mission.synchronized && !root.mission.busy; onClicked: root.mission.retry(record.nodeId) }
            }
        }
    }
}
