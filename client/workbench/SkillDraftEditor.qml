pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "WorkflowTheme.js" as Theme
import "WorkflowUi.js" as Flow

ColumnLayout {
    id: editor
    required property var draft
    readonly property var attempt: draft.latest_attempt || ({})
    readonly property var suggestion: attempt.suggestion || null
    readonly property var sections: [
        ["phenomenon", "适用现象"], ["priority_checks", "先检查什么"],
        ["decision_branches", "按结果怎样分支"], ["stop_conditions", "何时停止"],
        ["boundaries", "适用边界"], ["conclusion", "结论"]
    ]
    property var saved: ({})
    property var editedLesson: null
    readonly property bool hasEditedLesson: !!editedLesson
    readonly property var edits: ({
        name: skillName.text.trim(),
        developer_note: observation.text.trim(),
        pending_inference: inference.text.trim(),
        content: Object.assign({}, saved.content || {}, {
            triggers: lines(triggers.text), boundaries: lines(boundaries.text),
            body: body.text.trim(), stages: (impactStage.checked ? ["impact_test_analysis"] : [])
                .concat(rootStage.checked ? ["root_cause_analysis"] : []),
            usage_scope: policyScope.checked ? "allowed_by_policy" : "local_only"
        }),
        edited_lesson: editedLesson
    })
    property string savedFields: ""
    readonly property bool dirty: JSON.stringify(edits) !== savedFields
    readonly property bool complete: !!edits.name && !!edits.content.body && edits.content.stages.length > 0
        && edits.content.boundaries.length > 0 && (edits.content.triggers.length > 0 || (edits.content.keyword_groups || []).length > 0)
    signal locate(var reference)
    spacing: Theme.spaceSm

    function lines(text) {
        return text.split("\n").map(function(line) { return line.trim() }).filter(Boolean)
    }
    function fill(data) {
        saved = data
        var c = data.content || {}
        skillName.text = data.name || ""
        observation.text = data.developer_note || ""
        inference.text = data.pending_inference || ""
        body.text = c.body || ""
        triggers.text = (c.triggers || []).join("\n")
        boundaries.text = (c.boundaries || []).join("\n")
        var stages = c.stages || []
        rootStage.checked = stages.indexOf("root_cause_analysis") >= 0
        impactStage.checked = stages.indexOf("impact_test_analysis") >= 0
        policyScope.checked = c.usage_scope === "allowed_by_policy"
        editedLesson = data.edited_lesson || null
    }
    function load(data) {
        fill(data)
        savedFields = JSON.stringify(edits)
    }
    function merge(remote) {
        // Content is a full replacement on PATCH. Start with the server copy and
        // overlay only fields actually changed since the editor was opened.
        var local = JSON.parse(JSON.stringify(edits))
        var original = JSON.parse(savedFields)
        load(remote)
        var merged = JSON.parse(JSON.stringify(edits))
        for (var field of ["name", "developer_note", "pending_inference", "edited_lesson"])
            if (JSON.stringify(local[field]) !== JSON.stringify(original[field])) merged[field] = local[field]
        for (var key of Object.keys(local.content))
            if (JSON.stringify(local.content[key]) !== JSON.stringify(original.content[key])) merged.content[key] = local.content[key]
        fill(merged)
    }
    function basisLabel(type) {
        return ({source_context:"来源记录", developer_observation:"开发者观察",
                 pending_inference:"待确认", verified_result:"已验证的记录值"})[type] || "来源记录"
    }
    function lessonText(lesson) {
        return sections.map(function(section) { return section[1] + "：" + lesson[section[0]] }).join("\n")
    }
    function copySuggestion() {
        var copy = {attempt_id: attempt.attempt_id, basis_note: ""}
        for (var section of sections) copy[section[0]] = suggestion[section[0]]
        editedLesson = copy
    }
    component Caption: Label {
        Layout.fillWidth: true; color: Theme.secondary; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
    }
    component Heading: Caption {
        Layout.topMargin: Theme.spaceMd; color: Theme.text; font.bold: true
    }
    component Input: Controls.AppTextArea {
        Layout.fillWidth: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText
        readOnly: !!editor.draft.result_version
    }
    component ReferenceButton: Controls.ActionButton {
        required property var reference
        Layout.fillWidth: true; flat: true; objectName: "skillDraftEvidence"
        text: editor.basisLabel(reference.classification) + " · " + reference.record_id
              + (reference.locator || "") + (reference.recorded_value ? " = " + reference.recorded_value : "")
        onClicked: editor.locate(reference)
    }

    Heading { text: "模型建议"; visible: !!editor.suggestion }
    Caption {
        objectName: "skillDraftSuggestion"; visible: !!editor.suggestion
        text: editor.suggestion ? editor.lessonText(editor.suggestion) : ""
    }
    Repeater {
        model: editor.suggestion ? editor.suggestion.basis : []
        delegate: ColumnLayout {
            id: suggestionBasis
            required property var modelData
            Layout.fillWidth: true
            ReferenceButton { visible: !!suggestionBasis.modelData.reference; reference: suggestionBasis.modelData.reference || ({}) }
            Caption { visible: !suggestionBasis.modelData.reference; text: "待确认 · 此判断缺少可定位的依据" }
        }
    }
    Controls.ActionButton {
        visible: !!editor.suggestion && (!editor.editedLesson || editor.editedLesson.attempt_id !== editor.attempt.attempt_id)
                 && !editor.draft.result_version
        text: "复制模型建议到开发者编辑"; onClicked: editor.copySuggestion()
    }
    Heading { visible: !!editor.editedLesson; text: "开发者编辑 · 建议副本" }
    Repeater {
        model: editor.hasEditedLesson ? editor.sections.concat([["basis_note", "对依据的判断"]]) : []
        delegate: ColumnLayout {
            id: sectionField
            required property var modelData
            Layout.fillWidth: true
            Caption { text: sectionField.modelData[1] }
            Controls.AppTextField {
                Layout.fillWidth: true; maximumLength: sectionField.modelData[0] === "basis_note" ? 1024 : 512
                readOnly: !!editor.draft.result_version
                text: editor.editedLesson ? editor.editedLesson[sectionField.modelData[0]] || "" : ""
                onTextEdited: {
                    var copy = Object.assign({}, editor.editedLesson)
                    copy[sectionField.modelData[0]] = text
                    editor.editedLesson = copy
                }
            }
        }
    }
    Controls.ActionButton {
        visible: !!editor.editedLesson && !editor.draft.result_version; text: "将编辑后的建议填入收录字段"
        onClicked: {
            var lesson = editor.editedLesson
            triggers.text = lesson.phenomenon
            boundaries.text = lesson.boundaries
            body.text = [lesson.priority_checks, lesson.decision_branches, lesson.stop_conditions, lesson.conclusion].join("\n")
        }
    }

    Heading { text: "本机依据"; visible: (editor.draft.data?.evidence_refs || []).length > 0 }
    Repeater {
        model: editor.draft.data?.evidence_refs || []
        delegate: ReferenceButton { required property var modelData; reference: modelData }
    }
    Heading { text: "开发者编辑 · 私有手记" }
    Caption { text: "开发者观察" }
    Input { id: observation; objectName: "skillDraftObservation" }
    Caption { text: "待确认的推断" }
    Input { id: inference; objectName: "skillDraftInference" }
    Heading { text: "收录内容" }
    Caption { text: "技能名称" }
    Controls.AppTextField { id: skillName; objectName: "skillDraftName"; Layout.fillWidth: true; maximumLength: 128; readOnly: !!editor.draft.result_version }
    Caption { text: "适用现象 · 每行一个触发短语" }
    Input { id: triggers; objectName: "skillDraftTriggers" }
    Caption { text: Flow.skillMatchDetails(editor.edits.content); visible: !!text }
    Caption { text: "正文" }
    Input {
        id: body; objectName: "skillDraftBody"
        placeholderText: "先检查什么？按结果怎样分支？何时停止？"
    }
    Caption { text: "适用边界 · 每行一项" }
    Input { id: boundaries; objectName: "skillDraftBoundaries" }
    Caption { text: "适用阶段" }
    RowLayout {
        enabled: !editor.draft.result_version
        CheckBox { id: rootStage; objectName: "skillDraftRootStage"; text: "根因分析" }
        CheckBox { id: impactStage; objectName: "skillDraftImpactStage"; text: "影响与测试分析" }
    }
    CheckBox { id: policyScope; enabled: !editor.draft.result_version; text: "允许在任务数据策略许可的模型上复用（默认仅限本机）" }
}
