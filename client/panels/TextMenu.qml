import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Menu {
    id: menu
    required property var editor
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    background: Rectangle { color: Theme.panel; border.color: Theme.border; radius: Theme.radiusSm }
    AppMenuItem { text: "撤销"; enabled: !menu.editor.readOnly && menu.editor.canUndo; onTriggered: menu.editor.undo() }
    AppMenuItem { text: "重做"; enabled: !menu.editor.readOnly && menu.editor.canRedo; onTriggered: menu.editor.redo() }
    MenuSeparator {}
    AppMenuItem { text: "剪切"; enabled: !menu.editor.readOnly && menu.editor.selectedText.length > 0; onTriggered: menu.editor.cut() }
    AppMenuItem { text: "复制"; enabled: menu.editor.selectedText.length > 0; onTriggered: menu.editor.copy() }
    AppMenuItem { text: "粘贴"; enabled: !menu.editor.readOnly && menu.editor.canPaste; onTriggered: menu.editor.paste() }
    AppMenuItem { text: "全选"; enabled: menu.editor.length > 0; onTriggered: menu.editor.selectAll() }
}
