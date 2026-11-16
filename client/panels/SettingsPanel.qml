import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "RagUi.js" as RagUi

ColumnLayout {
    id: root
    required property var controller
    property var mcp: null
    property var rag: null
    property var runtimeConfig: null
    signal configureRuntimeRequested()
    onVisibleChanged: if (visible && rag && controller.repositoryPath) rag.refresh()
    TabBar {
        id: tabs; Layout.fillWidth: true
        NavigationTab { objectName: "connectionSettingsTab"; text: "连接" }
        NavigationTab { objectName: "mcpSettingsTab"; text: "MCP 工具"; enabled: !!root.mcp }
    }
    StackLayout {
        currentIndex: tabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
        ColumnLayout {
            Panel {
                Layout.fillWidth: true; title: "运行配置"
                Label {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary
                    text: "配置模型、密钥及可选开发工具。后端未启动时也可打开；保存后通过受控重启生效。"
                }
                ActionButton {
                    objectName: "openRuntimeConfigButton"; text: "配置模型与运行环境"; enabled: !!root.runtimeConfig
                    onClicked: root.configureRuntimeRequested()
                }
            }
            Panel {
                Layout.fillWidth: true; title: "连接"; subtitle: "本机后端"
                RowLayout {
                    Layout.fillWidth: true
                    TextField { id: server; objectName: "serverInput"; text: root.controller.baseUrl; placeholderText: "http://127.0.0.1:8765"; Layout.fillWidth: true; selectByMouse: true }
                    ActionButton { objectName: "connectButton"; text: "连接"; enabled: !root.controller.busy; onClicked: root.controller.configureBackend(server.text) }
                    StatusBadge { status: root.controller.serverStatus }
                }
                Label { text: "连接地址仅在本次运行中生效。切换后端或关闭窗口不会取消任务；模型配置由后端管理。"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: 12 }
            }
            Panel {
                objectName: "codeIndexPanel"; Layout.fillWidth: true
                title: "代码索引"; subtitle: root.rag ? RagUi.statusLabel(root.rag.status) : "控制器不可用"
                RowLayout {
                    Layout.fillWidth: true
                    Switch {
                        objectName: "ragUseSwitch"
                        text: "新任务使用 RAG"
                        checked: !!root.rag && root.rag.useForTasks
                        enabled: !!root.rag
                        onToggled: if (root.rag) root.rag.useForTasks = checked
                        Accessible.description: "只影响之后发送的工作任务，不会启动或停止后端，也不会删除索引。"
                    }
                    Item { Layout.fillWidth: true }
                    Label {
                        objectName: "codeIndexStatus"
                        text: root.rag ? RagUi.statusLabel(root.rag.status) : "不可用"
                        color: root.rag ? RagUi.statusColor(root.rag.status, root.rag.reachable) : Theme.muted
                        font.bold: true; textFormat: Text.PlainText
                    }
                }
                Label {
                    objectName: "codeIndexStatusDetail"
                    Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                    color: root.rag && (root.rag.status === "failed" || !!root.rag.error) ? Theme.danger
                        : root.rag && root.rag.status === "degraded" ? Theme.warning : Theme.muted
                    text: root.rag ? RagUi.statusDetail(root.rag.status, root.rag.statusErrorCode, root.rag.reachable)
                        : "代码索引控制器不可用。"
                }
                Label {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                    color: root.rag && root.rag.available ? Theme.secondary : Theme.muted
                    text: !root.controller.repositoryPath ? "请先为当前对话选择项目。"
                        : !root.rag ? "代码索引控制器不可用。"
                        : "文件 %1  ·  Chunk %2  ·  待更新 %3  ·  失败 %4".arg(root.rag.fileCount).arg(root.rag.chunkCount).arg(root.rag.dirtyCount).arg(root.rag.failedCount)
                }
                ProgressBar {
                    objectName: "codeIndexProgress"; Layout.fillWidth: true
                    visible: !!root.rag && root.rag.running
                    from: 0; to: 1; value: root.rag ? root.rag.progress : 0
                    indeterminate: root.rag && root.rag.running && root.rag.totalFiles === 0
                }
                Label {
                    Layout.fillWidth: true; color: Theme.muted; font.pixelSize: 11
                    textFormat: Text.PlainText; elide: Text.ElideRight
                    text: root.rag && root.rag.running
                        ? "正在处理 %1 / %2".arg(root.rag.processed).arg(root.rag.totalFiles)
                        : root.rag && root.rag.lastIndexedAt ? "最近索引：" + root.rag.lastIndexedAt : "尚未建立索引"
                }
                RowLayout {
                    Layout.fillWidth: true
                    ActionButton {
                        objectName: "buildCodeIndexButton"; text: root.rag && root.rag.fileCount ? "更新索引" : "构建索引"
                        enabled: !!root.rag && !!root.controller.repositoryPath && root.rag.enabled && root.rag.available && !root.rag.busy && !root.rag.running
                        onClicked: root.rag.buildIndex()
                    }
                    ActionButton {
                        objectName: "cancelCodeIndexButton"; text: "取消"; visible: !!root.rag && root.rag.running
                        enabled: visible && !root.rag.busy; onClicked: root.rag.cancelIndex()
                    }
                    Item { Layout.fillWidth: true }
                    ActionButton {
                        objectName: "deleteCodeIndexButton"; text: "清空索引"; flat: true
                        enabled: !!root.rag && root.rag.fileCount > 0 && !root.rag.busy && !root.rag.running
                        onClicked: deleteIndexDialog.open()
                    }
                }
                Label {
                    objectName: "codeIndexError"; visible: !!root.rag && !!root.rag.error
                    text: root.rag ? root.rag.error : ""; textFormat: Text.PlainText
                    color: Theme.danger; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; maximumLineCount: 2
                }
                RowLayout {
                    visible: !!root.rag && !!root.rag.error; Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    ActionButton { text: "忽略"; flat: true; onClicked: root.rag.clearError() }
                    ActionButton { text: "重试"; flat: true; enabled: !!root.rag && !root.rag.busy; onClicked: if (root.rag) root.rag.refresh() }
                }
            }
            Item { Layout.fillHeight: true }
        }
        Loader { active: !!root.mcp; sourceComponent: Component { McpSettingsPanel { mcp: root.mcp } } }
    }
    Dialog {
        id: deleteIndexDialog; objectName: "deleteCodeIndexDialog"; modal: true
        title: "清空代码索引？"; anchors.centerIn: parent
        width: Math.min(420, Math.max(280, root.width - 48))
        standardButtons: Dialog.Cancel
        contentItem: Label { text: "只会删除当前项目的检索索引，不会修改项目源码。"; wrapMode: Text.Wrap; textFormat: Text.PlainText }
        footer: DialogButtonBox {
            ActionButton { text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            ActionButton { objectName: "confirmDeleteCodeIndexButton"; text: "清空"; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: if (root.rag) root.rag.deleteIndex() }
        }
    }
}
