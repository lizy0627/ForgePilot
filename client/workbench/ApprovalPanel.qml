import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Controls.Panel {
    id: root
    required property var mission
    readonly property var review: mission.approval.review || ({})
    readonly property var taskArtifact: W.artifactById(mission.artifacts, review.task_spec_artifact_id)
    readonly property var planArtifact: W.artifactById(mission.artifacts, review.plan_artifact_id)
    readonly property var spec: taskArtifact.content || ({})
    readonly property var plan: planArtifact.content || ({})
    readonly property bool bundleMatches: !!review.review_hash && taskArtifact.contentHash === review.task_spec_hash && planArtifact.contentHash === review.plan_hash
    property var seenReviews: ({})
    property string changeNotice: ""
    title: "Approval · 执行审批"
    subtitle: root.review.revision ? "修订 " + root.review.revision : ""
    objectName: "approvalPanel"
    function noticeReview() {
        var id = mission.taskId;
        if (!id || !review.review_hash) return;
        var old = seenReviews[id];
        var now = {revision: review.revision, task: review.task_spec_hash, plan: review.plan_hash, scope: review.scope_hash};
        if (old && (old.revision !== now.revision || old.task !== now.task || old.plan !== now.plan || old.scope !== now.scope)) {
            changeNotice = "审批包已变化：修订 " + old.revision + " → " + now.revision
                + (old.task !== now.task ? "；TaskSpec Hash 已变化" : "")
                + (old.plan !== now.plan ? "；Plan Hash 已变化" : "")
                + (old.scope !== now.scope ? "；范围 Hash 已变化" : "") + "。请重新审阅。";
        }
        seenReviews[id] = now;
    }
    onReviewChanged: Qt.callLater(noticeReview)
    Label { visible: !!root.changeNotice; text: root.changeNotice; color: Theme.warning; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    StateNotice { mission: root.mission; hasData: !!root.review.review_hash; emptyText: "当前没有待审阅的审批包。"; Layout.fillWidth: true }
    Label {
        objectName: "missionApprovalDetails"
        visible: !!root.review.review_hash
        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text
        text: "TaskSpec：" + (root.spec.title || "待加载") + "\n目标：" + (root.spec.objective || "待加载")
            + "\nPlan：" + (root.plan.summary || "待加载")
            + "\n允许范围：" + W.lines((root.spec.allowed_scope || {}).path_patterns) + "；最多修改 " + ((root.spec.allowed_scope || {}).max_changed_files || "—") + " 个文件"
            + "\n禁止范围：" + W.lines(root.spec.forbidden_scope)
            + "\n测试策略：既有测试只读 " + ((root.spec.test_policy || {}).existing_tests_read_only ? "是" : "否")
            + "；允许新增测试 " + ((root.spec.test_policy || {}).allow_new_tests ? "是" : "否")
            + "；可改既有测试 " + W.lines((root.spec.test_policy || {}).modifiable_existing_test_patterns)
            + "\n云端数据范围：云端模型 " + ((root.spec.data_policy || {}).cloud_model_allowed ? "允许" : "禁止")
            + "；外部 MCP " + ((root.spec.data_policy || {}).external_mcp_allowed ? "允许" : "禁止")
            + "\n预计命令：CMake 配置 / 构建、CTest 测试；方案验证方式 " + (root.plan.steps || []).map(function(s) { return s.step_id + ":" + W.lines(s.verification) }).join("；")
            + "。实际命令以验证页记录为准。"
            + "\n风险：" + (root.spec.risk_level || "待加载") + "；" + W.lines(root.spec.risks)
            + "；步骤风险 " + (root.plan.steps || []).map(function(s) { return s.step_id + ":" + s.risk }).join("；")
            + "\n当前修订：" + (root.review.revision || "—")
            + "\nTaskSpec Hash：" + (root.review.task_spec_hash || "—")
            + "\nPlan Hash：" + (root.review.plan_hash || "—")
            + "\n范围 Hash：" + (root.review.scope_hash || "—")
    }
    Label { visible: !!root.review.review_hash && !root.bundleMatches; text: "审批内容仍在加载或哈希不匹配，请刷新后审阅。"; color: Theme.warning; Layout.fillWidth: true; wrapMode: Text.Wrap }
    Label { visible: !!root.mission.approval.error; text: root.mission.approval.error; color: Theme.danger; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
    RowLayout {
        visible: !!root.review.review_hash; Layout.fillWidth: true
        Controls.ActionButton { objectName: "missionApproveButton"; text: "批准当前修订"; highlighted: true; enabled: root.bundleMatches && root.mission.approval.available; onClicked: root.mission.approval.approve() }
        Controls.ActionButton { objectName: "missionRejectButton"; text: "拒绝"; danger: true; enabled: root.bundleMatches && root.mission.approval.available; onClicked: rejectDialog.open() }
    }
    Dialog {
        id: rejectDialog; title: "拒绝执行审批"; modal: true; anchors.centerIn: Overlay.overlay; width: 420; implicitWidth: 420
        contentItem: Label { wrapMode: Text.Wrap; text: "将拒绝任务 " + root.mission.taskId + " 的修订 " + root.review.revision + " 并取消本次执行。该决定不可撤回。" }
        footer: DialogButtonBox {
            Controls.ActionButton { text: "返回"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            Controls.ActionButton { text: "确认拒绝"; danger: true; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: root.mission.approval.reject() }
        }
    }
}
