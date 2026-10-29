import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    readonly property var artifact: W.artifact(mission.artifacts, "TaskSpec")
    readonly property var spec: artifact.content || ({})
    title: "TaskSpec · 任务说明"
    subtitle: artifact.contentHash ? artifact.contentHash.substring(0, 12) : ""
    objectName: "taskSpecPanel"
    StateNotice { mission: root.mission; hasData: !!root.artifact.artifactId; emptyText: "TaskSpec 尚未生成。"; Layout.fillWidth: true }
    Label { visible: !!root.spec.title; text: root.spec.title || ""; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    Label { visible: !!root.spec.objective; text: root.spec.objective || ""; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    Label { visible: !!root.spec.problem; text: "预期行为：" + ((root.spec.problem || {}).expected_behavior || ""); Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.secondary; textFormat: Text.PlainText }
    Label { visible: !!root.spec.acceptance_criteria; text: "验收标准"; font.weight: Font.DemiBold }
    Repeater { model: root.spec.acceptance_criteria || []
        delegate: Label { required property var modelData; text: modelData.id + " · " + modelData.description + "（" + modelData.verification + "）"; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    }
    Label { visible: !!root.spec.risk_level; text: "风险级别：" + (root.spec.risk_level || ""); color: Theme.warning }
}
