import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Menu {
    id: menu
    required property var editor
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    background: Rectangle { color: Theme.panel; border.color: Theme.border; radius: Theme.radiusSm }
    component EditItem: MenuItem {
        id: control
        hoverEnabled: true
        contentItem: Label { text: control.text; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeMd; color: control.enabled ? Theme.text : Theme.disabledText; verticalAlignment: Text.AlignVCenter }
        background: Rectangle {
            color: control.enabled && (control.highlighted || control.hovered) ? Theme.accentSubtle : Theme.panel
            border.color: control.activeFocus ? Theme.focusRing : Theme.transparent
        }
    }
    EditItem { text: "撤销"; enabled: !menu.editor.readOnly && menu.editor.canUndo; onTriggered: menu.editor.undo() }
    EditItem { text: "重做"; enabled: !menu.editor.readOnly && menu.editor.canRedo; onTriggered: menu.editor.redo() }
    MenuSeparator {}
    EditItem { text: "剪切"; enabled: !menu.editor.readOnly && menu.editor.selectedText.length > 0; onTriggered: menu.editor.cut() }
    EditItem { text: "复制"; enabled: menu.editor.selectedText.length > 0; onTriggered: menu.editor.copy() }
    EditItem { text: "粘贴"; enabled: !menu.editor.readOnly && menu.editor.canPaste; onTriggered: menu.editor.paste() }
    EditItem { text: "全选"; enabled: menu.editor.length > 0; onTriggered: menu.editor.selectAll() }
}
