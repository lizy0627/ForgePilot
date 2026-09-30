import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Menu {
    required property var editor
    component EditItem: MenuItem {
        id: control
        hoverEnabled: true
        contentItem: Label { text: control.text; color: control.enabled ? Theme.text : Theme.disabledText; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { color: control.enabled && (control.highlighted || control.hovered) ? Theme.selection : Theme.panel }
    }
    EditItem { text: "撤销"; enabled: !editor.readOnly && editor.canUndo; onTriggered: editor.undo() }
    EditItem { text: "重做"; enabled: !editor.readOnly && editor.canRedo; onTriggered: editor.redo() }
    MenuSeparator {}
    EditItem { text: "剪切"; enabled: !editor.readOnly && editor.selectedText.length > 0; onTriggered: editor.cut() }
    EditItem { text: "复制"; enabled: editor.selectedText.length > 0; onTriggered: editor.copy() }
    EditItem { text: "粘贴"; enabled: !editor.readOnly && editor.canPaste; onTriggered: editor.paste() }
    EditItem { text: "全选"; enabled: editor.length > 0; onTriggered: editor.selectAll() }
}
