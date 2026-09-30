import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var controller
    property var mcp: null
    TabBar {
        id: tabs; Layout.fillWidth: true
        NavigationTab { objectName: "connectionSettingsTab"; text: "连接" }
        NavigationTab { objectName: "mcpSettingsTab"; text: "MCP 工具"; enabled: !!root.mcp }
    }
    StackLayout {
        currentIndex: tabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
        ColumnLayout {
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
            Item { Layout.fillHeight: true }
        }
        Loader { active: !!root.mcp; sourceComponent: Component { McpSettingsPanel { mcp: root.mcp } } }
    }
}
