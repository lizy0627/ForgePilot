import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

AppDialog {
    id: root
    objectName: "runtimeConfigDialog"
    property var controller: null
    property var draft: ({})
    readonly property string externalModelConfig: draft.external_model_config || ""
    property bool advanced: false
    title: controller && controller.configured ? "运行配置" : "首次运行配置"
    modal: true
    width: Math.min(820, parent ? parent.width - 32 : 820)
    height: Math.min(780, parent ? parent.height - 32 : 780)
    anchors.centerIn: parent
    closePolicy: root.controller && root.controller.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    component StepArrow: Rectangle {
        required property bool pressed
        required property bool hovered
        property bool upward: false
        implicitWidth: 30; implicitHeight: Theme.controlHeight
        radius: Theme.radiusSm
        color: !enabled ? Theme.disabled : pressed ? Theme.pressed : hovered ? Theme.hover : Theme.raised
        border.color: enabled && pressed ? Theme.accent : Theme.border
        Image {
            anchors.centerIn: parent
            source: Theme.icon("chevron-down")
            width: 16; height: 16; rotation: parent.upward ? 180 : 0
            opacity: parent.enabled ? 1 : 0.45
        }
    }

    function setField(name, value) {
        var next = Object.assign({}, draft)
        next[name] = value
        draft = next
    }
    function readFields() {
        if (controller) draft = Object.assign({}, controller.fields)
    }
    function clearSecrets() { apiKey.text = ""; embeddingKey.text = "" }
    function submit(action) {
        if (!controller || controller.busy) return
        if (action === "save") controller.save(draft, apiKey.text, embeddingKey.text)
        else controller.check(draft, apiKey.text, embeddingKey.text)
        clearSecrets()
    }
    onOpened: {
        readFields()
        if (controller && !controller.loaded && !controller.busy) controller.load()
    }
    onClosed: clearSecrets()
    Connections {
        target: root.controller || null
        function onFieldsChanged() { root.readFields() }
        function onSecretsConsumed() { root.clearSecrets() }
    }
    contentItem: ColumnLayout {
        spacing: Theme.spaceMd
        Label {
            Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
            text: "在本机保存模型和可选开发工具。后端未启动时也可配置；运行中的任务继续使用原有冻结配置。"
            color: Theme.secondary
        }
        AppScrollView {
            id: scroll
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: scroll.availableWidth; spacing: Theme.spaceMd
                enabled: !!root.controller && !root.controller.busy
                Label { text: "模型服务"; font.weight: Font.DemiBold; font.pixelSize: Theme.fontSizeLg; color: Theme.text }
                Label {
                    objectName: "runtimeExternalModelConfig"
                    visible: root.externalModelConfig !== ""; Layout.fillWidth: true
                    wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; color: Theme.secondary
                    text: "模型路由由此文件管理：" + root.externalModelConfig
                }
                Label { visible: root.externalModelConfig === ""; text: "服务地址"; color: Theme.secondary }
                AppTextField {
                    objectName: "runtimeBaseUrl"; Layout.fillWidth: true
                    visible: root.externalModelConfig === ""
                    text: root.draft.base_url || ""; placeholderText: "https://服务地址/v1"; selectByMouse: true
                    onTextEdited: root.setField("base_url", text)
                }
                GridLayout {
                    visible: root.externalModelConfig === ""
                    columns: 2; Layout.fillWidth: true; columnSpacing: Theme.spaceLg; rowSpacing: Theme.spaceSm
                    Label { text: "聊天模型"; color: Theme.secondary }
                    AppTextField {
                        objectName: "runtimeChatModel"; Layout.fillWidth: true; text: root.draft.chat_model || ""
                        selectByMouse: true; onTextEdited: root.setField("chat_model", text)
                    }
                    Label { text: "任务模型"; color: Theme.secondary }
                    AppTextField {
                        objectName: "runtimeTaskModel"; Layout.fillWidth: true; text: root.draft.task_model || ""
                        selectByMouse: true; onTextEdited: root.setField("task_model", text)
                    }
                    Label { text: "独立审查模型"; color: Theme.secondary }
                    AppTextField {
                        objectName: "runtimeReviewModel"; Layout.fillWidth: true; text: root.draft.review_model || ""
                        selectByMouse: true; onTextEdited: root.setField("review_model", text)
                    }
                    Label { text: "摘要模型"; color: Theme.secondary }
                    AppTextField {
                        objectName: "runtimeSummaryModel"; Layout.fillWidth: true; text: root.draft.summary_model || ""
                        selectByMouse: true; onTextEdited: root.setField("summary_model", text)
                    }
                    Label { text: "任务模型家族"; color: Theme.secondary }
                    AppTextField {
                        objectName: "runtimeTaskFamily"; Layout.fillWidth: true; text: root.draft.task_model_family || ""
                        placeholderText: "如 qwen"; selectByMouse: true; onTextEdited: root.setField("task_model_family", text)
                    }
                    Label { text: "审查模型家族"; color: Theme.secondary }
                    AppTextField {
                        objectName: "runtimeReviewFamily"; Layout.fillWidth: true; text: root.draft.review_model_family || ""
                        placeholderText: "使用与任务模型不同的家族"; selectByMouse: true; onTextEdited: root.setField("review_model_family", text)
                    }
                }
                Label {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                    visible: root.externalModelConfig === ""
                    text: "任务模型同时用于编排、分析、实现和修复。独立审查须使用不同的模型及模型家族。"
                }
                Label { text: "API Key"; color: Theme.secondary }
                AppTextField {
                    id: apiKey; objectName: "runtimeApiKey"; Layout.fillWidth: true; echoMode: TextInput.Password
                    placeholderText: root.controller && root.controller.hasApiKey ? "已加密保存，留空保留现有密钥" : "输入 API Key"
                    selectByMouse: true
                }
                Label {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                    text: "密钥通过 Windows 当前账户加密保存。配置读取不会显示密钥；保存和检查后清空此输入。"
                }
                AppCheckBox {
                    objectName: "runtimeCloudAllowed"; text: "允许工作任务向云端模型发送项目内容"
                    checked: root.draft.cloud_model_allowed === true
                    onToggled: root.setField("cloud_model_allowed", checked)
                }
                AppCheckBox {
                    objectName: "runtimeAdvancedToggle"; text: "高级：Embedding、构建工具和 Oracle"
                    checked: root.advanced; onToggled: root.advanced = checked
                }
                ColumnLayout {
                    visible: root.advanced; Layout.fillWidth: true; spacing: Theme.spaceSm
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "本机后端端口"; color: Theme.secondary }
                        SpinBox {
                            id: port
                            hoverEnabled: true
                            leftPadding: 34; rightPadding: 34
                            font.family: Theme.fontMono
                            Accessible.name: "本机后端端口"
                            background: Rectangle {
                                implicitWidth: 144; implicitHeight: Theme.controlHeight
                                radius: Theme.radiusSm; color: port.enabled ? Theme.input : Theme.disabled
                                border.width: port.activeFocus ? 2 : 1
                                border.color: port.activeFocus ? Theme.focusRing : port.enabled && port.hovered ? Theme.accentBorder : Theme.border
                            }
                            up.indicator: StepArrow {
                                x: port.mirrored ? 0 : port.width - width
                                height: port.height
                                upward: true
                                pressed: port.up.pressed; hovered: port.up.hovered
                            }
                            down.indicator: StepArrow {
                                x: port.mirrored ? port.width - width : 0
                                height: port.height
                                pressed: port.down.pressed; hovered: port.down.hovered
                            }
                            objectName: "runtimePort"; from: 1; to: 65535; editable: true
                            value: Number(root.draft.port || 8765); onValueModified: root.setField("port", value)
                        }
                    }
                    AppCheckBox {
                        objectName: "runtimeEmbeddingEnabled"; text: "启用可选代码索引 Embedding"
                        checked: root.draft.embedding_enabled === true
                        onToggled: root.setField("embedding_enabled", checked)
                    }
                    AppTextField {
                        objectName: "runtimeEmbeddingUrl"; Layout.fillWidth: true
                        text: root.draft.embedding_base_url || ""; placeholderText: "Embedding 服务地址"
                        selectByMouse: true; onTextEdited: root.setField("embedding_base_url", text)
                    }
                    AppTextField {
                        objectName: "runtimeEmbeddingModel"; Layout.fillWidth: true
                        text: root.draft.embedding_model || ""; placeholderText: "Embedding 模型"
                        selectByMouse: true; onTextEdited: root.setField("embedding_model", text)
                    }
                    AppTextField {
                        id: embeddingKey; objectName: "runtimeEmbeddingKey"; Layout.fillWidth: true; echoMode: TextInput.Password
                        placeholderText: root.controller && root.controller.hasEmbeddingKey ? "Embedding Key 已加密保存，留空保留" : "可选独立 Embedding Key；留空复用模型 Key"
                        selectByMouse: true
                    }
                    Label {
                        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                        text: "Embedding 未配置或故障时仍可使用文件检索。云端 Embedding 继续受任务的数据外发权限约束。"
                    }
                    Label { Layout.topMargin: Theme.spaceMd; text: "开发工具路径（留空使用已安装工具）"; font.weight: Font.DemiBold; font.pixelSize: Theme.fontSizeLg; color: Theme.text }
                    Repeater {
                        model: [{key: "cmake", label: "CMake"}, {key: "ctest", label: "CTest"}, {key: "git", label: "Git"}, {key: "rg", label: "ripgrep"}, {key: "clangd", label: "clangd"}]
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Label { text: modelData.label; Layout.preferredWidth: 80; color: Theme.secondary }
                            AppTextField {
                                objectName: "runtimeTool_" + modelData.key; Layout.fillWidth: true
                                text: root.draft[modelData.key] || ""; selectByMouse: true
                                onTextEdited: root.setField(modelData.key, text)
                            }
                        }
                    }
                    AppTextField {
                        objectName: "runtimeOraclePath"; Layout.fillWidth: true
                        text: root.draft.oracle_path || ""; placeholderText: "可选 Protected Oracle 的绝对目录"
                        selectByMouse: true; onTextEdited: root.setField("oracle_path", text)
                    }
                    Label {
                        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                        text: "普通聊天不依赖开发工具。缺少 Git/ripgrep 会影响代码任务；CMake/CTest 用于获批后的构建测试；clangd 缺失时退回文本检索。要求受保护测试的任务必须提供独立 Oracle。"
                    }
                }
            }
        }
        AppScrollView {
            id: feedback
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(feedbackColumn.implicitHeight, root.height * 0.22)
            visible: feedbackColumn.implicitHeight > 0
            clip: true; contentWidth: availableWidth
            ColumnLayout {
                id: feedbackColumn
                width: feedback.availableWidth
                spacing: Theme.spaceXs
                Label {
                    objectName: "runtimeConfigMessage"; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
                    visible: text.length > 0
                    text: root.controller ? root.controller.message : "配置控制器不可用。"
                    color: root.controller && root.controller.errors.length ? Theme.danger : Theme.secondary
                }
                Repeater {
                    model: root.controller ? root.controller.errors : []
                    delegate: Label {
                        required property var modelData
                        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.danger
                        text: ({chat: "聊天", task: "任务", review: "审查", summary: "摘要", orchestration: "编排",
                                analysis: "分析", implementation: "实现", repair: "修复", embedding: "Embedding",
                                configuration: "配置"}[modelData.purpose] || "配置") + "：" + (modelData.message || modelData.code || "操作失败")
                    }
                }
                Repeater {
                    model: root.controller ? root.controller.warnings : []
                    delegate: Label {
                        required property var modelData
                        Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                        text: modelData
                    }
                }
                Label {
                    objectName: "runtimeConfigEffectiveTime"; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.secondary
                    visible: !!root.controller && root.controller.restartRequired
                    text: root.controller && root.controller.activeCount > 0
                        ? "已保存。有运行任务时只保存配置；任务结束后重新读取状态，再重启本机服务生效。"
                        : "已保存的配置在下次受控重启后生效，当前服务和任务继续使用原配置。"
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            BusyIndicator { running: !!root.controller && root.controller.busy; visible: running; Layout.preferredWidth: 28; Layout.preferredHeight: 28 }
            ActionButton {
                objectName: "runtimeConfigReload"; text: "重新读取"; icon.source: Theme.icon("refresh"); flat: true
                enabled: !!root.controller && !root.controller.busy; onClicked: root.controller.load()
            }
            Item { Layout.fillWidth: true }
            ActionButton {
                objectName: "runtimeConfigCheck"; text: "检查连接"
                enabled: !!root.controller && !root.controller.busy && root.controller.available
                onClicked: root.submit("check")
            }
            ActionButton {
                objectName: "runtimeConfigSave"; text: "保存配置"; highlighted: true
                enabled: !!root.controller && !root.controller.busy && root.controller.available
                onClicked: root.submit("save")
            }
            ActionButton {
                objectName: "runtimeConfigRestart"; text: "重启并生效"; visible: !!root.controller && root.controller.restartRequired
                enabled: !!root.controller && root.controller.restartAllowed && !root.controller.busy
                onClicked: root.controller.restart()
            }
            ActionButton {
                objectName: "runtimeConfigCancel"; text: "取消操作"; visible: !!root.controller && root.controller.busy
                onClicked: root.controller.cancel()
            }
            ActionButton {
                objectName: "runtimeConfigClose"; text: "关闭"; flat: true
                enabled: !root.controller || !root.controller.busy; onClicked: root.close()
            }
        }
    }
}
