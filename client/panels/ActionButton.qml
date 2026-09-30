import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Button {
    id: control
    property bool danger: false
    property real cornerRadius: 8
    hoverEnabled: true
    padding: 8
    horizontalPadding: 14
    palette.buttonText: !enabled ? Theme.disabledText : danger ? Theme.danger : highlighted || checked ? Theme.onAccent : Theme.text
    background: Rectangle {
        implicitWidth: 84; implicitHeight: 36; radius: control.cornerRadius
        color: !control.enabled ? Theme.disabled
            : control.danger ? (control.hovered || control.down ? Theme.dangerHover : Theme.dangerFill)
            : control.highlighted || control.checked ? (control.down ? Theme.accentPressed : control.hovered ? Theme.accentHover : Theme.accentFill)
            : control.down ? Theme.pressed : control.hovered ? Theme.hover : control.flat ? "transparent" : Theme.raised
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus ? Theme.accent : control.flat || control.highlighted || control.checked ? "transparent" : Theme.border
    }
}
