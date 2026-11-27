import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme
import "RagUi.js" as RagUi

Rectangle {
    id: root
    required property var controller
    color: Theme.panel; radius: Theme.radiusMd
    ColumnLayout {
        anchors.fill: parent; anchors.margins: Theme.spaceLg; spacing: Theme.spaceSm
        RowLayout {
            Layout.fillWidth: true
            Label { text: "修改方案"; color: Theme.text; font.weight: Font.DemiBold; font.pixelSize: Theme.fontSizeLg; Layout.fillWidth: true }
            Label { text: root.controller.planVersion ? "方案 v" + root.controller.planVersion : "尚未生成"; color: Theme.muted }
        }
        Label {
            objectName: "taskRagUsage"
            visible: !!root.controller.taskId
            text: RagUi.taskUsage(root.controller.ragRequested, root.controller.ragUsed, root.controller.ragStatus)
            textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.Wrap
            color: root.controller.ragStatus === "degraded" || root.controller.ragStatus === "unavailable"
                ? Theme.warning : root.controller.ragUsed ? Theme.accent : Theme.muted
            font.pixelSize: Theme.fontSizeSm
        }
        Label {
            objectName: "feedbackPlanningNotice"
            visible: root.controller.feedbackReplanning
            Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.warning
            text: "正在根据意见重新规划，完成后需重新批准"
        }
        Label {
            objectName: "previousPlanNotice"
            visible: root.controller.feedbackShowsPreviousPlan
            text: "上一版方案，仅供查看"; color: Theme.muted; textFormat: Text.PlainText
        }
        ColumnLayout {
            visible: root.controller.feedbackOpen
            Layout.fillWidth: true; spacing: Theme.spaceSm
            Label { text: "对方案 v" + root.controller.feedbackSourceVersion + " 提出修改意见"; color: Theme.secondary }
            AppScrollView {
                Layout.fillWidth: true; Layout.preferredHeight: 110; clip: true
                AppTextArea {
                    id: feedbackInput; objectName: "planFeedbackInput"
                    text: root.controller.feedbackDraft
                    onTextChanged: root.controller.feedbackDraft = text
                    textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; selectByMouse: true
                    readOnly: root.controller.feedbackSending || root.controller.historyReadOnly
                        || root.controller.state !== "waiting_approval" || root.controller.planVersion !== root.controller.feedbackSourceVersion
                    placeholderText: "说明希望如何调整方案；提交后仍需重新审阅和批准。"
                    color: Theme.text; selectionColor: Theme.selection; selectedTextColor: Theme.text
                    TextMenu { id: feedbackMenu; editor: feedbackInput }
                    TapHandler { acceptedButtons: Qt.RightButton; onTapped: feedbackMenu.popup() }
                }
            }
            Label {
                objectName: "planFeedbackCount"
                text: root.controller.feedbackCharacterCount(feedbackInput.text) + " / 4000 字符"
                color: root.controller.feedbackValidationError(feedbackInput.text) ? Theme.warning : Theme.muted
            }
            Label {
                objectName: "planFeedbackNotice"
                visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                text: root.controller.feedbackNotice; color: Theme.warning
            }
            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.muted; textFormat: Text.PlainText
                    text: root.controller.feedbackValidationError(feedbackInput.text)
                }
                ActionButton {
                    objectName: "closePlanFeedbackButton"; text: "收起"
                    enabled: !root.controller.feedbackSending
                    onClicked: root.controller.closePlanFeedback()
                }
                ActionButton {
                    objectName: "submitPlanFeedbackButton"; text: root.controller.feedbackSending ? "正在提交…" : "提交并重新规划"
                    highlighted: true
                    enabled: root.controller.canFeedback && root.controller.feedbackSourceVersion === root.controller.planVersion
                        && !root.controller.feedbackValidationError(feedbackInput.text)
                    onClicked: root.controller.submitPlanFeedback(feedbackInput.text)
                }
            }
        }
        AppScrollView {
            id: contentScroll; objectName: "workspaceScroll"
            Layout.fillWidth: true; Layout.fillHeight: true; contentWidth: availableWidth; clip: true
            ColumnLayout {
                width: contentScroll.availableWidth; spacing: Theme.spaceLg
                Label { visible: !root.controller.planVersion; text: "分析仓库后将在这里给出修改范围和验证方案。"; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                Repeater {
                    model: root.controller.planVersion ? [
                        {label:"摘要",key:"summary"}, {label:"根因",key:"root_cause"}, {label:"修改文件",key:"files_to_modify"},
                        {label:"实施步骤",key:"implementation_steps"}, {label:"验证步骤",key:"validation_steps"}, {label:"风险",key:"risks"}] : []
                    delegate: ColumnLayout {
                        id: field
                        required property var modelData
                        Layout.fillWidth: true; spacing: Theme.spaceXs
                        Label { text: field.modelData.label; font.weight: Font.DemiBold; color: Theme.secondary; font.pixelSize: Theme.fontSizeMd }
                        TextEdit {
                            objectName: field.modelData.key === "files_to_modify" ? "planView" : "plan_" + field.modelData.key
                            Layout.fillWidth: true; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; color: Theme.text; selectionColor: Theme.selection; selectedTextColor: Theme.text
                            font.family: field.modelData.key === "files_to_modify" ? Theme.fontMono : Theme.fontFamily
                            font.pixelSize: Theme.fontSizeMd
                            text: {
                                var v = root.controller.plan[field.modelData.key];
                                // QVariantList is a QML sequence, which need not satisfy JavaScript Array.isArray.
                                if (v && typeof v !== "string" && v.length !== undefined) {
                                    var rows=[];
                                    for(var i=0;i<v.length;++i) {
                                        var item=v[i]; rows.push((i+1) + ". " + (typeof item === "string" ? item : item.path + " — " + item.reason));
                                    }
                                    return rows.join("\n");
                                }
                                return v || "—";
                            }
                        }
                    }
                }
            }
        }
        RowLayout {
            visible: root.controller.planVersion > 0
            Layout.fillWidth: true
            Label { Layout.fillWidth: true; wrapMode: Text.Wrap; text: root.controller.historyReadOnly ? "历史只读 · 不可审批" : root.controller.canApprove ? "批准后，仅可修改方案授权的文件，并执行构建与测试。" : "仅当前待审批方案可操作"; color: Theme.muted; font.pixelSize: Theme.fontSizeSm }
            ActionButton { objectName: "planFeedbackButton"; text: "提出修改意见"; visible: !root.controller.feedbackOpen; enabled: root.controller.canFeedback; onClicked: root.controller.openPlanFeedback() }
            ActionButton { objectName: "rejectButton"; text: "拒绝方案"; danger: true; enabled: root.controller.canApprove; onClicked: root.controller.rejectPlan() }
            ActionButton { objectName: "approveButton"; text: root.controller.busy ? "处理中…" : "批准并执行"; highlighted: true; enabled: root.controller.canApprove; onClicked: root.controller.approvePlan() }
        }
    }
}
