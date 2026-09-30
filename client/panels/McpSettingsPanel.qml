import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

ColumnLayout {
    id: root
    required property var mcp
    spacing: 12
    RowLayout {
        Layout.fillWidth: true
        Label { text: "MCP 工具"; font.pixelSize: 17; font.bold: true; Layout.fillWidth: true }
        ActionButton { objectName: "refreshMcpButton"; text: "刷新"; enabled: !mcp.loading && !mcp.busy; onClicked: mcp.refresh() }
        ActionButton { objectName: "addMcpButton"; text: "添加服务"; highlighted: true; enabled: !mcp.disabled && !mcp.loading && !mcp.busy && !mcp.pendingSave; onClicked: mcp.edit() }
    }
    Label { text: "配置修改对新请求生效，正在运行的请求继续使用原配置。"; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { visible: mcp.disabled && !mcp.loading; text: "后端已禁用 MCP，或尚未读取状态。仅可查看、停用和删除。"; color: Theme.warning; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { objectName: "mcpPageError"; visible: !!mcp.error; text: mcp.error; textFormat: Text.PlainText; color: Theme.danger; wrapMode: Text.Wrap; Layout.fillWidth: true }
    RowLayout {
        visible: mcp.pendingSave
        Label { text: "上次保存结果待确认"; color: Theme.warning }
        ActionButton { objectName: "resolveMcpPending"; text: "确认保存状态"; enabled: mcp.canCloseEditor; onClicked: mcp.resolveSave() }
    }
    Label { visible: !!mcp.notice; text: mcp.notice; textFormat: Text.PlainText; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { visible: mcp.loading || mcp.busy; text: mcp.loading ? "正在读取配置…" : "正在检查或更新配置…"; color: Theme.muted }
    AppScrollView {
        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
        ColumnLayout {
            width: parent.width; spacing: 10
            Label { visible: !mcp.loading && mcp.items.length === 0; text: "尚未配置服务。添加可信的读取/查询工具后，连接并选择允许使用的工具。"; wrapMode: Text.Wrap; color: Theme.muted; Layout.fillWidth: true }
            Repeater {
                model: mcp.items
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true; implicitHeight: row.implicitHeight + 24; color: Theme.panel; border.color: Theme.border; radius: 8
                    ColumnLayout {
                        id: row; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 6
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: modelData.name; textFormat: Text.PlainText; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                            Label { text: modelData.transport === "stdio" ? "本地程序" : "远程服务"; color: Theme.muted }
                            Switch { objectName: "mcpEnabled_" + modelData.server_id; text: checked ? "启用" : "停用"; checked: modelData.enabled; enabled: !mcp.busy && !mcp.loading && (!mcp.disabled || modelData.enabled); onClicked: mcp.setEnabled(modelData.server_id, modelData.revision, !modelData.enabled) }
                        }
                        Label {
                            text: "最近连接检查：" + (modelData.last_checked_at ? (modelData.last_check_status === "success" ? "成功" : "失败") + " · " + new Date(modelData.last_checked_at).toLocaleString(Qt.locale("zh_CN")) : "未测试") + "　已选 " + modelData.selected_tool_count + " 个工具"
                            textFormat: Text.PlainText; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true
                        }
                        RowLayout {
                            ActionButton { objectName: "editMcp_" + modelData.server_id; text: "编辑"; flat: true; enabled: !mcp.loading && !mcp.busy; onClicked: mcp.edit(modelData.server_id) }
                            ActionButton { objectName: "testMcp_" + modelData.server_id; text: "测试连接"; flat: true; enabled: !mcp.disabled && !mcp.loading && !mcp.busy; onClicked: mcp.test(modelData.server_id, modelData.revision) }
                            Item { Layout.fillWidth: true }
                            ActionButton { objectName: "deleteMcp_" + modelData.server_id; text: "删除"; danger: true; enabled: !mcp.loading && !mcp.busy; onClicked: { removal.serverId = modelData.server_id; removal.revision = modelData.revision; removal.open() } }
                        }
                    }
                }
            }
        }
    }
    Label { text: "检查结果是历史记录，不表示服务常驻在线。仅接入可信服务；工具说明不能授予源码修改权限。"; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Dialog {
        id: removal; property string serverId; property double revision
        parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(440, parent.width - 48); title: "删除 MCP 服务"; modal: true
        contentItem: Label { text: "删除此配置及凭证？已完成的历史记录会保留。"; wrapMode: Text.Wrap }
        footer: DialogButtonBox {
            ActionButton { text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            ActionButton { objectName: "confirmMcpRemovalButton"; text: "删除"; danger: true; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole; onClicked: mcp.remove(removal.serverId, removal.revision) }
        }
        Connections { target: mcp; function onChanged() { if (!mcp.editorOpen && mcp.items.length === 0) removal.close() } }
    }
}
