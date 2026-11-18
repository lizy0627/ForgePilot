import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    readonly property var artifact: W.artifact(mission.artifacts, "ReviewReport")
    readonly property var review: artifact.content || ({})
    title: "Review · 审查结论"
    subtitle: review.verdict || ""
    objectName: "reviewPanel"
    StateNotice { mission: root.mission; hasData: !!root.artifact.artifactId; emptyText: "审查尚未完成。"; Layout.fillWidth: true }
    Label { visible: !!root.review.summary; text: root.review.summary || ""; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; font.weight: Font.DemiBold }
    Label { visible: !!root.review.verdict; text: "结论：" + root.review.verdict + " · 建议交付：" + (root.review.recommend_delivery ? "是" : "否"); color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Repeater { model: root.review.acceptance_criteria || []
        delegate: Label { required property var modelData; text: modelData.criterion_id + " · " + modelData.status + " · " + modelData.evidence; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    }
    Label { visible: !!(root.review.blocking_issues || []).length; text: "阻塞问题：" + W.lines(root.review.blocking_issues); Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.danger; textFormat: Text.PlainText }
    Label { visible: !!root.review.test_review; text: "测试审查：" + (root.review.test_review || ""); Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    Label { visible: !!root.review.security_review; text: "安全审查：" + (root.review.security_review || ""); Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
}
