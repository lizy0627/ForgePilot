import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

TabButton {
    id: control
    hoverEnabled: true
    padding: 8
    contentItem: Label {
        text: control.text; font: control.font
        color: control.enabled ? Theme.text : Theme.disabledText
        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        implicitHeight: 36; radius: 4
        color: !control.enabled ? Theme.disabled : control.down ? Theme.pressed : control.checked ? Theme.selection : control.hovered ? Theme.hover : Theme.panel
        border.color: control.visualFocus ? Theme.accent : control.checked ? Theme.accentFill : Theme.border
    }
}
