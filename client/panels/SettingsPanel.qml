import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "RagUi.js" as RagUi

ColumnLayout {
    id: root
    required property var backendConnection
    property string serverStatus: ""
    property var mcp: null
    property var rag: null
    property var runtimeConfig: null
    signal configureRuntimeRequested()
    spacing: Theme.spaceLg

    component SectionTitle: Label {
        color: Theme.text
        font.pixelSize: Theme.fontSizeLg
        font.weight: Font.DemiBold
        textFormat: Text.PlainText
    }
    component SectionRule: Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 1
        Layout.topMargin: Theme.spaceSm
        Layout.bottomMargin: Theme.spaceSm
        color: Theme.border
    }

    TabBar {
        id: tabs; Layout.fillWidth: true; spacing: Theme.spaceXs
        background: Item {}
        NavigationTab { objectName: "connectionSettingsTab"; text: "连接与运行" }
        NavigationTab { objectName: "mcpSettingsTab"; text: "MCP 工具"; enabled: !!root.mcp }
    }
    StackLayout {
        currentIndex: tabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
        AppScrollView {
            id: connectionScroll; clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: connectionScroll.availableWidth
                spacing: Theme.spaceSm

                RowLayout {
                    Layout.fillWidth: true
                    SectionTitle { text: "本机后端"; Layout.fillWidth: true }
                    StatusBadge {
                        visible: root.serverStatus.length > 0
                        status: root.serverStatus
                        Layout.maximumWidth: Math.max(120, connectionScroll.availableWidth / 2)
                        elide: Text.ElideRight
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    AppTextField {
                        id: server
                        objectName: "serverInput"
                        Layout.fillWidth: true
                        Accessible.name: "本机后端地址"
                        text: root.backendConnection.baseUrl.toString()
                        placeholderText: "http://127.0.0.1:8765"
                        selectByMouse: true
                        font.family: Theme.fontMono
                        onAccepted: root.backendConnection.configureBackend(text)
                    }
                    ActionButton {
                        objectName: "connectButton"; text: "连接"
                        onClicked: root.backendConnection.configureBackend(server.text)
                    }
                }
                Label {
                    objectName: "connectionError"; visible: text.length > 0
                    Layout.fillWidth: true; color: Theme.danger; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                    text: root.backendConnection.error
                }
                Label {
                    text: "地址仅在本次运行中生效。切换后端时，已启动的任务会继续运行。"
                    color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSizeSm
                }
                SectionRule {}
                RowLayout {
                    Layout.fillWidth: true
                    ColumnLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "运行配置" }
                        Label {
                            Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.muted
                            font.pixelSize: Theme.fontSizeSm
                            text: "模型、密钥和开发工具保存在本机，可离线配置。"
                        }
                    }
                    ActionButton {
                        objectName: "openRuntimeConfigButton"
                        text: "配置运行环境"; icon.source: Theme.icon("settings")
                        enabled: !!root.runtimeConfig
                        onClicked: root.configureRuntimeRequested()
                    }
                }
                SectionRule {}
                ColumnLayout {
                    id: codeIndex; objectName: "codeIndexPanel"; Layout.fillWidth: true
                    spacing: Theme.spaceSm
                    readonly property string subtitle: !root.rag ? "控制器不可用" : !root.rag.repositoryPath ? "待选择仓库" : RagUi.statusLabel(root.rag.status, root.rag.reachable, root.rag.statusReadFailed, !!root.rag.repositoryPath, root.rag.busy)
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "代码索引"; Layout.fillWidth: true }
                        Label {
                            objectName: "codeIndexStatus"
                            text: codeIndex.subtitle
                            color: root.rag ? RagUi.statusColor(root.rag.status, root.rag.reachable, root.rag.statusReadFailed, !!root.rag.repositoryPath) : Theme.muted
                            font.pixelSize: Theme.fontSizeSm
                            textFormat: Text.PlainText
                        }
                        AppSwitch {
                            objectName: "ragUseSwitch"
                            text: "新任务使用 RAG"
                            checked: !!root.rag && root.rag.useForTasks
                            enabled: !!root.rag
                            onToggled: if (root.rag) root.rag.useForTasks = checked
                            Accessible.description: "只影响之后发送的工作任务，不会启动或停止后端，也不会删除索引。"
                        }
                    }
                    Label {
                        objectName: "codeIndexTarget"; Layout.fillWidth: true
                        visible: !!(root.rag && root.rag.repositoryPath)
                        text: "索引目标：" + (root.rag && root.rag.repositoryPath || "待选择仓库")
                        color: Theme.secondary; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                    }
                    Label {
                        id: codeIndexDetail; objectName: "codeIndexStatusDetail"
                        visible: !!root.rag
                        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                        color: root.rag && (root.rag.status === "failed" || !!root.rag.error) ? Theme.danger
                            : root.rag && root.rag.status === "degraded" ? Theme.warning : Theme.muted
                        text: !root.rag ? "代码索引控制器不可用。" : !root.rag.repositoryPath ? "请先在当前页面选择仓库。"
                            : RagUi.statusDetail(root.rag.status, root.rag.statusErrorCode, root.rag.reachable, root.rag.statusReadFailed, !!root.rag.repositoryPath, root.rag.busy)
                    }
                    LongText {
                        objectName: "codeIndexStatusFull"; visible: codeIndexDetail.lineCount > 2 || !!(root.rag && root.rag.error)
                        Layout.fillWidth: true; title: "完整索引状态"
                        text: codeIndexDetail.text
                        monospace: false; maximumHeight: 170
                    }
                    Label {
                        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                        color: root.rag && root.rag.available ? Theme.secondary : Theme.muted
                        visible: !!root.rag && !!root.rag.repositoryPath
                        text: root.rag ? "文件 %1  ·  索引片段 %2  ·  待更新 %3  ·  失败 %4".arg(root.rag.fileCount).arg(root.rag.chunkCount).arg(root.rag.dirtyCount).arg(root.rag.failedCount) : ""
                    }
                    ProgressBar {
                        objectName: "codeIndexProgress"; Layout.fillWidth: true
                        visible: !!root.rag && root.rag.running
                        from: 0; to: 1; value: root.rag ? root.rag.progress : 0
                        indeterminate: root.rag && root.rag.running && root.rag.totalFiles === 0
                    }
                    Label {
                        Layout.fillWidth: true; color: Theme.muted; font.pixelSize: Theme.fontSizeXs
                        textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                        visible: !!(root.rag && root.rag.repositoryPath)
                        text: root.rag && root.rag.running
                            ? "正在处理 %1 / %2".arg(root.rag.processed).arg(root.rag.totalFiles)
                            : root.rag && root.rag.lastIndexedAt ? "最近索引：" + root.rag.lastIndexedAt : "尚未建立索引"
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        ActionButton {
                            objectName: "buildCodeIndexButton"; text: root.rag && root.rag.fileCount ? "更新索引" : "构建索引"
                            enabled: !!root.rag && !!root.rag.repositoryPath && root.rag.enabled && root.rag.available
                                && root.rag.reachable && !root.rag.statusReadFailed && !root.rag.busy && !root.rag.running
                            onClicked: root.rag.buildIndex()
                        }
                        ActionButton {
                            objectName: "cancelCodeIndexButton"; text: "取消"; visible: !!root.rag && root.rag.running
                            enabled: visible && root.rag.reachable && !root.rag.statusReadFailed && !root.rag.busy; onClicked: root.rag.cancelIndex()
                        }
                        Item { Layout.fillWidth: true }
                        ActionButton {
                            objectName: "deleteCodeIndexButton"; text: "清空索引"; danger: true
                            enabled: !!root.rag && root.rag.fileCount > 0 && root.rag.reachable
                                && !root.rag.statusReadFailed && !root.rag.busy && !root.rag.running
                            onClicked: deleteIndexDialog.open()
                        }
                    }
                    Label {
                        objectName: "codeIndexError"; visible: !!root.rag && !!root.rag.error
                        text: root.rag ? root.rag.error : ""; textFormat: Text.PlainText
                        color: Theme.danger; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                        maximumLineCount: 2; elide: Text.ElideRight
                    }
                    LongText {
                        objectName: "codeIndexErrorDetail"
                        visible: !!root.rag && !!root.rag.error; Layout.fillWidth: true
                        title: "完整代码索引错误"; text: root.rag ? root.rag.error : ""
                        monospace: false; maximumHeight: 190
                    }
                    RowLayout {
                        visible: !!root.rag && !!root.rag.error; Layout.fillWidth: true
                        Item { Layout.fillWidth: true }
                        ActionButton { text: "忽略"; flat: true; onClicked: root.rag.clearError() }
                        ActionButton { text: "重试"; flat: true; enabled: !!root.rag && !root.rag.busy; onClicked: if (root.rag) root.rag.refresh() }
                    }
                }
            }
        }
        Loader { active: !!root.mcp; sourceComponent: Component { McpSettingsPanel { mcp: root.mcp } } }
    }
    AppDialog {
        id: deleteIndexDialog; objectName: "deleteCodeIndexDialog"; modal: true
        parent: Overlay.overlay; title: "清空代码索引？"; anchors.centerIn: parent
        width: Math.min(420, Math.max(280, parent.width - 48))
        contentItem: Label { text: "只会删除当前项目的检索索引，不会修改项目源码。"; wrapMode: Text.Wrap; textFormat: Text.PlainText }
        footer: AppDialogButtonBox {
            ActionButton { text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            ActionButton { objectName: "confirmDeleteCodeIndexButton"; text: "清空"; danger: true; enabled: !!root.rag && root.rag.reachable && !root.rag.statusReadFailed && !root.rag.busy && !root.rag.running; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: if (root.rag) root.rag.deleteIndex() }
        }
    }
}
