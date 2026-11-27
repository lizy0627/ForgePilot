import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Switch {
    id: control
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    palette.windowText: enabled ? Theme.text : Theme.disabledText
    indicator: Rectangle {
        implicitWidth: 34
        implicitHeight: 20
        x: control.leftPadding
        y: (control.height - height) / 2
        radius: height / 2
        color: Theme.buttonFill(control, control.checked ? "primary" : "secondary")
        border.color: control.enabled ? Theme.controlEdge : Theme.border
        Rectangle {
            width: 12
            height: 12
            radius: width / 2
            y: 4
            x: 4 + control.visualPosition * (parent.width - width - 8)
            color: control.checked && control.enabled ? Theme.onAccent : Theme.secondary
        }
    }
    background: Rectangle {
        radius: Theme.radiusSm
        color: control.enabled ? Theme.buttonFill(control, "flat") : Theme.transparent
        border.width: control.visualFocus || control.down ? 2 : 1
        border.color: Theme.buttonBorder(control, "flat")
    }
}
