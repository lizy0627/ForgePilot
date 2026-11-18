import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Button {
    id: control
    property bool danger: false
    property real cornerRadius: Theme.radiusMd
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    font.weight: highlighted || checked ? Font.DemiBold : Font.Normal
    padding: Theme.spaceSm
    horizontalPadding: Theme.spaceLg
    palette.buttonText: !enabled ? Theme.disabledText : danger ? Theme.danger : highlighted || checked ? Theme.onAccent : Theme.text
    background: Rectangle {
        implicitWidth: 84; implicitHeight: Theme.controlHeight; radius: control.cornerRadius
        color: !control.enabled ? Theme.disabled
            : control.danger ? (control.hovered || control.down ? Theme.dangerHover : Theme.dangerFill)
            : control.highlighted || control.checked ? (control.down ? Theme.accentPressed : control.hovered ? Theme.accentHover : Theme.accentFill)
            : control.down ? Theme.pressed : control.hovered ? Theme.hover : control.flat ? Theme.transparent : Theme.raised
        border.width: control.visualFocus || control.activeFocus ? 2 : 1
        border.color: control.visualFocus || control.activeFocus ? Theme.focusRing
            : control.flat || control.highlighted || control.checked ? Theme.transparent : Theme.border
    }
}
