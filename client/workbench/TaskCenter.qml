import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Rectangle {
    id: root
    required property var mission
    property string seedRequest: ""
    property string selectedContext: ""
    property string seedRepository: ""
    property var baselinePaths: []
    function baselineRequest() {
        var result = {repository_path: repositoryInput.text.trim(), base_ref: "HEAD", baseline_paths: baselinePaths}
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
        root.mission.createMission(result)
    }
    Connections {
        target: root.mission
        function onCreationChanged() { if (baselineConfirmation && !root.mission.creationReady) baselineConfirmation.checked = false }
    }
    color: Theme.sidebar
    implicitWidth: 244
    Component.onCompleted: { if (!mission.historyLoaded && !mission.historyLoading) mission.refreshMissions() }
    onSeedRequestChanged: { if (seedRequest) requestInput.text = seedRequest }
    FolderDialog { id: folder; title: "选择任务仓库"; options: FolderDialog.DontUseNativeDialog; onAccepted: repositoryInput.text = root.mission.localPath(selectedFolder) }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 12; spacing: 10
        Label { text: "任务中心"; font.pixelSize: 17; font.weight: Font.DemiBold }
        Label { text: root.selectedContext || "创建任务仅使用下方文本与仓库路径。"; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 11 }
        TextArea { id: requestInput; objectName: "missionRequestInput"; Layout.fillWidth: true; Layout.preferredHeight: 94; wrapMode: TextEdit.Wrap; placeholderText: "描述 Agent 任务"; textFormat: TextEdit.PlainText }
        TextField {
            id: repositoryInput; objectName: "missionRepositoryInput"; Layout.fillWidth: true
            placeholderText: "本地仓库绝对路径"; text: root.seedRepository
            onTextChanged: { root.baselinePaths = []; root.mission.invalidateCreationPreview(true) }
        }
        Controls.ActionButton { text: "选择仓库…"; Layout.fillWidth: true; onClicked: folder.open() }
        ComboBox { id: kind; Layout.fillWidth: true; model: ["自动判断", "修复缺陷", "实现功能"]; Accessible.name: "任务类型" }
        Controls.ActionButton {
            objectName: "missionCreateButton"; text: root.mission.busy ? "创建中…" : "创建 Agent 任务"; highlighted: true; Layout.fillWidth: true
            enabled: !root.mission.busy && root.mission.creationReady && !!requestInput.text.trim()
                && !!repositoryInput.text.trim() && (!root.baselinePaths.length || baselineConfirmation.checked)
            onClicked: root.submitMission()
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        Label { text: "打开任务 ID"; color: Theme.secondary }
        RowLayout { Layout.fillWidth: true
            TextField { id: taskIdInput; objectName: "missionIdInput"; Layout.fillWidth: true; placeholderText: "UUID" }
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
            id: taskList; objectName: "missionTaskList"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            model: root.mission.missions; spacing: 5
            delegate: Controls.ActionButton {
                required property var record
                property string taskId: record.taskId
                objectName: "missionTaskRow"
                width: taskList.width; height: 62; checked: record.taskId === root.mission.taskId
                Accessible.name: "选择任务：" + (record.request || record.taskId) + "，" + W.label(record.status)
                onClicked: root.mission.selectMission(record.taskId)
                contentItem: Column {
                    spacing: 3
                    Label { width: parent.width; text: record.request || record.taskId; elide: Text.ElideRight; textFormat: Text.PlainText; font.weight: Font.DemiBold }
                    Label { width: parent.width; text: W.icon(record.status) + " " + W.label(record.status) + " · " + (record.repositoryPath || ""); elide: Text.ElideMiddle; color: W.color(record.status); font.pixelSize: 11 }
                }
            }
            ScrollBar.vertical: Controls.AppScrollBar {}
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
            objectName: "missionHistoryLoadMore"; text: root.mission.historyLoading ? "读取中…" : "加载更多"
            visible: root.mission.historyHasMore; enabled: !root.mission.historyLoading
            Layout.fillWidth: true; onClicked: root.mission.loadMoreMissions()
        }
        Label { text: "打开历史只查看状态；中断任务需确认恢复计划后才能继续。"; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 10; color: Theme.muted }
    }
}
