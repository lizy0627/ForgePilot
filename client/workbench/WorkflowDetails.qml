pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "WorkflowTheme.js" as Theme
import "WorkbenchUi.js" as W
import "WorkflowUi.js" as Flow

Item {
    id: root
    objectName: "workflowDetails"
    required property var mission
    property var node: ({})
    property var artifacts: []
    property bool compact: false
    readonly property string nodeId: node.nodeId || node.node_id || ""
    readonly property var result: node.result || ({})
    readonly property var visual: Flow.visualStatus(node, artifacts)
    readonly property string status: visual.status
    readonly property bool approvalNode: nodeId === "execution_approval"
    readonly property bool deliveryNode: nodeId === "delivery_bundle" || node.nodeType === "delivery"
    readonly property bool pausedWriter: mission.status === "awaiting_human" && node.status === "awaiting_human"
        && (nodeId === "implementer" || /^repair_\d+$/.test(nodeId))
    signal closeRequested()
    signal cancelRequested()
    implicitWidth: 420
    implicitHeight: 640

    function focusHeader() { closeButton.forceActiveFocus(Qt.OtherFocusReason) }

    function hasAction(action) { return (node.actions || []).indexOf(action) >= 0 }
    function text(value) {
        if (value === undefined || value === null || value === "") return "—";
        return String(value);
    }
    function list(values) { return values && values.length ? values.map(function(value) { return root.text(value) }).join("\n") : "无" }
    function flag(value) { return value ? "是" : "否" }
    function artifactType(artifact) { return artifact.artifactType || artifact.artifact_type || "" }
    function artifactContent(type) {
        for (var i = 0; i < artifacts.length; ++i)
            if (artifactType(artifacts[i]) === type) return artifacts[i].content || {};
        return {};
    }
    function knownArtifactTitle(type) {
        var titles = {TaskSpec:"任务说明", EvidenceBundle:"分析证据", ExecutionPlan:"执行计划", ChangeSet:"代码修改",
            ValidationReport:"构建与测试", ReviewReport:"独立审查", DeliveryBundle:"交付结果"};
        return titles.hasOwnProperty(type) ? titles[type] : "";
    }
    function sections(artifact) {
        var data = artifact.content || {}, entries = [];
        function add(title, value) { if (value) entries.push({title:title, value:String(value)}) }
        switch (artifactType(artifact)) {
        case "TaskSpec":
            add("目标", (data.title || "") + "\n" + (data.objective || ""));
            var problem = data.problem || {};
            add("问题与预期", "当前行为：" + text(problem.observed_behavior) + "\n预期行为：" + text(problem.expected_behavior)
                + "\n复现步骤：\n" + list(problem.reproduction_steps));
            add("验收标准", (data.acceptance_criteria || []).map(function(item) {
                return item.id + " · " + item.description + "\n验证方式：" + item.verification;
            }).join("\n\n"));
            var scope = data.allowed_scope || {}, policy = data.test_policy || {}, privacy = data.data_policy || {};
            add("修改范围与约束", "允许范围：\n" + list(scope.path_patterns) + "\n最多修改：" + text(scope.max_changed_files)
                + " 个文件\n禁止范围：\n" + list(data.forbidden_scope) + "\n既有测试只读：" + flag(policy.existing_tests_read_only)
                + "\n允许新增测试：" + flag(policy.allow_new_tests) + "\n允许修改的既有测试：\n" + list(policy.modifiable_existing_test_patterns)
                + "\n允许云端模型：" + flag(privacy.cloud_model_allowed) + "\n允许外部工具：" + flag(privacy.external_mcp_allowed));
            add("假设与风险", "风险级别：" + text(data.risk_level) + "\n假设：\n" + list(data.assumptions) + "\n风险：\n" + list(data.risks));
            break;
        case "EvidenceBundle":
            add("分析结论", data.summary);
            break;
        case "ExecutionPlan":
            add("计划摘要", data.summary);
            add("执行步骤", (data.steps || []).map(function(step) {
                return step.step_id + " · " + step.action + "\n负责：" + step.owner + "\n依赖：" + list(step.depends_on)
                    + "\n预计文件：\n" + list(step.expected_files) + "\n验证：" + list(step.verification)
                    + "\n对应验收：" + list(step.acceptance_criteria_ids) + "\n新增测试：" + flag(step.adds_tests) + "\n风险：" + step.risk;
            }).join("\n\n"));
            break;
        case "ChangeSet":
            add("修改摘要", data.summary);
            if (data.requires_additional_approval) add("范围变化", "这是该次执行暂停时保存的部分改动。");
            break;
        case "ValidationReport":
            add("验证结论", text(data.summary) + "\n结果：" + text(data.status) + "\n测试通过：" + (data.tests_passed || 0)
                + " · 失败：" + (data.tests_failed || 0) + "\n受保护测试：" + text(data.hidden_oracle)
                + "\n测试完整性：" + text(data.test_integrity) + "\n工作区完整性：" + text(data.workspace_integrity)
                + "\n修改范围：" + text(data.diff_scope) + "\n失败分类：" + text(data.failure_classification));
            if ((data.compile_errors || []).length) add("编译错误", list(data.compile_errors));
            break;
        case "ReviewReport":
            add("审查结论", text(data.summary) + "\n结论：" + text(data.verdict) + "\n建议交付：" + flag(data.recommend_delivery));
            add("验收核对", (data.acceptance_criteria || []).map(function(item) {
                return item.criterion_id + " · " + item.status + "\n" + item.evidence;
            }).join("\n\n"));
            if ((data.blocking_issues || []).length) add("阻塞问题", data.blocking_issues.map(function(issue) {
                return issue.issue_id + " · " + issue.description + (issue.path ? "\n路径：" + issue.path : "");
            }).join("\n\n"));
            if ((data.non_blocking_suggestions || []).length) add("改进建议", list(data.non_blocking_suggestions));
            add("测试审查", data.test_review);
            add("安全审查", data.security_review);
            break;
        case "DeliveryBundle":
            add("交付内容", "验证：" + text(data.build_test_summary) + "\n审查：" + text(data.reviewer_conclusion)
                + "\n修改文件：\n" + list(data.changed_files) + "\n补丁路径：" + text(data.patch_path)
                + "\n残余风险：\n" + list(data.risks) + "\n回退：" + text(data.rollback));
            break;
        }
        return entries;
    }

    onNodeIdChanged: {
        if (detailsScroll && detailsScroll.contentItem) detailsScroll.contentItem.contentY = 0;
    }

    component DetailTextArea: TextArea {
        id: textControl
        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeMd
        color: Theme.text; selectionColor: Theme.selection; selectedTextColor: Theme.text
        leftPadding: Theme.spaceMd; rightPadding: Theme.spaceMd
        topPadding: Theme.spaceSm; bottomPadding: Theme.spaceSm
        background: Rectangle {
            implicitHeight: 72; radius: Theme.radiusMd; color: Theme.input
            border.width: textControl.activeFocus ? 2 : 1
            border.color: textControl.activeFocus ? Theme.focusRing : Theme.border
        }
    }

    component DetailSelector: ComboBox {
        id: selector
        hoverEnabled: true
        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSm
        leftPadding: Theme.spaceSm; rightPadding: Theme.spaceLg * 2
        topPadding: Theme.spaceSm; bottomPadding: Theme.spaceSm
        contentItem: Label {
            text: selector.displayText; color: Theme.text; font: selector.font
            elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
        }
        indicator: Label {
            anchors.right: parent.right; anchors.rightMargin: Theme.spaceSm; anchors.verticalCenter: parent.verticalCenter
            text: "▾"; color: Theme.secondary
        }
        background: Rectangle {
            implicitWidth: 180; implicitHeight: Theme.controlHeight; radius: Theme.radiusMd; color: Theme.input
            border.width: selector.activeFocus ? 2 : 1
            border.color: selector.activeFocus ? Theme.focusRing : selector.hovered ? Theme.accentBorder : Theme.border
        }
        delegate: ItemDelegate {
            id: option
            required property int index
            width: selector.width - Theme.spaceXs * 2
            text: selector.textAt(index); highlighted: selector.highlightedIndex === index
            contentItem: Label { text: option.text; color: Theme.text; font: selector.font; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
            background: Rectangle { color: option.highlighted ? Theme.accentSubtle : option.hovered ? Theme.hover : Theme.raised; radius: Theme.radiusSm }
            onClicked: { selector.currentIndex = index; selector.activated(index); selector.popup.close() }
        }
        popup: Popup {
            y: selector.height + Theme.spaceXs; width: selector.width; padding: Theme.spaceXs
            implicitHeight: Math.min(280, contentItem.implicitHeight + topPadding + bottomPadding)
            contentItem: ListView {
                implicitHeight: contentHeight; clip: true; model: selector.delegateModel; currentIndex: selector.highlightedIndex
                ScrollBar.vertical: Controls.AppScrollBar {}
            }
            background: Rectangle { color: Theme.raised; border.color: Theme.accentBorder; radius: Theme.radiusMd }
        }
    }

    component ReadableBlock: ColumnLayout {
        id: block
        property string heading: ""
        property string value: ""
        property bool monospace: false
        property int maximumHeight: 220
        spacing: Theme.spaceXs
        RowLayout {
            Layout.fillWidth: true
            Label { text: block.heading; Layout.fillWidth: true; Layout.minimumWidth: 0; wrapMode: Text.WrapAnywhere; color: Theme.secondary; font.weight: Font.DemiBold; textFormat: Text.PlainText }
            Controls.ActionButton {
                text: "复制"; flat: true; enabled: !!block.value
                Accessible.name: "复制" + block.heading
                onClicked: { readableText.selectAll(); readableText.copy(); readableText.deselect() }
            }
        }
        Controls.AppScrollView {
            id: textScroll
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(block.maximumHeight, Math.max(56, readableText.implicitHeight))
            clip: true; contentWidth: availableWidth
            DetailTextArea {
                id: readableText
                objectName: "workflowReadableText"
                width: textScroll.availableWidth
                text: block.value; readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText
                wrapMode: TextEdit.WrapAnywhere; font.family: block.monospace ? Theme.fontMono : Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
                Accessible.name: block.heading
            }
        }
    }

    component PagedText: ColumnLayout {
        id: paged
        property string value: ""
        property string heading: "输出"
        property int offset: 0
        readonly property int pageSize: 12000
        onValueChanged: offset = 0
        spacing: Theme.spaceXs
        Label { text: paged.heading; color: Theme.secondary; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
        Controls.AppScrollView {
            id: outputScroll
            Layout.fillWidth: true; Layout.preferredHeight: 280; clip: true
            DetailTextArea {
                id: pagedOutput; objectName: "workflowPagedOutput"
                text: paged.value.substring(paged.offset, paged.offset + paged.pageSize)
                readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText; wrapMode: TextEdit.NoWrap
                font.family: Theme.fontMono; font.pixelSize: Theme.fontSizeSm
                Accessible.name: paged.heading
            }
        }
        Label {
            Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: Theme.muted; font.pixelSize: Theme.fontSizeSm
            text: paged.value.length ? "显示 " + (paged.offset + 1) + "–" + Math.min(paged.value.length, paged.offset + paged.pageSize) + " / " + paged.value.length + " 字符" : "无输出"
        }
        RowLayout {
            Layout.fillWidth: true; spacing: Theme.spaceXs
            Controls.ActionButton { text: "上一段"; enabled: paged.offset > 0; onClicked: paged.offset = Math.max(0, paged.offset - paged.pageSize) }
            Controls.ActionButton { objectName: "workflowCopyOutput"; text: "复制本段"; enabled: !!paged.value; onClicked: { pagedOutput.selectAll(); pagedOutput.copy(); pagedOutput.deselect() } }
            Controls.ActionButton { objectName: "workflowNextOutput"; text: "下一段"; enabled: paged.offset + paged.pageSize < paged.value.length; onClicked: paged.offset += paged.pageSize }
        }
    }

    component ScopeDecisionButton: Controls.ActionButton {
        required property bool approve
        required property var proposal
        property var pressedProposal: null
        // A refresh between press and release must not authorize a different revision.
        onPressed: pressedProposal = Object.assign({}, proposal)
        onCanceled: pressedProposal = null
        onClicked: {
            var reviewed = pressedProposal || proposal
            pressedProposal = null
            root.mission.approval.decideScopeExpansion(reviewed, approve)
        }
    }

    Rectangle { anchors.fill: parent; color: Theme.sidebar; border.color: Theme.border }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: Theme.spaceMd; spacing: Theme.spaceSm
        RowLayout {
            Layout.fillWidth: true
            Label {
                objectName: "workflowDetailsTitle"
                text: root.nodeId ? Flow.nodeTitle(root.node) : "节点详情"
                color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold
                textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; Layout.minimumWidth: 0
            }
            Controls.ActionButton { id: closeButton; objectName: "workflowDetailsClose"; text: root.compact ? "返回流程图" : "关闭"; flat: true; Accessible.name: "关闭节点详情，返回流程图"; onClicked: root.closeRequested() }
        }
        Controls.AppScrollView {
            id: detailsScroll
            objectName: "workflowDetailsScroll"
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0
            clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: detailsScroll.availableWidth; spacing: Theme.spaceMd
                Label {
                    visible: !!root.nodeId
                    text: root.visual.label
                        + (root.node.attempt ? " · 第 " + root.node.attempt + " 次尝试" : " · 尚未执行")
                    color: root.visual.needsRepair ? Theme.warning : W.color(root.status); font.weight: Font.DemiBold
                    Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                }
                ReadableBlock {
                    Layout.fillWidth: true; visible: !!root.nodeId
                    heading: root.result.decision_summary ? "节点摘要" : "节点职责"
                    value: root.visual.needsRepair ? root.visual.description + (root.result.decision_summary ? "\n" + root.result.decision_summary : "")
                        : root.result.decision_summary || Flow.nodeDescription(root.node)
                    maximumHeight: 150
                }
                ReadableBlock {
                    objectName: "workflowNodeError"
                    Layout.fillWidth: true; visible: !!root.result.error_summary
                    heading: "执行问题"; value: (root.result.failure_kind ? "类型：" + root.result.failure_kind + "\n" : "") + (root.result.error_summary || "")
                }
                Label {
                    visible: !!root.nodeId && !root.artifacts.length && !root.approvalNode && !root.deliveryNode && !root.pausedWriter
                    text: root.node.status === "pending" ? "节点尚未开始，完成后可在这里查看本节点的具体结果。"
                        : root.node.status === "running" ? "节点正在执行，产物生成后会自动更新。"
                        : root.node.status === "skipped" ? "当前执行路径已跳过该节点。"
                        : "本节点没有独立产物，可查看上方摘要与执行状态。"
                    color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                }
                ApprovalPanel { mission: root.mission; visible: root.approvalNode; Layout.fillWidth: true }
                Controls.Panel {
                    id: scopePanel
                    objectName: "workflowScopePanel"
                    visible: root.pausedWriter
                    Layout.fillWidth: true
                    readonly property var proposal: {
                        var p = root.mission.approval.scopeExpansion
                        return root.pausedWriter && p && p.task_id === root.mission.taskId
                            && p.node_id === root.nodeId && p.attempt === root.node.attempt ? p : ({})
                    }
                    readonly property bool structured: !!proposal.status
                    readonly property var reviewedRevision: proposal.revision
                    onReviewedRevisionChanged: {
                        if (reviewedRevision && detailsScroll.contentItem) detailsScroll.contentItem.contentY = 0
                    }
                    readonly property bool rejected: proposal.status === "rejected"
                    readonly property var proposedSpec: W.artifactById(root.mission.artifacts, proposal.task_spec_artifact_id)
                    readonly property var proposedPlan: W.artifactById(root.mission.artifacts, proposal.plan_artifact_id)
                    readonly property var originalSpec: W.artifactById(root.mission.artifacts, (proposedSpec.parentArtifactIds || [])[0])
                    readonly property var originalPlan: W.artifactById(root.mission.artifacts, (proposedPlan.parentArtifactIds || [])[1])
                    readonly property bool bundleMatches: structured && proposedSpec.contentHash === proposal.task_spec_hash
                        && proposedPlan.contentHash === proposal.plan_hash && !!originalSpec.content && !!originalPlan.content
                        && (proposedPlan.parentArtifactIds || [])[0] === proposedSpec.artifactId
                        && proposedPlan.content.task_spec_artifact_id === proposedSpec.artifactId
                        && originalPlan.content.task_spec_artifact_id === originalSpec.artifactId
                    title: structured ? (rejected ? "扩围申请已拒绝" : "文件扩围审批") : "节点暂停"
                    subtitle: structured ? "修订 " + proposal.revision + " · 第 " + proposal.attempt + " 次尝试" : ""
                    function step(id) {
                        return ((originalPlan.content || {}).steps || []).find(function(item) { return item.step_id === id }) || {}
                    }
                    Label {
                        visible: !scopePanel.structured || scopePanel.rejected || !scopePanel.bundleMatches
                        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: Theme.warning; textFormat: Text.PlainText
                        text: !root.mission.synchronized ? "正在同步当前节点…"
                            : !scopePanel.structured ? "此暂停记录缺少结构化扩围申请。可查看节点摘要、已有改动，或取消任务。"
                            : scopePanel.rejected ? "任务保持暂停。可继续查看已保存的改动，或取消任务。"
                            : "原范围或计划尚未核对，请刷新任务后重新审阅。"
                    }
                    ReadableBlock {
                        objectName: "workflowScopeOriginal"; visible: scopePanel.structured; Layout.fillWidth: true
                        heading: "原文件范围"
                        value: scopePanel.structured ? (scopePanel.originalSpec.content
                            ? root.list(scopePanel.originalSpec.content.allowed_scope.path_patterns) : "正在读取原范围…")
                            + "\n文件数量上限：" + scopePanel.proposal.previous_max_changed_files
                            + " → " + scopePanel.proposal.max_changed_files : ""
                    }
                    ReadableBlock {
                        objectName: "workflowScopeFiles"; visible: scopePanel.structured; Layout.fillWidth: true
                        heading: "申请新增的文件"
                        value: (scopePanel.proposal.files || []).map(function(file) {
                            var step = scopePanel.step(file.plan_step_id)
                            return file.path + "\n原因：" + file.reason
                                + "\n计划步骤：" + file.plan_step_id + (step.action ? " · " + step.action : "")
                                + (step.expected_files ? "\n步骤原定文件：" + root.list(step.expected_files) : "")
                                + "\n对应验收：" + root.list(file.acceptance_criteria_ids)
                        }).join("\n\n")
                    }
                    ReadableBlock {
                        objectName: "workflowScopeReplay"; visible: scopePanel.structured; Layout.fillWidth: true
                        heading: "批准后的执行方式"
                        value: scopePanel.structured ? "从进入此节点时的检查点重新执行，保留先前已完成的节点。"
                            + (scopePanel.proposal.partial_change_set_artifact_id
                                ? "\n本轮部分改动会从工作区撤回，已保存的差异仍可查看。" : "\n本轮没有已保存的部分改动。")
                            + "\n检查点：" + scopePanel.proposal.checkpoint_id : ""
                    }
                    PagedText {
                        objectName: "workflowScopeDiff"; visible: !!scopePanel.proposal.partial_change_set_artifact_id
                        Layout.fillWidth: true; heading: "本轮已保存的差异"; value: scopePanel.proposal.partial_patch || ""
                    }
                    Controls.LongText {
                        visible: scopePanel.structured; Layout.fillWidth: true; title: "后端重做说明"
                        text: scopePanel.proposal.replay_behavior || ""; monospace: false; maximumHeight: 160
                    }
                    Label {
                        visible: !!text; Layout.fillWidth: true; text: root.mission.approval.error
                        wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; color: Theme.warning
                    }
                    RowLayout {
                        visible: scopePanel.proposal.status === "pending"; Layout.fillWidth: true
                        enabled: scopePanel.bundleMatches && root.mission.approval.scopeAvailable
                        ScopeDecisionButton {
                            objectName: "workflowScopeApprove"; text: "批准扩围"; highlighted: true
                            proposal: scopePanel.proposal; approve: true
                        }
                        ScopeDecisionButton {
                            objectName: "workflowScopeReject"; text: "拒绝扩围"; danger: true
                            proposal: scopePanel.proposal; approve: false
                        }
                    }
                    Controls.ActionButton {
                        objectName: "workflowScopeCancel"; text: "取消任务"; danger: true
                        visible: scopePanel.rejected || !scopePanel.structured
                        enabled: root.mission.synchronized && !root.mission.busy
                        onClicked: root.cancelRequested()
                    }
                }
                DeliveryPanel { mission: root.mission; visible: root.deliveryNode; Layout.fillWidth: true }
                ReadableBlock {
                    visible: root.deliveryNode && !!value; Layout.fillWidth: true; heading: "使用说明"
                    value: root.artifactContent("DeliveryBundle").usage_instructions || ""
                }
                Repeater {
                    model: root.approvalNode || root.deliveryNode
                        ? root.artifacts.filter(function(item) { return !root.knownArtifactTitle(root.artifactType(item)) })
                        : root.artifacts
                    delegate: Controls.Panel {
                        id: artifactPanel
                        required property var modelData
                        readonly property var artifact: modelData
                        readonly property var artifactData: artifact.content || ({})
                        readonly property string kind: root.artifactType(artifact)
                        readonly property var files: kind === "ChangeSet" ? artifactData.files || [] : []
                        readonly property var commands: kind === "ValidationReport" ? artifactData.commands || [] : []
                        readonly property var evidence: kind === "EvidenceBundle" ? artifactData.items || [] : []
                        property int selectedFile: 0
                        property int selectedCommand: 0
                        property int selectedEvidence: 0
                        readonly property var file: files[selectedFile] || ({})
                        readonly property var command: commands[selectedCommand] || ({})
                        readonly property var evidenceItem: evidence[selectedEvidence] || ({})
                        objectName: "workflowArtifactPanel"
                        Layout.fillWidth: true
                        readonly property string knownTitle: root.knownArtifactTitle(kind)
                        title: knownTitle || kind
                        subtitle: knownTitle ? "" : "当前客户端暂不支持详情"
                        Repeater {
                            model: root.sections(artifactPanel.artifact)
                            delegate: ReadableBlock {
                                required property var modelData
                                Layout.fillWidth: true; heading: modelData.title; value: modelData.value
                            }
                        }
                        ColumnLayout {
                            visible: artifactPanel.evidence.length > 0; Layout.fillWidth: true; spacing: Theme.spaceSm
                            DetailSelector {
                                objectName: "workflowEvidenceSelect"
                                Layout.fillWidth: true; model: artifactPanel.evidence.map(function(item) { return item.evidence_id + " · " + (item.path || item.source) })
                                Accessible.name: "选择本节点证据"
                                onActivated: artifactPanel.selectedEvidence = currentIndex
                            }
                            ReadableBlock {
                                Layout.fillWidth: true; heading: "证据内容"
                                value: root.text(artifactPanel.evidenceItem.claim) + "\n来源：" + root.text(artifactPanel.evidenceItem.source)
                                    + (artifactPanel.evidenceItem.path ? "\n路径：" + artifactPanel.evidenceItem.path : "")
                                    + (artifactPanel.evidenceItem.start_line ? "\n行号：" + artifactPanel.evidenceItem.start_line + "–" + artifactPanel.evidenceItem.end_line : "")
                                    + "\n可信度：" + root.text(artifactPanel.evidenceItem.confidence) + "\n依据：" + root.text(artifactPanel.evidenceItem.reason)
                            }
                        }
                        ColumnLayout {
                            visible: artifactPanel.files.length > 0; Layout.fillWidth: true; spacing: Theme.spaceSm
                            Label { text: "本节点修改 · " + artifactPanel.files.length + " 个文件"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                            DetailSelector {
                                objectName: "workflowFileSelect"
                                Layout.fillWidth: true; model: artifactPanel.files.map(function(file) { return file.path })
                                Accessible.name: "选择本节点修改文件"
                                onActivated: artifactPanel.selectedFile = currentIndex
                            }
                            ReadableBlock {
                                Layout.fillWidth: true; heading: "文件信息"; maximumHeight: 150
                                value: root.text(artifactPanel.file.path) + "\n变更：" + root.text(artifactPanel.file.change_type)
                                    + "\n对应步骤：" + root.list(artifactPanel.file.plan_step_ids) + "\n对应验收：" + root.list(artifactPanel.file.acceptance_criteria_ids)
                            }
                            PagedText { objectName: "workflowNodeDiff"; Layout.fillWidth: true; heading: "文件差异"; value: artifactPanel.file.unified_diff || "" }
                        }
                        ColumnLayout {
                            visible: artifactPanel.commands.length > 0; Layout.fillWidth: true; spacing: Theme.spaceSm
                            DetailSelector {
                                objectName: "workflowCommandSelect"
                                Layout.fillWidth: true; model: artifactPanel.commands.map(function(command) { return (command.exit_code === 0 ? "✓ " : "! ") + command.command_summary })
                                Accessible.name: "选择本节点验证命令"
                                onActivated: artifactPanel.selectedCommand = currentIndex
                            }
                            ReadableBlock {
                                Layout.fillWidth: true; heading: "命令信息"; maximumHeight: 150
                                value: root.text(artifactPanel.command.command_summary) + "\n退出码：" + root.text(artifactPanel.command.exit_code)
                                    + "\n开始：" + root.text(artifactPanel.command.started_at) + "\n结束：" + root.text(artifactPanel.command.finished_at)
                            }
                            PagedText { objectName: "workflowNodeCommandOutput"; Layout.fillWidth: true; heading: "命令输出"; value: artifactPanel.command.output_summary || "" }
                        }
                        Controls.LongText {
                            Layout.fillWidth: true; title: "产物标识与版本"; maximumHeight: 150
                            text: "产物 ID：" + (artifactPanel.artifact.artifactId || artifactPanel.artifact.artifact_id || "—")
                                + "\n来源节点：" + (artifactPanel.artifact.producerNode || artifactPanel.artifact.producer_node || "—")
                                + "\n内容 Hash：" + (artifactPanel.artifact.contentHash || artifactPanel.artifact.content_sha256 || "—")
                        }
                    }
                }
                Controls.Panel {
                    visible: root.hasAction("resume") || root.mission.snapshot.recovery_required || !!root.mission.recovery.token
                    Layout.fillWidth: true; title: "恢复执行"
                    Label { text: "先检查恢复方案，核对当前修订与恢复动作后再确认。"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                    Controls.ActionButton { objectName: "workflowRecoveryPreview"; text: "检查恢复方案"; enabled: root.mission.synchronized && !root.mission.busy; onClicked: root.mission.prepareRecovery() }
                    ReadableBlock {
                        visible: !!root.mission.recovery.revision; Layout.fillWidth: true; heading: "恢复预览"
                        value: "当前修订：" + root.mission.recovery.revision + "\n检查：\n" + root.list(root.mission.recovery.checks)
                            + "\n动作：\n" + root.list((root.mission.recovery.actions || []).map(function(action) { return action.action || action.kind || action.type || "检查" }))
                            + "\n阻塞原因：" + (root.mission.recovery.blocked_reason || "无")
                    }
                    Controls.ActionButton {
                        objectName: "workflowRecoveryConfirm"; text: "确认恢复"; visible: !!root.mission.recovery.token
                        enabled: root.mission.synchronized && !root.mission.busy && !root.mission.recovery.blocked_reason
                            && root.mission.recovery.task_id === root.mission.taskId && root.mission.recovery.revision === root.mission.revision
                        onClicked: root.mission.confirmRecovery()
                    }
                }
                Controls.ActionButton {
                    objectName: "workflowRetryNode"; text: "重试此节点"; visible: root.hasAction("retry")
                    enabled: root.mission.synchronized && !root.mission.busy; onClicked: root.mission.retry(root.nodeId)
                }
                Controls.LongText {
                    objectName: "workflowNodeMore"; visible: !!root.nodeId; Layout.fillWidth: true
                    title: "更多节点信息"; maximumHeight: 220
                    text: "节点 ID：" + root.nodeId + "\n执行者：" + (root.node.agentId || "系统")
                        + "\n依赖：" + root.list(root.node.dependencies) + "\n尝试次数：" + (root.node.attempt || 0)
                        + "\n耗时：" + ((root.node.metrics || {}).duration_ms || 0) + " ms"
                        + "\n模型调用：" + ((root.node.metrics || {}).model_calls || 0) + "\n工具调用：" + ((root.node.metrics || {}).tool_calls || 0)
                        + "\nToken：" + ((root.node.metrics || {}).total_tokens || 0)
                }
                Item { Layout.preferredHeight: Theme.spaceSm }
            }
        }
    }
}
