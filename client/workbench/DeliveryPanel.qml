import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    readonly property var artifact: W.artifact(mission.artifacts, "DeliveryBundle")
    readonly property var bundle: artifact.content || ({})
    readonly property var preview: mission.delivery.preview || ({})
    readonly property var result: mission.delivery.result || ({})
    readonly property var modes: ["patch", "bundle", "keep_branch", "squash", "abandon", "recover"]
    readonly property bool recovering: modes[mode.currentIndex] === "recover"
    function outcomeText(outcome) {
        if (outcome === "completed") return "合并已完成"
        if (outcome === "restored") return "目标已恢复原状"
        if (outcome === "manual_inspection") return "需要人工检查"
        return ""
    }
    readonly property string bundleDetails: "验证：" + (bundle.build_test_summary || "—")
        + "\n审查：" + (bundle.reviewer_conclusion || "—")
        + "\n修改文件：" + W.lines(bundle.changed_files)
        + "\n补丁文件：" + (bundle.patch_path || "—")
        + "\n残余风险：" + W.lines(bundle.risks)
        + "\n回退：" + (bundle.rollback || "—")
    readonly property string previewDetails: preview.outcome
        ? "核验结果：" + outcomeText(preview.outcome)
          + "\n目标分支：" + preview.target_branch
          + "\n当前目标提交：" + preview.current_target_commit
          + "\n合并前提交：" + preview.parent_commit
          + "\n预期合并提交：" + preview.delivery_commit
          + "\n目标工作区干净：" + (preview.target_clean ? "是" : "否")
          + (preview.reason ? "\n原因：" + preview.reason : "")
        : "目标：" + (preview.target_branch || mission.delivery.previewTarget || "默认交付目标")
        + "\n任务提交：" + (preview.head_commit || "—")
        + "\n目标提交：" + (preview.target_commit || "—")
        + "\n目标干净：" + (preview.target_clean ? "是" : "否")
        + " · 验证匹配：" + (preview.validated ? "是" : "否")
        + "\n当前修订：" + mission.revision
    readonly property string resultDetails: result.outcome
        ? "已登记：" + outcomeText(result.outcome)
          + "\n目标分支：" + result.target_branch + "\n目标提交：" + result.target_commit
        : "交付完成：" + (result.mode || "")
        + "\n目标分支：" + (result.target_branch || result.branch || "—")
        + "\n提交：" + (result.commit || result.task_commit || "—")
        + "\n文件：" + (result.patch_path || (result.patch || {}).path || result.bundle_path || (result.bundle || {}).path || "—")
    readonly property string confirmDetails: preview.outcome ? previewDetails + "\n确认后仅登记核验结果。"
        : "任务 " + mission.taskId + "，修订 " + mission.revision + "。"
        + "\n方式：" + mission.delivery.previewMode
        + "\n目标：" + (preview.target_branch || mission.delivery.previewTarget || "默认")
        + "\n" + (mission.delivery.previewMode === "abandon" ? "隔离工作区将被丢弃，未交付改动无法从该工作区恢复。"
            : mission.delivery.previewMode === "squash" ? "目标分支引用会改变，并创建合并提交。" : "将执行已预览的交付操作。")
    title: "Delivery · 交付"
    subtitle: mission.status === "succeeded" ? "可预览交付" : ""
    objectName: "deliveryPanel"
    StateNotice { mission: root.mission; hasData: !!root.artifact.artifactId; emptyText: "尚无交付包。"; Layout.fillWidth: true }
    Label { visible: !!root.bundle.build_test_summary; text: "验证：" + (root.bundle.build_test_summary || ""); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Label { visible: !!root.bundle.reviewer_conclusion; text: "审查：" + (root.bundle.reviewer_conclusion || ""); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Label { visible: !!root.bundle.changed_files; text: "修改文件：" + W.lines(root.bundle.changed_files); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Label { visible: !!root.bundle.risks; text: "残余风险：" + W.lines(root.bundle.risks); color: Theme.warning; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Label { visible: !!root.bundle.rollback; text: "回退：" + (root.bundle.rollback || ""); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    Controls.LongText { objectName: "missionDeliveryBundleDetails"; visible: !!root.artifact.artifactId
        Layout.fillWidth: true; title: "交付包全文"; text: root.bundleDetails; maximumHeight: 220 }
    Controls.AppComboBox { id: mode; objectName: "missionDeliveryMode"; Layout.fillWidth: true; enabled: !root.mission.delivery.busy && !root.preview.token
        model: ["生成补丁", "生成归档", "保留分支", "压缩合并到目标分支", "放弃隔离工作区", "核验中断的合并"]
        Accessible.name: "交付方式"
        Binding on currentIndex {
            when: !!root.mission.delivery.previewMode
            value: root.modes.indexOf(root.mission.delivery.previewMode)
            restoreMode: Binding.RestoreNone
        }
    }
    Controls.AppTextField { id: target; objectName: "missionDeliveryTarget"; visible: !root.recovering; Layout.fillWidth: true; enabled: !root.mission.delivery.busy && !root.preview.token; placeholderText: "目标分支（留空使用默认；仅合并时必填）"
        Binding on text {
            when: !!root.mission.delivery.previewMode
            value: root.mission.delivery.previewTarget
            restoreMode: Binding.RestoreNone
        }
    }
    Controls.ActionButton {
        objectName: "missionDeliveryPreviewButton"; Layout.fillWidth: true
        readonly property bool abandoning: root.modes[mode.currentIndex] === "abandon"
        text: root.recovering ? "核验目标分支现状" : abandoning && root.mission.delivery.abandonBlockReason ? root.mission.delivery.abandonBlockReason : "预览交付操作"
        enabled: root.mission.delivery.canPreview && (abandoning ? !root.mission.delivery.abandonBlockReason : root.mission.status === "succeeded")
        onClicked: root.mission.delivery.prepare(root.modes[mode.currentIndex], target.text.trim())
    }
    Label { visible: !!root.preview.token; text: root.previewDetails
        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; maximumLineCount: 5; elide: Text.ElideRight; textFormat: Text.PlainText }
    Controls.LongText { objectName: "missionDeliveryPreviewDetails"; visible: !!root.preview.token
        Layout.fillWidth: true; title: "完整交付预览"; text: root.previewDetails; maximumHeight: 220 }
    Label { objectName: "missionDeliveryWarning"; visible: !!root.preview.token; text: root.preview.outcome === "manual_inspection" ? root.preview.reason
        : root.recovering ? "确认后仅登记当前结果。"
        : root.mission.delivery.previewMode === "abandon" ? "危险：将丢弃任务的隔离工作区；其中未交付的改动将无法从该工作区恢复。"
        : root.mission.delivery.previewMode === "squash" ? "危险：将在目标分支 " + (root.preview.target_branch || root.mission.delivery.previewTarget) + " 创建合并提交，并改变该分支引用。"
        : root.mission.delivery.previewMode === "keep_branch" ? "将保留任务分支与提交；请核对预览中的目标分支。"
        : "将生成交付文件；请核对目标和当前提交。"; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: root.mission.delivery.previewMode === "abandon" || root.mission.delivery.previewMode === "squash" ? Theme.danger : Theme.warning; textFormat: Text.PlainText }
    Controls.ActionButton { objectName: "missionDeliveryConfirmButton"; visible: !!root.preview.token && root.preview.outcome !== "manual_inspection"; text: root.recovering ? "确认" + root.outcomeText(root.preview.outcome) : "确认执行交付"; danger: root.mission.delivery.previewMode === "abandon" || root.mission.delivery.previewMode === "squash"; enabled: root.mission.delivery.canConfirm; onClicked: confirmDialog.open() }
    Label { visible: !!root.result.mode || !!root.result.outcome; text: root.resultDetails; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
        maximumLineCount: 5; elide: Text.ElideRight; color: Theme.success; textFormat: Text.PlainText }
    Controls.LongText { objectName: "missionDeliveryResultDetails"; visible: !!root.result.mode || !!root.result.outcome
        Layout.fillWidth: true; title: "完整交付结果"; text: root.resultDetails; maximumHeight: 220 }
    Label { visible: !!root.mission.delivery.error; text: root.mission.delivery.error; Layout.fillWidth: true
        wrapMode: Text.WrapAnywhere; maximumLineCount: 5; elide: Text.ElideRight; color: Theme.danger; textFormat: Text.PlainText }
    Controls.LongText { objectName: "missionDeliveryErrorDetails"; visible: !!root.mission.delivery.error
        Layout.fillWidth: true; title: "完整交付错误"; text: root.mission.delivery.error; maximumHeight: 220 }
    Connections {
        target: root.mission.delivery
        function onChanged() {
            if (!root.mission.delivery.canConfirm) confirmDialog.close()
        }
    }
    Dialog {
        id: confirmDialog; objectName: "missionDeliveryConfirmDialog"; title: root.recovering ? "确认核验结果" : "确认交付操作"; modal: true; anchors.centerIn: Overlay.overlay
        width: Math.min(440, Math.max(240, Overlay.overlay ? Overlay.overlay.width - Theme.spaceLg * 2 : 440))
        padding: Theme.spaceLg
        background: Rectangle { color: Theme.panel; radius: Theme.radiusLg; border.color: Theme.border }
        contentItem: ColumnLayout {
            spacing: Theme.spaceSm
            Controls.AppScrollView {
                id: confirmScroll
                Layout.fillWidth: true
                Layout.preferredHeight: 180
                clip: true; contentWidth: availableWidth
                Controls.AppTextArea {
                    id: confirmText; objectName: "missionDeliveryConfirmText"
                    text: root.confirmDetails; textFormat: TextEdit.PlainText
                    wrapMode: TextEdit.WrapAnywhere; readOnly: true; selectByMouse: true
                }
            }
            Controls.ActionButton {
                text: "复制详情"; flat: true
                onClicked: { confirmText.selectAll(); confirmText.copy(); confirmText.deselect() }
            }
        }
        footer: DialogButtonBox {
            Controls.ActionButton { text: "返回"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            Controls.ActionButton { objectName: "missionDeliveryDialogConfirmButton"; enabled: root.mission.delivery.canConfirm; text: root.recovering ? "确认" + root.outcomeText(root.preview.outcome) : "确认交付"; danger: root.mission.delivery.previewMode === "abandon" || root.mission.delivery.previewMode === "squash"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: root.mission.delivery.confirm() }
        }
    }
}
