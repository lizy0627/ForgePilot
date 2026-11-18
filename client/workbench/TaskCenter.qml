import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Rectangle {
    id: root
    objectName: "missionTaskCenter"
    required property var mission
    property var mcp: null
    property var rag: null
    readonly property string draftRepositoryPath: repositoryInput.text.trim()
    property bool ragRequested: rag ? rag.useForTasks : false
    property var selectedMcpIds: []
    property string selectedContext: ""
    property var baselinePaths: []
    function baselineRequest() {
        var result = {repository_path: draftRepositoryPath, base_ref: "HEAD", baseline_paths: baselinePaths}
        if (presetSelection.checked) {
            if (configurePreset.text.trim()) result.configure_preset = configurePreset.text.trim()
            if (buildPreset.text.trim()) result.build_preset = buildPreset.text.trim()
            if (testPreset.text.trim()) result.test_preset = testPreset.text.trim()
        }
        return result
    }
    function submitMission() {
        var result = baselineRequest()
        result.request = requestInput.text.trim()
        result.task_type = ["auto", "bug_fix", "feature"][kind.currentIndex]
        result.preview_token = root.mission.creationPreview.preview_token
        result.baseline_confirmed = baselineConfirmation.checked
        result.mcp_server_ids = root.selectedMcpIds
        result.use_rag = useRag.checked
        root.mission.createMission(result)
    }
    Connections {
        target: root.mission
        function onCreationChanged() { if (baselineConfirmation && !root.mission.creationReady) baselineConfirmation.checked = false }
    }
    Connections {
        target: root.mcp
        function onMissionChoicesChanged() { if (root.mcp.missionLoading || !root.mcp.missionChoices.length) root.selectedMcpIds = [] }
    }
    color: Theme.sidebar
    border.color: Theme.border
    border.width: 1
    implicitWidth: 244
    Component.onCompleted: {
        if (!mission.historyLoaded && !mission.historyLoading) mission.refreshMissions()
        if (mcp) mcp.refreshMissionChoices()
    }
    function resetBaseline() {
        baselinePaths = []
        mission.invalidateCreationPreview(true)
    }
    function fillDraft(request, repository, source) {
        requestInput.text = request
        selectedContext = source
        if (repositoryInput.text === repository) resetBaseline()
        else repositoryInput.text = repository
    }
    FolderDialog { id: folder; title: "选择任务仓库"; options: FolderDialog.DontUseNativeDialog; onAccepted: repositoryInput.text = root.mission.localPath(selectedFolder) }
    Controls.AppScrollView {
        id: sidebarScroll
        objectName: "missionTaskCenterScroll"
        anchors.fill: parent; clip: true; contentWidth: availableWidth
        contentHeight: sidebarLayout.implicitHeight + Theme.spaceMd * 2
        ColumnLayout {
        id: sidebarLayout
        x: Theme.spaceMd; y: Theme.spaceMd
        width: Math.max(0, sidebarScroll.availableWidth - Theme.spaceMd * 2)
        spacing: Theme.spaceSm
        Label { text: "任务中心"; color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold }
        Label {
            id: contextPreview
            objectName: "missionContextPreview"
            text: root.selectedContext || "创建任务仅使用下方文本与仓库路径。"
            color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
            maximumLineCount: 3; elide: Text.ElideRight; textFormat: Text.PlainText
            font.pixelSize: Theme.fontSizeSm
        }
        Controls.LongText {
            objectName: "missionContextFull"
            visible: !!root.selectedContext && (contextPreview.truncated || root.selectedContext.length > 100)
            Layout.fillWidth: true; title: "来源上下文"; text: root.selectedContext
            monospace: false; maximumHeight: 170
        }
        Controls.AppScrollView {
            objectName: "missionRequestScroll"
            Layout.fillWidth: true; Layout.preferredHeight: 94; clip: true; contentWidth: availableWidth
            Controls.AppTextArea {
                id: requestInput; objectName: "missionRequestInput"
                wrapMode: TextEdit.WrapAnywhere; placeholderText: "描述 Agent 任务"
                textFormat: TextEdit.PlainText
            }
        }
        Controls.AppTextField {
            id: repositoryInput; objectName: "missionRepositoryInput"; Layout.fillWidth: true
            placeholderText: "本地仓库绝对路径"
            onTextChanged: root.resetBaseline()
        }
        Controls.LongText {
            objectName: "missionRepositoryInputFull"
            visible: repositoryInput.text.length > 30
            Layout.fillWidth: true; title: "完整仓库路径"; text: repositoryInput.text
            maximumHeight: 150
        }
        Controls.ActionButton { text: "选择仓库…"; Layout.fillWidth: true; onClicked: folder.open() }
        Controls.AppComboBox {
            id: kind; Layout.fillWidth: true; model: ["自动判断", "修复缺陷", "实现功能"]; Accessible.name: "任务类型"
        }
        CheckBox {
            id: useRag; objectName: "missionUseRag"; text: "本次任务使用代码索引（RAG）"
            Layout.fillWidth: true; checked: root.ragRequested
            onToggled: root.ragRequested = checked
        }
        Label {
            text: "代码索引仅提供候选，任务会复核当前源码；不可用时退回文件检索。"
            color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
            font.pixelSize: 12
        }
        RowLayout {
            Layout.fillWidth: true
            Label { text: "本次 MCP 只读工具"; color: Theme.secondary; Layout.fillWidth: true }
            Controls.ActionButton {
                objectName: "missionMcpRefresh"; text: "刷新"; flat: true
                enabled: !!root.mcp && !root.mcp.missionLoading
                onClicked: root.mcp.refreshMissionChoices()
            }
        }
        Label {
            objectName: "missionMcpAvailability"; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
            text: !root.mcp ? "MCP 控制器不可用，仍可使用文件检索。"
                : root.mcp.missionLoading ? "正在读取已保存的工具配置…"
                : root.mcp.missionError || (!root.mcp.missionChoices.length ? "没有已配置的兼容任务只读工具。" : "仅本次勾选的服务可被 Analyst 使用，最多 4 个。")
            color: root.mcp && root.mcp.missionError ? Theme.warning : Theme.secondary
            font.pixelSize: 12
        }
        Repeater {
            model: root.mcp ? root.mcp.missionChoices : []
            delegate: CheckBox {
                id: mcpChoice; objectName: "missionMcpChoice"; required property var modelData
                property string serverId: modelData.server_id
                Layout.fillWidth: true; text: modelData.name + " · " + modelData.tools.join("、")
                checked: root.selectedMcpIds.indexOf(serverId) >= 0
                enabled: !root.mcp.missionLoading && (checked || root.selectedMcpIds.length < 4)
                contentItem: Label {
                    text: mcpChoice.text; textFormat: Text.PlainText; color: Theme.text
                    wrapMode: Text.WrapAnywhere; leftPadding: mcpChoice.indicator.width + mcpChoice.spacing
                }
                onToggled: {
                    var ids = root.selectedMcpIds.slice()
                    if (checked) ids.push(serverId)
                    else ids = ids.filter(function(id) { return id !== mcpChoice.serverId })
                    root.selectedMcpIds = ids
                }
            }
        }
        Label {
            objectName: "missionBaselineSummary"; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
            color: Theme.secondary
            text: root.mission.creationReady
                ? "实际基线：HEAD " + root.mission.creationPreview.base_commit + (root.baselinePaths.length ? " + " + root.baselinePaths.length + " 个选定文件" : "（不纳入未提交改动）")
                : "默认基线：HEAD。请先预览实际提交、文件选择和构建配置。"
        }
        CheckBox {
            id: presetSelection; objectName: "missionUsePresets"; text: "选择 CMake presets"
            onToggled: root.mission.invalidateCreationPreview()
        }
        TextField {
            id: configurePreset; objectName: "missionConfigurePreset"; Layout.fillWidth: true; visible: presetSelection.checked
            placeholderText: "configure preset 名称"; onTextChanged: root.mission.invalidateCreationPreview()
        }
        TextField {
            id: buildPreset; objectName: "missionBuildPreset"; Layout.fillWidth: true; visible: presetSelection.checked
            placeholderText: "build preset 名称"; onTextChanged: root.mission.invalidateCreationPreview()
        }
        TextField {
            id: testPreset; objectName: "missionTestPreset"; Layout.fillWidth: true; visible: presetSelection.checked
            placeholderText: "test preset 名称"; onTextChanged: root.mission.invalidateCreationPreview()
        }
        Controls.ActionButton {
            objectName: "missionPreviewButton"; text: root.mission.previewLoading ? "预览中…" : "预览任务基线与构建配置"
            Layout.fillWidth: true; enabled: !root.mission.busy && !root.mission.previewLoading && !!repositoryInput.text.trim()
            onClicked: root.mission.previewCreation(root.baselineRequest())
        }
        Label {
            visible: !!root.mission.creationError; objectName: "missionCreationError"
            text: root.mission.creationError; color: Theme.danger; textFormat: Text.PlainText
            Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
        }
        Label {
            visible: (root.mission.creationPreview.changed_files || []).length > 0
            text: "可选未提交文件（默认均不纳入）"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap
        }
        Repeater {
            model: root.mission.creationPreview.changed_files || []
            delegate: CheckBox {
                id: baselineFile; objectName: "missionBaselineFile"; required property string modelData
                Layout.fillWidth: true; text: modelData; checked: root.baselinePaths.indexOf(modelData) >= 0
                contentItem: Label {
                    text: baselineFile.text; textFormat: Text.PlainText; color: Theme.text
                    wrapMode: Text.WrapAnywhere; leftPadding: baselineFile.indicator.width + baselineFile.spacing
                }
                onToggled: {
                    var paths = root.baselinePaths.slice()
                    if (checked) paths.push(modelData)
                    else paths = paths.filter(function(path) { return path !== modelData })
                    root.baselinePaths = paths; root.mission.invalidateCreationPreview()
                }
            }
        }
        Label {
            visible: root.baselinePaths.length > 0 && !root.mission.creationReady
            text: "文件选择已变化，请重新预览补丁。"; color: Theme.warning; Layout.fillWidth: true; wrapMode: Text.Wrap
        }
        Controls.LongText {
            objectName: "missionBaselinePatch"; visible: root.mission.creationReady && root.baselinePaths.length > 0
            Layout.fillWidth: true; title: "纳入任务的补丁"; text: root.mission.creationPreview.patch || ""
            maximumHeight: 180
        }
        Label {
            visible: presetSelection.checked; Layout.fillWidth: true; color: Theme.secondary; wrapMode: Text.WrapAnywhere
            text: "预设原 binaryDir 会替换为任务受控构建目录；预览会检查其余受支持字段的语义。"
            textFormat: Text.PlainText
        }
        Controls.LongText {
            objectName: "missionBuildSelection"; visible: root.mission.creationReady
            Layout.fillWidth: true; title: "冻结的构建配置"; text: JSON.stringify(root.mission.creationPreview.build || {}, null, 2)
            maximumHeight: 180
        }
        CheckBox {
            id: baselineConfirmation; objectName: "missionBaselineConfirmation"; text: "确认将上述补丁纳入任务基线"
            Layout.fillWidth: true; Layout.minimumWidth: 0
            visible: root.baselinePaths.length > 0; enabled: root.mission.creationReady
            contentItem: Label {
                text: baselineConfirmation.text; textFormat: Text.PlainText; color: Theme.text
                wrapMode: Text.WrapAnywhere; leftPadding: baselineConfirmation.indicator.width + baselineConfirmation.spacing
            }
        }
        Controls.ActionButton {
            objectName: "missionCreateButton"; text: root.mission.busy ? "创建中…" : "创建 Agent 任务"; highlighted: true; Layout.fillWidth: true
            enabled: !root.mission.busy && root.mission.creationReady && !!requestInput.text.trim()
                && !!repositoryInput.text.trim() && (!root.baselinePaths.length || baselineConfirmation.checked)
            onClicked: root.submitMission()
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "打开任务 ID"; color: Theme.secondary }
        RowLayout { Layout.fillWidth: true
            Controls.AppTextField { id: taskIdInput; objectName: "missionIdInput"; Layout.fillWidth: true; placeholderText: "UUID" }
            Controls.ActionButton { text: "打开"; enabled: !!taskIdInput.text.trim(); onClicked: root.mission.selectMission(taskIdInput.text.trim()) }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.topMargin: 8
            Label { text: "任务历史"; color: Theme.text; font.weight: Font.DemiBold; Layout.fillWidth: true }
            Controls.ActionButton {
                objectName: "missionHistoryRefresh"; text: "刷新"
                enabled: !root.mission.historyLoading; onClicked: root.mission.refreshMissions()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            TextField {
                id: repositoryFilterInput; objectName: "missionRepositoryFilter"
                Layout.fillWidth: true; placeholderText: "按仓库路径筛选（留空为全部）"
                text: root.mission.repositoryFilter
                onAccepted: root.mission.repositoryFilter = text
            }
            Controls.ActionButton { objectName: "missionApplyRepositoryFilter"; text: "筛选"; onClicked: root.mission.repositoryFilter = repositoryFilterInput.text }
        }
        ComboBox {
            id: statusFilterInput; objectName: "missionStatusFilter"; Layout.fillWidth: true
            property var values: ["", "pending", "ready", "running", "awaiting_human", "interrupted", "retryable", "succeeded", "failed", "cancelled", "skipped"]
            model: ["全部状态", "待执行", "已就绪", "执行中", "等待确认", "已中断", "可重试", "已成功", "失败", "已取消", "已跳过"]
            Accessible.name: "按任务状态筛选"
            currentIndex: Math.max(0, values.indexOf(root.mission.statusFilter))
            onActivated: root.mission.statusFilter = values[currentIndex]
        }
        Label {
            objectName: "missionHistoryError"; visible: !!root.mission.historyError
            text: root.mission.historyError; color: Theme.danger; textFormat: Text.PlainText
            Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
        }
        Label {
            objectName: "missionHistoryStatus"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap
            text: root.mission.historyLoading ? "正在读取任务历史…"
                : root.mission.missions.count ? "已显示 " + root.mission.missions.count + " / " + root.mission.historyTotal + " 个任务"
                : root.mission.historyError ? "读取失败，请刷新重试。" : "暂无符合条件的任务。"
        }
        ListView {
            id: taskList; objectName: "missionTaskList"
            Layout.fillWidth: true; Layout.preferredHeight: Math.min(260, Math.max(100, contentHeight + Theme.spaceSm))
            clip: true; model: root.mission.missions; spacing: Theme.spaceSm
            delegate: ColumnLayout {
                id: taskEntry
                required property var record
                property string taskId: record.taskId
                width: taskList.width; spacing: Theme.spaceXs
                Controls.ActionButton {
                    id: selectTask
                    objectName: "missionTaskRow"
                    property string taskId: taskEntry.taskId
                    Layout.fillWidth: true
                    implicitHeight: Math.max(Theme.controlHeight, taskSummary.implicitHeight + taskMetadata.implicitHeight + Theme.spaceSm * 2 + Theme.spaceXs)
                    checked: taskEntry.record.taskId === root.mission.taskId
                    Accessible.name: "选择任务：" + (taskEntry.record.request || taskEntry.record.taskId) + "，" + W.label(taskEntry.record.status)
                    onClicked: root.mission.selectMission(taskEntry.record.taskId)
                    contentItem: Column {
                        spacing: Theme.spaceXs
                        Label {
                            id: taskSummary
                            objectName: "missionTaskPreview"
                            width: parent.width; text: taskEntry.record.request || taskEntry.record.taskId
                            wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                            textFormat: Text.PlainText; font.weight: Font.DemiBold; color: Theme.text
                        }
                        Label {
                            id: taskMetadata
                            objectName: "missionTaskMetadataPreview"
                            width: parent.width
                            text: W.icon(taskEntry.record.status) + " " + W.label(taskEntry.record.status) + " · " + (taskEntry.record.repositoryPath || "")
                            wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                            textFormat: Text.PlainText; color: W.color(taskEntry.record.status); font.pixelSize: Theme.fontSizeSm
                        }
                    }
                }
                Controls.LongText {
                    objectName: "missionTaskFull"
                    visible: taskSummary.truncated || taskMetadata.truncated
                        || (taskEntry.record.request || "").length > 60 || (taskEntry.record.repositoryPath || "").length > 30
                    Layout.fillWidth: true; title: "任务标题、状态和路径"
                    text: "任务：" + (taskEntry.record.request || taskEntry.record.taskId)
                        + "\n状态：" + W.label(taskEntry.record.status)
                        + "\n仓库：" + (taskEntry.record.repositoryPath || "—")
                        + "\n任务 ID：" + taskEntry.record.taskId
                    monospace: false; maximumHeight: 180
                }
            }
            ScrollBar.vertical: Controls.AppScrollBar {}
        }
        Controls.ActionButton {
            objectName: "missionHistoryLoadMore"; text: root.mission.historyLoading ? "读取中…" : "加载更多"
            visible: root.mission.historyHasMore; enabled: !root.mission.historyLoading
            Layout.fillWidth: true; onClicked: root.mission.loadMoreMissions()
        }
        Label { text: "打开历史只查看状态；中断任务需确认恢复计划后才能继续。"; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSizeXs; color: Theme.muted }
        }
    }
}
