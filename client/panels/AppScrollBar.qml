import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

ScrollBar {
    id: control
    hoverEnabled: true
    padding: 2
    contentItem: Rectangle {
        implicitWidth: 8; implicitHeight: 8; radius: 4
        color: !control.enabled ? Theme.disabledText : control.pressed ? Theme.accent : control.hovered ? Theme.secondary : Theme.scroll
        opacity: control.size < 1 ? 1 : 0
    }
    background: Rectangle { color: Theme.input; radius: 4; visible: control.size < 1 }
}
