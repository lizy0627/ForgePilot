import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

Dialog {
    id: root; objectName: "memoryManagerDialog"
    required property var conversations
    readonly property var memory: conversations ? conversations.memory : null
    readonly property var state: memory ? memory.summary : ({})
    property var editing: ({})
    property string removeId: ""
    property bool removingSummary: false
    property bool summaryDirty: false
    parent: Overlay.overlay; anchors.centerIn: parent
    title: "记忆管理"; modal: true
    width: Math.min(820, parent ? parent.width - 48 : 820)
    height: Math.min(680, parent ? parent.height - 60 : 680)
    closePolicy: memory && memory.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    onOpened: { if (memory) memory.open() }
    onClosed: { if (memory) memory.close(); editor.close(); removal.close() }
    contentItem: ColumnLayout {
        spacing: 10
        Label { text: "只保存你明确添加的偏好；当前指令优先，记忆不能授予修改权限。"; color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { text: root.memory ? root.memory.error : ""; visible: !!text; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: Theme.danger; Layout.fillWidth: true }
        RowLayout {
            TabBar {
                id: tabs; Layout.fillWidth: true
                NavigationTab { objectName: "longTermMemoryTab"; text: "长期记忆" }
                NavigationTab { objectName: "summaryMemoryTab"; text: "当前对话摘要" }
            }
            ActionButton { objectName: "refreshMemoryButton"; text: root.memory && root.memory.loading ? "加载中…" : "刷新"; enabled: root.memory && !root.memory.loading && !root.memory.busy; onClicked: { root.summaryDirty = false; root.memory.refresh() } }
        }
        StackLayout {
            currentIndex: tabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
            ColumnLayout {
                RowLayout {
                    Label { text: "全局 + 当前项目 · " + (root.memory ? root.memory.items.length : 0) + " 条"; color: Theme.text; Layout.fillWidth: true }
                    ActionButton {
                        objectName: "addMemoryButton"; text: "新增记忆"; highlighted: true
                        enabled: root.memory && !root.memory.loading && !root.memory.busy
                        onClicked: { root.editing = ({}); content.text = ""; editor.scope = "global"; source.checked = !!root.conversations.conversationId; editor.open() }
                    }
                }
                Label { text: root.conversations && root.conversations.repositoryPath ? "当前项目：" + root.conversations.repositoryPath : "未关联项目，仅使用全局记忆。"; textFormat: Text.PlainText; color: Theme.muted; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                Label { text: "每次模型调用最多使用 12 条、6000 字符；优先当前项目及最近编辑的记忆。单条最多 500 字符。"; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                ListView {
                    objectName: "memoryList"; model: root.memory ? root.memory.items : []
                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                    Label { anchors.centerIn: parent; visible: !parent.count; text: "暂无记忆。点击“新增记忆”手动保存偏好。"; color: Theme.secondary }
                    delegate: Rectangle {
                        id: row; required property var modelData
                        width: ListView.view.width - 12; height: info.implicitHeight + 24
                        radius: 6; color: Theme.raised
                        RowLayout {
                            id: info; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 }
                            ColumnLayout {
                                Layout.fillWidth: true
                                Label { text: row.modelData.scope === "project" ? "当前项目" : "全局"; color: Theme.accent; font.bold: true }
                                Label { text: row.modelData.content; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true; color: Theme.text }
                                Label {
                                    text: row.modelData.source_conversation_id ? "来源：" + (row.modelData.source_title || row.modelData.source_conversation_id) + (row.modelData.source_message_id ? " · 消息 #" + row.modelData.source_message_id : " · 手动保存") : "来源：手动新增（未关联对话）"
                                    textFormat: Text.PlainText; color: Theme.muted; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true
                                }
                                Label { text: "更新于 " + row.modelData.updated_at; color: Theme.muted; font.pixelSize: 11 }
                            }
                            ColumnLayout {
                                ActionButton {
                                    objectName: "editMemory_" + row.modelData.memory_id; text: "编辑"; enabled: !root.memory.busy && !root.memory.loading
                                    onClicked: { root.editing = row.modelData; content.text = row.modelData.content; editor.scope = row.modelData.scope; editor.open() }
                                }
                                ActionButton {
                                    objectName: "deleteMemory_" + row.modelData.memory_id; text: "删除"; danger: true; enabled: !root.memory.busy && !root.memory.loading
                                    onClicked: { root.removeId = row.modelData.memory_id; root.removingSummary = false; removal.open() }
                                }
                            }
                        }
                    }
                    ScrollBar.vertical: AppScrollBar {}
                }
            }
            ColumnLayout {
                Label {
                    text: !root.conversations || !root.conversations.conversationId ? "请先选择一段对话。" : root.state.summary ? "摘要覆盖至消息 #" + root.state.summary.through_message_id + " · 原始消息全部保留" : root.state.enabled === false ? "自动摘要已停用。启用后会在后续提问接近预算时重新生成。" : "尚无摘要。内容接近预算时自动压缩较早消息，并保留最近几轮原文。"
                    color: Theme.secondary; wrapMode: Text.Wrap; Layout.fillWidth: true
                }
                Label { text: "摘要只作历史背景；当前项目的代码事实必须重新读取文件验证。摘要失败不会删除原文。"; color: Theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                AppScrollView {
                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                    TextArea {
                        id: summaryText; objectName: "summaryMemoryContent"; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText
                        readOnly: !root.state.summary || (root.memory && root.memory.busy)
                        placeholderText: "当前对话摘要将在这里显示。已有摘要可直接编辑，最多 4000 字符。"
                        onTextChanged: { if (activeFocus) root.summaryDirty = true }
                        background: Rectangle { color: Theme.input; border.color: summaryText.activeFocus ? Theme.accent : Theme.border; radius: 5 }
                    }
                }
                RowLayout {
                    Label { text: summaryText.length + " / 4000"; color: Theme.muted; Layout.fillWidth: true }
                    ActionButton { objectName: "enableSummaryButton"; text: "启用自动摘要"; visible: root.state.enabled === false; enabled: root.memory && !root.memory.busy && !root.memory.loading; onClicked: root.memory.enableSummary() }
                    ActionButton { objectName: "deleteSummaryButton"; text: "删除摘要"; danger: true; enabled: !!root.state.summary && !root.memory.busy && !root.memory.loading; onClicked: { root.removingSummary = true; removal.open() } }
                    ActionButton { objectName: "saveSummaryButton"; text: "保存摘要"; highlighted: true; enabled: !!root.state.summary && !root.memory.busy && !root.memory.loading && !!summaryText.text.trim() && summaryText.length <= 4000; onClicked: root.memory.saveSummary(summaryText.text) }
                }
            }
        }
    }
    footer: DialogButtonBox { ActionButton { objectName: "closeMemoryButton"; text: "关闭"; enabled: !root.memory || !root.memory.busy; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole } }
    Connections {
        target: root.memory
        function onChanged() { if (!root.summaryDirty) summaryText.text = root.state.summary ? root.state.summary.content : "" }
        function onSaved() { editor.close(); removal.close(); root.summaryDirty = false; summaryText.focus = false }
        function onScopeChanged() { editor.close(); removal.close(); content.clear(); summaryText.clear(); root.summaryDirty = false; root.editing = ({}); root.removeId = "" }
    }
    Dialog {
        id: editor; objectName: "memoryEditorDialog"; property string scope: "global"
        parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(620, parent ? parent.width - 64 : 620); modal: true
        title: root.editing.memory_id ? "编辑记忆" : "新增记忆"
        closePolicy: root.memory && root.memory.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
        contentItem: ColumnLayout {
            RowLayout {
                Label { text: "作用范围"; color: Theme.secondary }
                ActionButton { objectName: "globalMemoryScope"; text: "全局"; checkable: true; checked: editor.scope === "global"; enabled: !root.memory || !root.memory.busy; onClicked: editor.scope = "global" }
                ActionButton { objectName: "projectMemoryScope"; text: "当前项目"; checkable: true; checked: editor.scope === "project"; enabled: root.conversations && !!root.conversations.repositoryPath && !root.memory.busy; onClicked: editor.scope = "project" }
            }
            AppScrollView {
                Layout.fillWidth: true; Layout.preferredHeight: 150; clip: true
                TextArea { id: content; objectName: "memoryContentInput"; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; readOnly: root.memory && root.memory.busy; placeholderText: "例如：默认中文回答；回答先给结论。请勿填写 API Key、密码或其他凭证。"; background: Rectangle { color: Theme.input; border.color: content.activeFocus ? Theme.accent : Theme.border; radius: 5 } }
            }
            Label { text: content.length + " / 500"; color: content.length > 500 ? Theme.danger : Theme.muted }
            CheckBox { id: source; objectName: "memorySourceCheck"; text: "记录来源为当前对话（删除对话时同步删除）"; visible: !root.editing.memory_id; enabled: root.conversations && !!root.conversations.conversationId && !root.memory.busy }
            Label { visible: !!root.editing.memory_id; text: "编辑保留原来源信息。"; color: Theme.muted }
            Label { text: root.memory ? root.memory.error : ""; visible: !!text; color: Theme.danger; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; Layout.fillWidth: true }
        }
        footer: DialogButtonBox {
            ActionButton { text: "取消"; enabled: !root.memory || !root.memory.busy; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            ActionButton { objectName: "saveMemoryButton"; text: "保存记忆"; highlighted: true; enabled: root.memory && !root.memory.busy && !root.memory.loading && !!content.text.trim() && content.length <= 500; DialogButtonBox.buttonRole: DialogButtonBox.ActionRole; onClicked: root.memory.save(root.editing.memory_id || "", content.text, editor.scope, root.editing.revision || 0, source.checked) }
        }
    }
    Dialog {
        id: removal; objectName: "memoryRemovalDialog"
        parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(480, parent ? parent.width - 64 : 480); modal: true
        title: root.removingSummary ? "删除这段摘要？" : "删除这条记忆？"
        closePolicy: root.memory && root.memory.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
        contentItem: ColumnLayout {
            Label { text: root.removingSummary ? "将删除摘要并停用自动摘要，原始消息保留。你可以稍后重新启用。" : "删除后不再作为长期记忆用于后续请求，原始对话不变。"; color: Theme.text; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Label { text: root.memory ? root.memory.error : ""; visible: !!text; color: Theme.danger; wrapMode: Text.Wrap; Layout.fillWidth: true }
        }
        footer: DialogButtonBox {
            ActionButton { text: "保留"; enabled: !root.memory || !root.memory.busy; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
            ActionButton { objectName: "confirmMemoryRemovalButton"; text: "删除"; danger: true; enabled: root.memory && !root.memory.busy; DialogButtonBox.buttonRole: DialogButtonBox.ActionRole; onClicked: { if (root.removingSummary) root.memory.removeSummary(); else root.memory.remove(root.removeId) } }
        }
    }
}
