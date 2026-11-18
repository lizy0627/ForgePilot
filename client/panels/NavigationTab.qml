import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

TabButton {
    id: control
    hoverEnabled: true
    padding: Theme.spaceSm
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeSm
    font.weight: checked ? Font.DemiBold : Font.Normal
    contentItem: Label {
        text: control.text; font: control.font
        color: !control.enabled ? Theme.disabledText : control.checked ? Theme.accent : Theme.secondary
        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        implicitHeight: Theme.controlHeight; radius: Theme.radiusSm
        color: !control.enabled ? Theme.disabled : control.down ? Theme.pressed
            : control.checked ? Theme.accentSubtle : control.hovered ? Theme.hover : Theme.transparent
        border.width: control.visualFocus || control.activeFocus ? 2 : 1
        border.color: control.visualFocus || control.activeFocus ? Theme.focusRing
            : control.checked ? Theme.accentBorder : Theme.transparent
    }
}
