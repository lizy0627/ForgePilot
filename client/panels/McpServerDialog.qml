import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "Theme.js" as Theme

AppDialog {
    id: root
    required property var mcp
    readonly property var draft: mcp.draft
    readonly property var connection: draft.connection || ({})
    readonly property bool locked: mcp.editorBusy || mcp.disabled || mcp.uncertain || mcp.pendingSave || mcp.state === "load_failed" || mcp.state === "conflict"
    parent: Overlay.overlay; anchors.centerIn: parent
    objectName: "mcpServerDialog"; title: "MCP 服务配置"; modal: true
    width: Math.min(760, parent.width - 40); height: Math.min(780, parent.height - 40)
    closePolicy: mcp.canCloseEditor ? Popup.CloseOnEscape : Popup.NoAutoClose
    onClosed: mcp.closeEditor()
    function args() { return Array.from(connection.args || []) }
    function envNames() { return Array.from(connection.env_names || []) }
    function publicEnvNames() { return Array.from(connection.public_env_names || []) }
    function secretHint(name) {
        var state = mcp.credentialState[name]
        return state === "saved" ? "已保存，留空保留" : state === "replacement" ? "本次已填写新值" : state === "cleared" ? "已清除，尚未保存" : "未填写"
    }
    function modes() { var result = []; if (chatScope.checked) result.push("chat"); if (taskScope.checked) result.push("task"); mcp.updateList("modes", result) }
    Connections {
        target: mcp
        function onEditorChanged() { if (mcp.editorOpen && !root.visible) root.open(); else if (!mcp.editorOpen) { programPicker.close(); directoryPicker.close(); if (root.visible) root.close() } }
        function onClearSensitiveInputs() { key.clear() }
        function onSaved() { root.close() }
    }
    contentItem: AppScrollView {
        id: scroll; objectName: "mcpEditorScroll"; clip: true; contentWidth: availableWidth
        ColumnLayout {
            width: scroll.availableWidth; spacing: Theme.spaceMd
            Label { objectName: "mcpEditorError"; visible: !!mcp.editorError; text: mcp.editorError; textFormat: Text.PlainText; color: Theme.danger; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight; Layout.fillWidth: true }
            LongText { visible: !!mcp.editorError; title: "完整 MCP 连接错误"; text: mcp.editorError; monospace: false; maximumHeight: 150; Layout.fillWidth: true }
            Label { visible: !!mcp.editorNotice; text: mcp.editorNotice; textFormat: Text.PlainText; color: Theme.secondary; wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight; Layout.fillWidth: true }
            LongText { visible: !!mcp.editorNotice; title: "完整 MCP 提示"; text: mcp.editorNotice; monospace: false; maximumHeight: 120; Layout.fillWidth: true }
            Label { visible: mcp.editorBusy; text: mcp.state === "probing" ? "正在连接并读取工具…" : mcp.state === "saving" ? "正在重新验证并保存…" : "正在读取配置…"; color: Theme.secondary }
            Label { text: "接入类型"; color: Theme.secondary }
            AppComboBox { objectName: "mcpKind"; model: ["Tavily 搜索", "自定义远程服务", "本地程序"]; currentIndex: mcp.kind; enabled: !root.locked; Layout.fillWidth: true; onActivated: mcp.setKind(currentIndex) }
            Label { text: "名称"; color: Theme.secondary }
            AppTextField { objectName: "mcpName"; text: root.draft.name || ""; maximumLength: 40; enabled: !root.locked; Layout.fillWidth: true; placeholderText: "服务名称（1–40 字）"; selectByMouse: true; onTextEdited: mcp.update("name", text) }
            Label { visible: mcp.kind === 0; text: "Tavily 搜索：填写 API Key 即可连接，地址与 Bearer 鉴权由预设提供。"; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.secondary }
            ColumnLayout {
                visible: mcp.kind === 1; Layout.fillWidth: true; spacing: Theme.spaceSm
                Label { text: "MCP 地址"; color: Theme.secondary }
                AppTextField { objectName: "mcpEndpoint"; text: root.connection.endpoint || ""; maximumLength: 2048; enabled: !root.locked; Layout.fillWidth: true; placeholderText: "https://example.com/mcp"; selectByMouse: true; onTextEdited: mcp.update("endpoint", text) }
                Label { text: "鉴权方式"; color: Theme.secondary }
                AppComboBox { objectName: "mcpAuth"; model: ["无", "Bearer Token", "API Key 请求头"]; currentIndex: ["none", "bearer", "api_key"].indexOf(root.connection.auth_type || "none"); Layout.fillWidth: true; enabled: !root.locked; onActivated: mcp.update("auth_type", ["none", "bearer", "api_key"][currentIndex]) }
                AppTextField { objectName: "mcpHeader"; visible: root.connection.auth_type === "api_key"; text: root.connection.header_name || ""; placeholderText: "请求头名称，例如 X-API-Key"; maximumLength: 128; enabled: !root.locked; Layout.fillWidth: true; onTextEdited: mcp.update("header_name", text) }
            }
            ColumnLayout {
                visible: mcp.kind !== 2 && root.connection.auth_type !== "none"; Layout.fillWidth: true; spacing: Theme.spaceSm
                Label { objectName: "mcpKeyStatus"; text: "密钥 · " + root.secretHint("api_key"); color: Theme.secondary }
                RowLayout {
                    Layout.fillWidth: true
                    AppTextField { id: key; objectName: "mcpKey"; echoMode: TextInput.Password; maximumLength: 4096; enabled: !root.locked; Layout.fillWidth: true; placeholderText: root.secretHint("api_key"); onTextEdited: mcp.setSecret("api_key", text) }
                    ActionButton { objectName: "replaceMcpKey"; text: "替换"; enabled: !root.locked; onClicked: { key.forceActiveFocus(); key.selectAll() } }
                    ActionButton { objectName: "clearMcpKey"; text: "清除"; danger: true; enabled: !root.locked; onClicked: mcp.clearSecret("api_key") }
                }
                Label { text: mcp.kind === 0 ? "搜索预设需要 Key；清除后必须重新填写才能保存。" : "清除密钥会将鉴权改为“无”；保存成功后才删除旧凭证。"; color: Theme.muted; font.pixelSize: Theme.fontSizeSm; wrapMode: Text.Wrap; Layout.fillWidth: true }
            }
            ColumnLayout {
                visible: mcp.kind === 2; Layout.fillWidth: true; spacing: Theme.spaceSm
                Label { text: "程序路径 · 使用已经安装的 .exe"; color: Theme.secondary }
                RowLayout {
                    Layout.fillWidth: true
                    AppTextField { objectName: "mcpCommand"; text: root.connection.command || ""; maximumLength: 4096; Layout.fillWidth: true; enabled: !root.locked; selectByMouse: true; onTextEdited: mcp.update("command", text) }
                    ActionButton { objectName: "chooseMcpProgram"; text: "选择文件"; icon.source: Theme.icon("folder"); enabled: !root.locked; onClicked: programPicker.open() }
                }
                RowLayout {
                    Label { text: "参数 · 每行作为一个完整 argv 项"; color: Theme.secondary; Layout.fillWidth: true }
                    ActionButton { objectName: "addMcpArg"; text: "添加参数"; icon.source: Theme.icon("add"); enabled: !root.locked && root.args().length < 32; onClicked: { var values = root.args(); values.push(""); mcp.updateList("args", values) } }
                }
                Repeater {
                    model: (root.connection.args || []).length
                    delegate: RowLayout {
                        required property int index
                        Layout.fillWidth: true
                        AppTextField { objectName: "mcpArg_" + index; text: (root.connection.args || [])[index] || ""; maximumLength: 4096; enabled: !root.locked; Layout.fillWidth: true; selectByMouse: true; onTextEdited: { var values = root.args(); values[index] = text; mcp.updateList("args", values) } }
                        ActionButton { text: "移除"; danger: true; enabled: !root.locked; onClicked: { var values = root.args(); values.splice(index, 1); mcp.updateList("args", values) } }
                    }
                }
                Label { text: "工作目录（留空使用程序所在目录）"; color: Theme.secondary }
                RowLayout {
                    Layout.fillWidth: true
                    AppTextField { objectName: "mcpCwd"; text: root.connection.cwd || ""; placeholderText: mcp.defaultDirectory(root.connection.command || ""); maximumLength: 4096; Layout.fillWidth: true; enabled: !root.locked; selectByMouse: true; onTextEdited: mcp.update("cwd", text || null) }
                    ActionButton { text: "选择目录"; icon.source: Theme.icon("folder"); enabled: !root.locked; onClicked: directoryPicker.open() }
                }
                Label { objectName: "mcpEffectiveCwd"; text: "实际目录：" + (root.connection.cwd || mcp.defaultDirectory(root.connection.command || "") || "选择程序后显示"); textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; color: Theme.muted; font.pixelSize: Theme.fontSizeSm }
                RowLayout {
                    Label { text: "环境变量 · 所有值均加密保存"; color: Theme.secondary; Layout.fillWidth: true }
                    ActionButton { objectName: "addMcpEnv"; text: "添加变量"; icon.source: Theme.icon("add"); enabled: !root.locked && root.envNames().length < 16; onClicked: { var values = root.envNames(); values.push(""); mcp.updateList("env_names", values) } }
                }
                Repeater {
                    model: (root.connection.env_names || []).length
                    delegate: RowLayout {
                        id: envRow; required property int index
                        readonly property string envName: (root.connection.env_names || [])[index] || ""
                        onEnvNameChanged: if (envValue) envValue.clear()
                        Layout.fillWidth: true
                        AppTextField { objectName: "mcpEnvName_" + index; text: envRow.envName; placeholderText: "变量名"; maximumLength: 128; Layout.preferredWidth: 160; enabled: !root.locked; onTextEdited: { var values = root.envNames(); values[index] = text; envValue.clear(); mcp.updateList("env_names", values) } }
                        AppTextField { id: envValue; objectName: "mcpEnvValue_" + index; echoMode: TextInput.Password; placeholderText: root.secretHint("env:" + envRow.envName); maximumLength: 4096; Layout.fillWidth: true; enabled: !root.locked && !!envRow.envName; onTextEdited: mcp.setSecret("env:" + envRow.envName, text) }
                        AppCheckBox { objectName: "mcpEnvSensitive_" + index; text: "敏感值"; checked: root.publicEnvNames().map(n => n.toUpperCase()).indexOf(envRow.envName.toUpperCase()) < 0; enabled: !root.locked && !!envRow.envName; onClicked: { var names = root.publicEnvNames().filter(n => n.toUpperCase() !== envRow.envName.toUpperCase()); if (!checked) names.push(envRow.envName); mcp.updateList("public_env_names", names) } }
                        ActionButton { text: "替换"; enabled: !root.locked; onClicked: { envValue.forceActiveFocus(); envValue.selectAll() } }
                        ActionButton { objectName: "removeMcpEnv_" + index; text: "移除"; danger: true; enabled: !root.locked; onClicked: { var values = root.envNames(); values.splice(index, 1); mcp.updateList("env_names", values) } }
                        Connections { target: mcp; function onClearSensitiveInputs() { envValue.clear() } }
                    }
                }
                Label { text: "不自动安装依赖。移除环境变量后，保存时会删除对应凭证。"; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.muted; font.pixelSize: Theme.fontSizeSm }
            }
            RowLayout {
                Label { text: "使用范围"; color: Theme.secondary }
                AppCheckBox { id: chatScope; objectName: "mcpChatScope"; text: "问答"; visible: mcp.kind !== 2; checked: (root.draft.modes || []).indexOf("chat") >= 0; enabled: !root.locked; onClicked: root.modes() }
                AppCheckBox { id: taskScope; objectName: "mcpTaskScope"; text: "工作"; checked: (root.draft.modes || []).indexOf("task") >= 0; enabled: !root.locked; onClicked: root.modes() }
            }
            Label { text: "仅启用读取和查询工具；修改、发送类工具暂不支持。请根据可信服务说明选择，行为提示不自动授予权限。"; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Repeater {
                model: mcp.tools
                delegate: ColumnLayout {
                    required property var modelData
                    Layout.fillWidth: true; spacing: Theme.spaceXs
                    AppCheckBox { id: toolCheck; objectName: "mcpTool_" + modelData.name; text: modelData.name; checked: modelData.selected; enabled: modelData.compatible && !root.locked; Layout.fillWidth: true; onClicked: mcp.selectTool(modelData.name, checked); contentItem: Label { text: toolCheck.text; font: toolCheck.font; color: toolCheck.enabled ? Theme.text : Theme.disabledText; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; leftPadding: toolCheck.indicator.width + toolCheck.spacing; verticalAlignment: Text.AlignVCenter } }
                    Label { text: modelData.description || "无说明"; textFormat: Text.PlainText; color: Theme.secondary; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; Layout.leftMargin: 30 }
                    Label { visible: !modelData.compatible; text: "不可选择：" + mcp.toolReason(modelData.reason || ""); textFormat: Text.PlainText; color: Theme.warning; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; Layout.leftMargin: 30 }
                }
            }
            Label { visible: mcp.state === "discovered" && !mcp.canSave; text: "至少勾选一个可用工具，并填写有效的名称和使用范围，才能保存。"; wrapMode: Text.Wrap; Layout.fillWidth: true; color: Theme.warning }
        }
    }
    footer: AppDialogButtonBox {
        ActionButton { objectName: "closeMcpEditor"; text: "关闭"; enabled: mcp.canCloseEditor; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        ActionButton { objectName: "resolveMcpSave"; visible: mcp.uncertain; text: "确认保存状态"; enabled: !mcp.editorBusy; onClicked: mcp.resolveSave() }
        ActionButton { objectName: "reloadMcpEditor"; visible: mcp.state === "load_failed" || mcp.state === "conflict"; text: "重新读取"; enabled: !mcp.editorBusy && !mcp.pendingSave; onClicked: mcp.edit(mcp.serverId) }
        ActionButton { objectName: "probeMcpButton"; text: mcp.editorError && !mcp.existing ? "重试连接" : "连接并读取工具"; enabled: !root.locked && mcp.state !== "conflict"; onClicked: mcp.probe() }
        ActionButton { objectName: "saveMcpButton"; text: mcp.existing ? "保存" : "保存并启用"; highlighted: true; enabled: mcp.canSave; onClicked: mcp.save() }
    }
    FileDialog { id: programPicker; title: "选择已安装的 MCP 程序"; nameFilters: ["程序 (*.exe)"]; onAccepted: mcp.update("command", mcp.localPath(selectedFile)) }
    FolderDialog { id: directoryPicker; title: "选择工作目录"; onAccepted: mcp.update("cwd", mcp.localPath(selectedFolder)) }
}
