import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

AppScrollView {
    id: root
    required property var mcp
    clip: true
    contentWidth: availableWidth
    ColumnLayout {
        id: page
        width: root.availableWidth
        spacing: Theme.spaceMd
        RowLayout {
            Layout.fillWidth: true
            Label { text: "已配置的服务"; font.pixelSize: Theme.fontSizeLg; font.bold: true; Layout.fillWidth: true }
            ActionButton { objectName: "refreshMcpButton"; text: "刷新"; icon.source: Theme.icon("refresh"); enabled: !mcp.loading && !mcp.busy; onClicked: mcp.refresh() }
            ActionButton { objectName: "addMcpButton"; text: "添加服务"; icon.source: Theme.icon("add"); highlighted: true; enabled: mcp.statusKnown && !mcp.disabled && !mcp.loading && !mcp.busy && !mcp.pendingSave; onClicked: mcp.edit() }
        }
        Label { text: "为新请求配置可使用的外部工具。内置聊天和工作任务可独立使用。"; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label {
            objectName: "mcpAvailabilityMessage"
            visible: !mcp.loading && (!mcp.statusKnown || mcp.disabled)
            text: !mcp.statusKnown
                ? (mcp.error ? "无法读取 MCP 状态，请检查本机后端并点击刷新。上次配置仅供参考，当前不可操作。"
                    : "尚未读取 MCP 配置；打开本页后会自动读取。")
                : "本机后端已禁用 MCP；外部服务不可用。已有配置可查看、停用或删除。"
            color: Theme.warning; wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        Label {
            objectName: "mcpPageError"; visible: !!mcp.error; text: mcp.error
            textFormat: Text.PlainText; color: Theme.danger; wrapMode: Text.WrapAnywhere
            maximumLineCount: 2; elide: Text.ElideRight; Layout.fillWidth: true
        }
        LongText { objectName: "mcpPageErrorDetail"; visible: !!mcp.error; Layout.fillWidth: true; title: "完整 MCP 错误"; text: mcp.error; monospace: false; maximumHeight: 180 }
        RowLayout {
            visible: mcp.pendingSave
            Label { text: "上次保存结果待确认"; color: Theme.warning }
            ActionButton { objectName: "resolveMcpPending"; text: "确认保存状态"; enabled: mcp.canCloseEditor; onClicked: mcp.resolveSave() }
        }
        Label { visible: !!mcp.notice; text: mcp.notice; textFormat: Text.PlainText; color: Theme.secondary; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight; Layout.fillWidth: true }
        LongText { visible: !!mcp.notice; Layout.fillWidth: true; title: "完整 MCP 提示"; text: mcp.notice; monospace: false; maximumHeight: 150 }
        Label { visible: mcp.loading || mcp.busy; text: mcp.loading ? "正在读取配置…" : "正在检查或更新配置…"; color: Theme.muted }
        ColumnLayout {
            Layout.fillWidth: true; spacing: Theme.spaceMd
            Label { objectName: "mcpEmptyMessage"; visible: mcp.statusKnown && !mcp.loading && mcp.items.length === 0; text: mcp.disabled ? "当前没有 MCP 服务配置。" : "尚未配置服务。添加可信的读取/查询工具后，连接并选择允许使用的工具。"; wrapMode: Text.Wrap; color: Theme.muted; Layout.fillWidth: true }
            Repeater {
                model: mcp.items
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true; implicitHeight: row.implicitHeight + Theme.spaceMd * 2; color: Theme.raised; radius: Theme.radiusMd
                    ColumnLayout {
                        id: row; anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.spaceMd } spacing: Theme.spaceSm
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                id: namePreview
                                text: modelData.name; textFormat: Text.PlainText; font.bold: true
                                wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            Label { text: modelData.transport === "stdio" ? "本地程序" : "远程服务"; color: Theme.muted }
                            AppSwitch { objectName: "mcpEnabled_" + modelData.server_id; text: checked ? "启用" : "停用"; checked: modelData.enabled; enabled: mcp.statusKnown && !mcp.busy && !mcp.loading && (!mcp.disabled || modelData.enabled); onClicked: mcp.setEnabled(modelData.server_id, modelData.revision, !modelData.enabled) }
                        }
                        LongText {
                            visible: namePreview.truncated || String(modelData.name || "").length > 60
                            Layout.fillWidth: true; title: "完整服务名"; text: modelData.name || ""
                            monospace: false; maximumHeight: 140
                        }
                        Label {
                            text: "最近连接检查：" + (modelData.last_checked_at ? (modelData.last_check_status === "success" ? "成功" : "失败") + " · " + new Date(modelData.last_checked_at).toLocaleString(Qt.locale("zh_CN")) : "未测试") + "　已选 " + modelData.selected_tool_count + " 个工具"
                            textFormat: Text.PlainText; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true
                        }
                        RowLayout {
                            ActionButton { objectName: "editMcp_" + modelData.server_id; text: mcp.disabled ? "查看" : "编辑"; flat: true; enabled: mcp.statusKnown && !mcp.loading && !mcp.busy; onClicked: mcp.edit(modelData.server_id) }
                            ActionButton { objectName: "testMcp_" + modelData.server_id; text: "测试连接"; flat: true; enabled: mcp.statusKnown && !mcp.disabled && !mcp.loading && !mcp.busy; onClicked: mcp.test(modelData.server_id, modelData.revision) }
                            Item { Layout.fillWidth: true }
                            ActionButton { objectName: "deleteMcp_" + modelData.server_id; text: "删除"; danger: true; enabled: mcp.statusKnown && !mcp.loading && !mcp.busy; onClicked: { removal.serverId = modelData.server_id; removal.revision = modelData.revision; removal.open() } }
                        }
                    }
                }
            }
        }
        Label { text: "检查结果是历史记录，不表示服务常驻在线。仅接入可信服务；工具说明不能授予源码修改权限。"; color: Theme.muted; font.pixelSize: Theme.fontSizeSm; wrapMode: Text.Wrap; Layout.fillWidth: true }
        AppDialog {
            id: removal; property string serverId; property double revision
            parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(440, parent.width - 48); title: "删除 MCP 服务"; modal: true
            contentItem: Label { text: "删除此配置及凭证？已完成的历史记录会保留。"; wrapMode: Text.Wrap }
            footer: AppDialogButtonBox {
                ActionButton { text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
                ActionButton { objectName: "confirmMcpRemovalButton"; text: "删除"; danger: true; enabled: mcp.statusKnown && !mcp.loading && !mcp.busy; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: mcp.remove(removal.serverId, removal.revision) }
            }
            Connections { target: mcp; function onChanged() { if (!mcp.editorOpen && mcp.items.length === 0) removal.close() } }
        }
    }
}
