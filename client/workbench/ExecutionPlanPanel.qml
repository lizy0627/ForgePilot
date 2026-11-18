import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    readonly property var artifact: W.artifact(mission.artifacts, "ExecutionPlan")
    readonly property var plan: artifact.content || ({})
    title: "ExecutionPlan · 执行方案"
    subtitle: artifact.contentHash || ""
    objectName: "executionPlanPanel"
    StateNotice { mission: root.mission; hasData: !!root.artifact.artifactId; emptyText: "执行方案尚未生成。"; Layout.fillWidth: true }
    Label { visible: !!root.plan.summary; text: root.plan.summary || ""; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    Repeater { model: root.plan.steps || []
        delegate: ColumnLayout {
            required property var modelData
            Layout.fillWidth: true; spacing: Theme.spaceXs
            Label { text: modelData.step_id + " · " + modelData.action; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
            Label { text: "负责：" + modelData.owner + " · 文件：" + W.lines(modelData.expected_files); color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
            Label { text: "验证：" + W.lines(modelData.verification) + " · 风险：" + modelData.risk; color: Theme.warning; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        }
    }
}
