import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

ScrollBar {
    id: control
    hoverEnabled: true
    padding: Theme.spaceXs / 2
    contentItem: Rectangle {
        implicitWidth: Theme.spaceSm; implicitHeight: Theme.spaceSm; radius: Theme.radiusSm
        color: !control.enabled ? Theme.disabledText : control.pressed ? Theme.accent : control.hovered ? Theme.silver : Theme.scroll
        opacity: control.size < 1 ? 1 : 0
    }
    background: Rectangle { color: Theme.input; radius: Theme.radiusSm; visible: control.size < 1 }
}
