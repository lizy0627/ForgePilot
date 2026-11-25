import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkflowTheme.js" as Palette

Rectangle {
    id: root
    objectName: "missionTaskCenter"
    required property var mission
    property var mcp: null
    property var rag: null
    property bool composerMode: false
    property bool advancedExpanded: false
    readonly property string draftRepositoryPath: repositoryInput.text.trim()
    readonly property string draftTaskType: ["auto", "bug_fix", "feature"][kind.currentIndex] || "auto"
    readonly property bool hasCmakePreset: mission.creationReady && mission.creationPreview.build?.source === "preset"
    property bool formReady: false
    property string committedRepositoryPath: ""
    property var ragOverride: null
    property var selectedMcpIds: []
    property bool submittedDraft: false
    property string selectedContext: ""
    property var baselinePaths: []
    function updateRecommendations() {
        if (!formReady) return
        if (visible)
            mission.updateSkillContext(requestInput.text.trim(), draftTaskType, draftRepositoryPath,
                                       hasCmakePreset ? ["cmake"] : [])
        else
            mission.resetSkillContext()
    }
    onVisibleChanged: updateRecommendations()
    onDraftRepositoryPathChanged: updateRecommendations()
    onDraftTaskTypeChanged: updateRecommendations()
    onHasCmakePresetChanged: updateRecommendations()
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
        result.task_type = draftTaskType
        result.preview_token = root.mission.creationPreview.preview_token
        result.baseline_confirmed = baselineConfirmation.checked
        result.mcp_server_ids = root.selectedMcpIds
        result.use_rag = useRag.checked
        root.submittedDraft = true
        root.mission.createMission(result)
    }
    Connections {
        target: root.mission
        function onBaseUrlChanged() { root.updateRecommendations() }
        function onCreationChanged() { if (baselineConfirmation && !root.mission.creationReady) baselineConfirmation.checked = false }
        function onMissionCreated() { if (root.submittedDraft) root.resetDraftTools() }
    }
    Connections {
        target: root.mcp
        function onMissionChoicesChanged() { if (root.mcp.missionLoading || !root.mcp.missionChoices.length) root.selectedMcpIds = [] }
    }
    color: composerMode ? Palette.background : Theme.sidebar
    border.color: Theme.border
    border.width: composerMode ? 0 : 1
    implicitWidth: 244
    Component.onCompleted: {
        formReady = true
        updateRecommendations()
        if (mcp) mcp.refreshMissionChoices()
    }
    function resetBaseline() {
        baselinePaths = []
        mission.invalidateCreationPreview(true)
    }
    function resetDraftTools() {
        selectedMcpIds = []
        ragOverride = null
        submittedDraft = false
    }
    function commitRepository(path) {
        if (committedRepositoryPath && committedRepositoryPath !== path) resetDraftTools()
        committedRepositoryPath = path
    }
    function fillDraft(request, repository, source) {
        resetDraftTools()
        mission.resetSkillContext()
        committedRepositoryPath = repository.trim()
        requestInput.text = request
        selectedContext = source
        if (repositoryInput.text === repository) resetBaseline()
        else repositoryInput.text = repository
        updateRecommendations()
    }
    FolderDialog {
        id: folder; title: "选择任务仓库"; options: FolderDialog.DontUseNativeDialog
        onAccepted: {
            repositoryInput.text = root.mission.localPath(selectedFolder)
            root.commitRepository(repositoryInput.text.trim())
        }
    }
    Controls.AppScrollView {
        id: sidebarScroll
        objectName: "missionTaskCenterScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        contentHeight: sidebarLayout.y + sidebarLayout.height + Theme.spaceMd

        ColumnLayout {
            id: sidebarLayout
            readonly property int margin: root.composerMode && sidebarScroll.availableWidth > 480 ? 32 : Theme.spaceMd
            width: Math.min(root.composerMode ? 660 : sidebarScroll.availableWidth, Math.max(0, sidebarScroll.availableWidth - 2 * margin))
            x: (sidebarScroll.availableWidth - width) / 2
            y: root.composerMode && !root.advancedExpanded && !root.mission.creationReady
               && !root.selectedContext && !root.mission.creationError
                ? Math.max(margin, (root.height - 340) * 0.42) : margin
            spacing: root.composerMode ? Theme.spaceMd : Theme.spaceSm

            Label {
                text: root.composerMode ? "你想让 Agent 完成什么？" : "任务中心"
                Layout.fillWidth: true
                Layout.bottomMargin: root.composerMode ? Theme.spaceSm : 0
                horizontalAlignment: root.composerMode ? Text.AlignHCenter : Text.AlignLeft
                wrapMode: Text.Wrap
                font.pixelSize: root.composerMode ? (sidebarLayout.width < 400 ? 23 : 30) : Theme.fontSizeLg
                font.weight: Font.DemiBold
                color: root.composerMode ? Palette.text : Theme.text
            }
            Label {
                id: contextPreview
                objectName: "missionContextPreview"
                visible: !root.composerMode || !!root.selectedContext
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
            Rectangle {
                id: draftCard
                Layout.fillWidth: true
                implicitHeight: draftFields.implicitHeight + 2 * inset
                readonly property int inset: root.composerMode ? Theme.spaceMd : 0
                radius: Theme.radiusLg
                color: root.composerMode ? Palette.panel : "transparent"
                border.width: root.composerMode ? 1 : 0
                border.color: requestInput.activeFocus || repositoryInput.activeFocus ? Palette.accentBorder : Palette.border

                ColumnLayout {
                    id: draftFields
                    anchors.fill: parent; anchors.margins: draftCard.inset
                    spacing: Theme.spaceSm

                    Controls.AppScrollView {
                        objectName: "missionRequestScroll"
                        Layout.fillWidth: true
                        Layout.preferredHeight: root.composerMode ? 112 : 94
                        clip: true; contentWidth: availableWidth
                        Controls.AppTextArea {
                            id: requestInput; objectName: "missionRequestInput"
                            wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText
                            placeholderText: root.composerMode ? "描述任务目标…" : "描述 Agent 任务"
                            Accessible.name: "任务目标"
                            onTextChanged: root.updateRecommendations()
                            font.pixelSize: root.composerMode ? 16 : Theme.fontSizeMd
                            background: Rectangle {
                                implicitHeight: 94; radius: Theme.radiusMd
                                color: root.composerMode ? "transparent" : Theme.input
                                border.width: root.composerMode ? 0 : 1
                                border.color: requestInput.activeFocus ? Theme.focusRing : Theme.border
                            }
                        }
                    }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: root.composerMode ? 2 : 1
                        columnSpacing: Theme.spaceSm; rowSpacing: Theme.spaceSm
                        Controls.AppTextField {
                            id: repositoryInput; objectName: "missionRepositoryInput"
                            Layout.fillWidth: true; Layout.minimumWidth: 0
                            placeholderText: "本地仓库绝对路径"
                            Accessible.name: "仓库路径"
                            background: Rectangle {
                                implicitHeight: Theme.controlHeight
                                radius: Theme.radiusMd
                                color: root.composerMode ? Palette.input : Theme.input
                                border.color: repositoryInput.activeFocus ? Theme.focusRing : (root.composerMode ? Palette.border : Theme.border)
                            }
                            onTextChanged: {
                                root.resetBaseline()
                                if (!activeFocus) root.commitRepository(text.trim())
                            }
                            onEditingFinished: root.commitRepository(text.trim())
                        }
                        Controls.ActionButton {
                            objectName: "missionBrowseRepository"
                            text: "选择仓库…"; Layout.fillWidth: !root.composerMode
                            flat: root.composerMode; horizontalPadding: Theme.spaceSm
                            onClicked: folder.open()
                        }
                    }
                }
            }
            Controls.LongText {
                objectName: "missionRepositoryInputFull"
                visible: root.composerMode
                    ? repositoryInput.contentWidth > repositoryInput.width - repositoryInput.leftPadding - repositoryInput.rightPadding
                    : repositoryInput.text.length > 30
                Layout.fillWidth: true; title: "完整仓库路径"; text: repositoryInput.text
                maximumHeight: 150
            }
            Label {
                objectName: "missionSkillRecommendationNotice"
                visible: root.mission.recommendationsLoading || !!root.mission.recommendationError
                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                color: root.mission.recommendationError ? Theme.warning : Theme.secondary
                text: root.mission.recommendationError || "正在匹配技能…"
            }
            Label {
                visible: root.mission.skillRecommendations.length > 0
                text: "推荐技能（最多选 " + root.mission.maxSelectedSkills + " 项）"; color: Theme.text; font.weight: Font.DemiBold
                Layout.fillWidth: true
            }
            Repeater {
                model: root.mission.skillRecommendations
                delegate: Rectangle {
                    id: skillCard
                    required property var modelData
                    property bool expanded: false
                    Layout.fillWidth: true; implicitHeight: skillFields.implicitHeight + 2 * Theme.spaceSm
                    radius: Theme.radiusMd; color: Theme.panel; border.color: Theme.border
                    ColumnLayout {
                        id: skillFields
                        anchors.fill: parent; anchors.margins: Theme.spaceSm; spacing: Theme.spaceXs
                        RowLayout {
                            Layout.fillWidth: true
                            CheckBox {
                                id: skillChoice; objectName: "missionSkillChoice"
                                readonly property string skillId: skillCard.modelData.skill_id
                                Layout.fillWidth: true; Layout.minimumWidth: 0
                                text: skillCard.modelData.name + " · v" + skillCard.modelData.version
                                checked: root.mission.selectedSkillIds.indexOf(skillId) >= 0
                                enabled: !root.mission.busy && (checked || root.mission.selectedSkillIds.length < root.mission.maxSelectedSkills)
                                contentItem: Label {
                                    text: skillChoice.text; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                                    color: skillChoice.enabled ? Theme.text : Theme.disabledText
                                    leftPadding: skillChoice.indicator.width + skillChoice.spacing
                                }
                                onToggled: root.mission.setSkillSelected(skillId, checked)
                            }
                            Controls.ActionButton {
                                objectName: "missionSkillSummaryToggle"
                                text: skillCard.expanded ? "收起摘要" : "查看摘要"; flat: true
                                Accessible.name: text + " · " + skillCard.modelData.name
                                onClicked: skillCard.expanded = !skillCard.expanded
                            }
                        }
                        Label {
                            objectName: "missionSkillReasons"; Layout.fillWidth: true
                            text: skillCard.modelData.reasons.join("、"); color: Theme.secondary
                            textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                        }
                        Label {
                            objectName: "missionSkillSummary"; Layout.fillWidth: true
                            text: skillCard.modelData.summary; visible: skillCard.expanded
                            textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: Theme.text
                        }
                    }
                }
            }
            GridLayout {
                id: creationGrid
                readonly property bool actionsInHeader: root.composerMode && !root.advancedExpanded
                    && !root.mission.creationReady && !root.mission.creationError && !root.baselinePaths.length
                    && !(root.mission.creationPreview.changed_files || []).length
                Layout.fillWidth: true
                columns: 2
                rowSpacing: Theme.spaceSm
                columnSpacing: Theme.spaceSm

                Controls.ActionButton {
                    objectName: "missionAdvancedToggle"
                    Layout.row: 0; Layout.column: 0; Layout.alignment: Qt.AlignLeft
                    visible: root.composerMode
                    text: root.advancedExpanded ? "收起高级选项" : "高级选项"
                    Accessible.checkable: true
                    Accessible.checked: root.advancedExpanded
                    flat: true; horizontalPadding: Theme.spaceSm
                    onClicked: root.advancedExpanded = !root.advancedExpanded
                }
                ColumnLayout {
                    Layout.row: 1; Layout.column: 0; Layout.columnSpan: 2
                    Layout.fillWidth: true
                    visible: !root.composerMode || root.advancedExpanded
                    spacing: Theme.spaceSm
                    Label {
                        visible: root.composerMode
                        text: "任务类型"; color: Palette.secondary; font.pixelSize: Theme.fontSizeSm
                    }
                    Controls.AppComboBox {
                        id: kind; objectName: "missionTaskType"; Layout.fillWidth: true; model: ["自动判断", "修复缺陷", "实现功能"]; Accessible.name: "任务类型"
                    }
                    CheckBox {
                        id: useRag; objectName: "missionUseRag"; text: "本次任务使用代码索引（RAG）"
                        Layout.fillWidth: true
                        checked: root.ragOverride === null ? !!root.rag && root.rag.useForTasks : root.ragOverride
                        onToggled: root.ragOverride = checked
                    }
                    Label {
                        text: "索引不可用时，任务会自动使用文件检索。"
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
                    CheckBox {
                        id: presetSelection; objectName: "missionUsePresets"; text: "选择 CMake presets"
                        onToggled: root.mission.invalidateCreationPreview()
                    }
                    Controls.AppTextField {
                        id: configurePreset; objectName: "missionConfigurePreset"; Layout.fillWidth: true; visible: presetSelection.checked
                        placeholderText: "configure preset 名称"; onTextChanged: root.mission.invalidateCreationPreview()
                    }
                    Controls.AppTextField {
                        id: buildPreset; objectName: "missionBuildPreset"; Layout.fillWidth: true; visible: presetSelection.checked
                        placeholderText: "build preset 名称"; onTextChanged: root.mission.invalidateCreationPreview()
                    }
                    Controls.AppTextField {
                        id: testPreset; objectName: "missionTestPreset"; Layout.fillWidth: true; visible: presetSelection.checked
                        placeholderText: "test preset 名称"; onTextChanged: root.mission.invalidateCreationPreview()
                    }
                    Label {
                        visible: presetSelection.checked; Layout.fillWidth: true; color: Theme.secondary; wrapMode: Text.WrapAnywhere
                        text: "预设原 binaryDir 会替换为任务受控构建目录；预览会检查其余受支持字段的语义。"
                        textFormat: Text.PlainText
                    }
                }
                ColumnLayout {
                    Layout.row: 2; Layout.column: 0; Layout.columnSpan: 2
                    Layout.fillWidth: true
                    visible: !root.composerMode || root.mission.creationReady || !!root.mission.creationError
                        || (root.mission.creationPreview.changed_files || []).length > 0 || root.baselinePaths.length > 0
                    spacing: Theme.spaceSm
                    Label {
                        objectName: "missionBaselineSummary"; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                        visible: !root.composerMode || root.mission.creationReady
                        color: Theme.secondary
                        text: root.mission.creationReady
                            ? "实际基线：HEAD " + root.mission.creationPreview.base_commit + (root.baselinePaths.length ? " + " + root.baselinePaths.length + " 个选定文件" : "（不纳入未提交改动）")
                            : "默认基线：HEAD。请先预览实际提交、文件选择和构建配置。"
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
                }
                GridLayout {
                    Layout.row: creationGrid.actionsInHeader ? 0 : 3
                    Layout.column: creationGrid.actionsInHeader ? 1 : 0
                    Layout.columnSpan: creationGrid.actionsInHeader ? 1 : 2
                    Layout.alignment: Qt.AlignRight
                    Layout.fillWidth: true
                    columns: root.composerMode && creationGrid.width > 340 ? 3 : 1
                    columnSpacing: Theme.spaceSm
                    Item { visible: parent.columns === 3; Layout.fillWidth: true }
                    Controls.ActionButton {
                        objectName: "missionPreviewButton"
                        text: root.mission.previewLoading ? "正在预览…" : root.mission.creationReady ? "重新预览" : root.composerMode ? "预览任务基线" : "预览任务基线与构建配置"
                        highlighted: root.composerMode && !root.mission.creationReady
                        Layout.fillWidth: parent.columns === 1
                        enabled: !root.mission.busy && !root.mission.previewLoading && !!repositoryInput.text.trim()
                        onClicked: root.mission.previewCreation(root.baselineRequest())
                    }
                    Controls.ActionButton {
                        objectName: "missionCreateButton"
                        text: root.mission.busy ? "创建中…" : "创建 Agent 任务"
                        visible: !root.composerMode || root.mission.creationReady
                        highlighted: true; Layout.fillWidth: parent.columns === 1
                        enabled: !root.mission.busy && root.mission.creationReady && !!requestInput.text.trim()
                            && !!repositoryInput.text.trim() && (!root.baselinePaths.length || baselineConfirmation.checked)
                        onClicked: root.submitMission()
                    }
                }
            }
            Loader {
                Layout.fillWidth: true
                active: !root.composerMode; visible: active
                sourceComponent: MissionHistory { mission: root.mission }
            }
        }
    }
}
