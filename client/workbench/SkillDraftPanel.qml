pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "WorkflowTheme.js" as Theme
import "WorkflowUi.js" as Flow

Dialog {
    id: panel
    objectName: "skillDraftDialog"
    required property var mission
    readonly property var api: mission.skillDrafts
    property string view: "source"
    property var draft: ({})
    property var versionRecord: ({})
    property var identity: ({})
    property var records: []
    property var nextOffset: null
    property var conflictCopy: null
    property string failure: ""
    property string notice: ""
    property bool unsupported: false
    property bool conflict: false
    property string operation: ""
    property double serial: 0
    property string retryKey: ""
    property string retryToken: ""
    property string sourceTask: ""
    property string sourceBackend: ""
    readonly property bool busy: serial > 0
    readonly property bool dirty: view === "editor" && editor.dirty
    readonly property string generation: draft.data?.generation_status || ""
    readonly property var generationLabels: ({
        manual: "手动草稿", generating: "模型正在起草…", ready: "模型建议已就绪",
        failed: "起草失败", interrupted: "起草中断", cancelled: "起草已取消", timed_out: "起草超时"
    })
    modal: true; anchors.centerIn: Overlay.overlay
    closePolicy: dirty ? Popup.NoAutoClose : Popup.CloseOnEscape
    title: view === "source" ? "提炼为技能" : view === "editor" ? "编辑技能草稿"
           : view === "version" ? "已收录版本" : view === "revisions" ? "找回修订草稿" : "本机技能库"
    background: Rectangle { color: Theme.panel; radius: Theme.radiusLg; border.color: Theme.border }

    function clearSession() {
        serial = 0; operation = ""; retryKey = ""; retryToken = ""
        draft = ({}); versionRecord = ({}); identity = ({}); records = []; nextOffset = null
        failure = ""; notice = ""; conflict = false; conflictCopy = null; unsupported = false
        note.text = ""; evidenceDialog.close(); discardDialog.close()
    }
    function showSource() {
        clearSession(); sourceTask = mission.taskId; sourceBackend = mission.baseUrl.toString()
        view = "source"; open(); loadPage(0)
    }
    function showLibrary() {
        clearSession(); sourceTask = mission.taskId; sourceBackend = mission.baseUrl.toString()
        view = "library"; open(); loadPage(0)
    }
    function send(action, request) {
        failure = ""; notice = ""; operation = action; serial = request
        if (!request) failure = "未能发起请求，请刷新后重试。"
    }
    function requestId(key) {
        if (retryKey !== key) { retryKey = key; retryToken = api.newRequestId() }
        return retryToken
    }
    function loadPage(offset) {
        if (!offset) records = []
        nextOffset = null
        if (view === "library") send("listSkills", api.listSkills(offset))
        else if (view === "revisions") send("listRevisions", api.listAllDrafts(offset))
        else send("listDrafts", api.listDrafts(offset))
    }
    function create(mode) {
        send("create", api.createDraft(note.text, mode, requestId(JSON.stringify([mode, note.text]))))
    }
    function openEditor(record) {
        draft = record; view = "editor"; editor.load(record.data)
        conflict = false; conflictCopy = null
    }
    function openVersion(id, number) { send("version", api.getVersion(id, number)) }
    function gotoList(page) {
        view = page; draft = ({}); versionRecord = ({}); identity = ({})
        failure = ""; notice = ""; conflict = false; conflictCopy = null
        loadPage(0)
    }
    function receive(body) {
        var action = operation
        serial = 0; operation = ""; retryKey = ""; retryToken = ""
        if (action === "listDrafts" || action === "listSkills" || action === "listRevisions") {
            unsupported = false
            var items = action === "listSkills" ? body.skills : body.drafts
            if (action === "listRevisions")
                items = items.filter(function(item) { return !!item.base_skill_id && !item.result_version })
            records = records.concat(items); nextOffset = body.next_offset
        } else if (action === "identity") {
            var current = body.skills.find(function(item) { return item.skill_id === panel.versionRecord.skill_id })
            if (current) identity = current
            else if (body.next_offset != null) send("identity", api.listSkills(body.next_offset))
        } else if (action === "compare") {
            conflictCopy = body
        } else if (action === "event") {
            evidenceText.text = [sourceTask, body.event_id, JSON.stringify(body, null, 2)].join("\n\n")
            evidenceDialog.open()
        } else if (action === "poll" || action === "regenerate") {
            // A generation attempt updates the suggestion independently of developer edits.
            if (draft.revision !== body.revision && editor.dirty) {
                conflict = true; conflictCopy = body
                failure = "草稿修订冲突。请核对最新版本，当前编辑已保留。"
            } else if (editor.dirty) draft = body
            else openEditor(body)
        } else if (action === "version" || action === "publish") {
            versionRecord = body; view = "version"
            if (action === "publish" || identity.skill_id !== body.skill_id) {
                // The version receipt has no identity revision. Fetch the library
                // record before enabling a subsequent revision or disable action.
                identity = ({})
                send("identity", api.listSkills(0))
            }
            if (action === "publish") notice = "已加入本机技能库"
        } else if (action === "disable") {
            identity = body; notice = "已停用"
        } else {
            openEditor(body)
            if (action === "save") notice = "草稿已保存"
        }
    }
    function staleContext() {
        return sourceTask !== mission.taskId || sourceBackend !== mission.baseUrl.toString()
    }
    Connections {
        target: panel.mission
        function onMissionChanged() { if (panel.visible && panel.staleContext()) panel.close() }
        function onBaseUrlChanged() { if (panel.visible) panel.close() }
    }
    Connections {
        target: panel.api
        function onSucceeded(serial, body) {
            if (serial === panel.serial && panel.visible && !panel.staleContext()) panel.receive(body)
        }
        function onFailed(serial, status, code, message) {
            if (serial !== panel.serial || !panel.visible || panel.staleContext()) return
            panel.serial = 0; panel.operation = ""
            panel.unsupported = code === "skill_api_unavailable"
            if (code === "skill_revision_conflict") panel.conflict = true
            panel.failure = panel.unsupported ? "当前后端不支持技能库。仍可直接创建任务。"
                : code === "skill_revision_conflict" ? "修订冲突。请核对最新版本，当前编辑已保留。"
                : (message || "技能请求失败") + "（" + code + "）"
        }
    }
    onClosed: clearSession()
    Timer {
        interval: 900; repeat: true
        running: panel.visible && panel.view === "editor" && !panel.busy && !panel.conflict
                 && !panel.failure && panel.generation === "generating"
        onTriggered: panel.send("poll", panel.api.getDraft(panel.draft.draft_id))
    }
    onViewChanged: if (scroll && scroll.contentItem) scroll.contentItem.contentY = 0

    function showEvidence(ref) {
        var record
        if (draft.source_mission_id === mission.taskId && ref.kind === "event") {
            send("event", api.getSourceEvent(ref.record_id))
            return
        } else if (draft.source_mission_id === mission.taskId && ref.kind === "artifact") {
            record = Flow.list(mission.artifacts).find(function(item) {
                return Flow.artifactId(item) === ref.record_id
                    && (item.content_sha256 || item.contentHash) === ref.content_sha256
            })
        } else if (draft.source_mission_id === mission.taskId && ref.kind === "node") {
            record = Flow.nodes(mission.graph).find(function(item) { return Flow.idOf(item) === ref.record_id })
        }
        var value = ref.kind === "artifact" ? record?.content : record
        if (value && ref.locator) {
            for (var key of ref.locator.slice(1).split("/"))
                value = value?.[key.replace(/~1/g, "/").replace(/~0/g, "~")]
        }
        evidenceText.text = [draft.source_mission_id, ref.record_id + (ref.locator || ""),
            value === undefined ? "此记录当前不可读取。" : typeof value === "string" ? value : JSON.stringify(value, null, 2)].join("\n\n")
        evidenceDialog.open()
    }
    component Caption: Label {
        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; color: Theme.secondary; textFormat: Text.PlainText
    }
    contentItem: ColumnLayout {
        Caption { objectName: "skillDraftError"; visible: !!text; text: panel.failure; color: Theme.warning }
        Caption { visible: !!text; text: panel.notice; color: Theme.text }
        Controls.AppScrollView {
            id: scroll
            Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: availableWidth; clip: true
            ColumnLayout {
                width: scroll.availableWidth; spacing: Theme.spaceSm
                ColumnLayout {
                    Layout.fillWidth: true; visible: panel.view === "source"
                    Caption { text: "下次遇到什么情况，你希望 Agent 更早做什么？" }
                    Controls.AppTextArea {
                        id: note; objectName: "skillDraftNote"; Layout.fillWidth: true
                        readOnly: panel.busy && panel.operation === "create"
                        wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; placeholderText: "可选备注"
                    }
                    RowLayout {
                        enabled: (!panel.busy || panel.operation === "listDrafts") && !panel.unsupported
                        Controls.ActionButton { objectName: "skillDraftCreateModel"; text: "请模型起草"; onClicked: panel.create("model") }
                        Controls.ActionButton { objectName: "skillDraftCreateManual"; text: "创建手动草稿"; onClicked: panel.create("manual") }
                    }
                    Caption { visible: panel.busy && panel.operation === "create"; text: "正在创建草稿…" }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    visible: ["library", "source", "revisions"].indexOf(panel.view) >= 0
                    RowLayout {
                        Caption { text: panel.view === "source" ? "这项任务的草稿" : panel.view === "library" ? "已收录的技能" : "待审核的修订"; font.bold: true }
                        Controls.ActionButton { text: "刷新"; flat: true; enabled: !panel.busy; onClicked: panel.loadPage(0) }
                        Controls.ActionButton {
                            visible: panel.view === "library"; enabled: !panel.busy
                            text: "找回修订草稿"; onClicked: panel.gotoList("revisions")
                        }
                    }
                    Caption { visible: !panel.records.length && !panel.failure; text: panel.busy ? "正在读取…" : "暂无记录" }
                    Repeater {
                        model: panel.records
                        delegate: Controls.ActionButton {
                            required property var modelData
                            objectName: "skillDraftListItem"
                            Layout.fillWidth: true; flat: true; enabled: !panel.busy
                            text: panel.view === "library" ? modelData.name + " · v" + modelData.current_version + (modelData.enabled ? "" : " · 已停用")
                                : (modelData.data?.name || modelData.data?.developer_note || "未命名草稿").slice(0, 64)
                                  + " · " + (modelData.result_version ? "已收录 v" + modelData.result_version : panel.generationLabels[modelData.data?.generation_status] || "")
                            onClicked: {
                                if (panel.view === "library") {
                                    panel.identity = modelData; panel.openVersion(modelData.skill_id, modelData.current_version)
                                } else panel.send("open", panel.api.getDraft(modelData.draft_id))
                            }
                        }
                    }
                    Controls.ActionButton {
                        visible: panel.nextOffset != null; enabled: !panel.busy; text: "加载更多"
                        onClicked: panel.loadPage(panel.nextOffset)
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; visible: panel.view === "editor"
                    RowLayout {
                        Caption {
                            objectName: "skillDraftStatus"
                            text: (panel.generationLabels[panel.generation] || "")
                                  + (panel.draft.latest_attempt?.error_code ? " · " + panel.draft.latest_attempt.error_code : "")
                        }
                        Caption { objectName: "skillDraftRevision"; text: "修订 " + (panel.draft.revision || "") }
                        Controls.ActionButton {
                            visible: !!panel.draft.source_mission_id && panel.generation !== "generating" && !panel.draft.result_version
                            enabled: !panel.busy && !panel.conflict
                            objectName: "skillDraftRetry"; text: "重新起草"
                            onClicked: panel.send("regenerate", panel.api.regenerate(panel.draft.draft_id, panel.draft.revision,
                                      panel.requestId("retry/" + panel.draft.draft_id + "/" + panel.draft.revision)))
                        }
                    }
                    Controls.ActionButton {
                        visible: !!panel.failure && !panel.unsupported
                        enabled: !panel.busy
                        text: panel.conflict ? "读取最新版本以核对" : "刷新草稿状态"
                        onClicked: panel.send(panel.conflict ? "compare" : "poll", panel.api.getDraft(panel.draft.draft_id))
                    }
                    Caption {
                        visible: !!panel.conflictCopy
                        text: panel.conflictCopy ? "服务器修订 " + panel.conflictCopy.revision + "\n"
                            + JSON.stringify(panel.conflictCopy.data, null, 2) : ""
                    }
                    Controls.ActionButton {
                        visible: !!panel.conflictCopy && !panel.conflictCopy?.result_version
                        text: "保留本次编辑，合并最新修订"
                        onClicked: {
                            editor.merge(panel.conflictCopy.data)
                            panel.draft = panel.conflictCopy; panel.conflict = false; panel.conflictCopy = null
                            panel.failure = ""; panel.notice = "请核对合并后的字段，再保存草稿。"
                        }
                    }
                    SkillDraftEditor {
                        id: editor; Layout.fillWidth: true; draft: panel.draft
                        selectableStages: panel.mission.selectableSkillStages
                        enabled: !(panel.busy && panel.operation !== "poll")
                        onLocate: function(reference) { panel.showEvidence(reference) }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; visible: panel.view === "version"
                    Caption {
                        objectName: "skillDraftPublishedVersion"; font.bold: true; color: Theme.text
                        text: (panel.versionRecord.name || "") + " · v" + (panel.versionRecord.version || "")
                    }
                    Caption { text: "SHA-256  " + (panel.versionRecord.content_sha256 || "") }
                    Caption { text: (panel.versionRecord.content?.triggers || []).join("、") }
                    Caption { text: Flow.skillMatchDetails(panel.versionRecord.content || {}); visible: !!text }
                    Caption { text: panel.versionRecord.content?.body || "" }
                    Caption { text: "适用边界 · " + (panel.versionRecord.content?.boundaries || []).join("、") }
                    Caption { text: "适用阶段 · " + (panel.versionRecord.content?.stages || []).map(function(id) { return Flow.nodeTitle({node_id:id}) }).join("、") }
                    Caption { text: panel.versionRecord.content?.usage_scope === "allowed_by_policy" ? "复用范围 · 遵循任务数据策略" : "复用范围 · 仅本机" }
                    RowLayout {
                        enabled: !panel.busy
                        Controls.ActionButton {
                            visible: panel.versionRecord.version > 1; text: "上一版本"
                            onClicked: panel.openVersion(panel.versionRecord.skill_id, panel.versionRecord.version - 1)
                        }
                        Controls.ActionButton {
                            visible: panel.versionRecord.version < panel.identity.current_version; text: "下一版本"
                            onClicked: panel.openVersion(panel.versionRecord.skill_id, panel.versionRecord.version + 1)
                        }
                        Controls.ActionButton {
                            objectName: "skillDraftRevise"; text: "创建修订草稿"
                            enabled: !!panel.identity.enabled && panel.versionRecord.version === panel.identity.current_version
                            onClicked: panel.send("revise", panel.api.revise(panel.versionRecord.skill_id, panel.versionRecord.version,
                                      panel.requestId("revise/" + panel.versionRecord.skill_id + "/" + panel.versionRecord.version)))
                        }
                        Controls.ActionButton {
                            visible: !!panel.identity.revision; enabled: !!panel.identity.enabled
                            text: panel.identity.enabled ? "停用技能" : "已停用"
                            onClicked: panel.send("disable", panel.api.disable(panel.identity.skill_id, panel.identity.revision))
                        }
                    }
                }
            }
        }
    }
    footer: RowLayout {
        Controls.ActionButton {
            visible: panel.view === "editor" && !!panel.draft.result_version; text: "查看已收录版本"; enabled: !panel.busy
            onClicked: panel.openVersion(panel.draft.result_skill_id, panel.draft.result_version)
        }
        Controls.ActionButton {
            objectName: "skillDraftSave"; text: "保存草稿"; visible: panel.view === "editor" && !panel.draft.result_version
            enabled: panel.dirty && !panel.busy && !panel.conflict
            onClicked: panel.send("save", panel.api.saveDraft(panel.draft.draft_id, panel.draft.revision, editor.edits))
        }
        Controls.ActionButton {
            objectName: "skillDraftPublish"; text: "加入本机技能库"; visible: panel.view === "editor" && !panel.draft.result_version
            enabled: !panel.dirty && !panel.busy && !panel.conflict && editor.complete
            onClicked: panel.send("publish", panel.api.publish(panel.draft.draft_id, panel.draft.revision,
                        panel.requestId("publish/" + panel.draft.draft_id + "/" + panel.draft.revision)))
        }
        Controls.ActionButton {
            visible: panel.view === "version" || panel.view === "revisions"; text: "返回技能库"; enabled: !panel.busy
            onClicked: panel.gotoList("library")
        }
        Item { Layout.fillWidth: true }
        Controls.ActionButton { text: "关闭"; onClicked: panel.dirty ? discardDialog.open() : panel.close() }
    }
    Dialog {
        id: discardDialog; modal: true; title: "放弃尚未保存的编辑？"; anchors.centerIn: Overlay.overlay
        footer: DialogButtonBox {
            Controls.ActionButton { text: "继续编辑"; onClicked: discardDialog.close() }
            Controls.ActionButton { text: "放弃编辑"; onClicked: { discardDialog.close(); panel.close() } }
        }
    }
    Dialog {
        id: evidenceDialog; objectName: "skillDraftEvidenceDialog"; modal: true; title: "本机来源记录"; anchors.centerIn: Overlay.overlay
        width: panel.width * 0.9; height: panel.height * 0.65
        contentItem: Controls.AppScrollView {
            clip: true
            Controls.AppTextArea { id: evidenceText; objectName: "skillDraftSourceRecord"; readOnly: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText }
        }
        standardButtons: Dialog.Close
    }
}
