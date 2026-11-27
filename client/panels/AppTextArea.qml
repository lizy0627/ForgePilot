import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

TextArea {
    id: control
    hoverEnabled: true
    selectByMouse: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    color: enabled ? Theme.text : Theme.disabledText
    placeholderTextColor: Theme.muted
    selectionColor: Theme.selection
    selectedTextColor: Theme.text
    leftPadding: Theme.spaceMd
    rightPadding: Theme.spaceMd
    topPadding: Theme.spaceSm
    bottomPadding: Theme.spaceSm
    background: Rectangle {
        implicitWidth: 180
        implicitHeight: 96
        radius: Theme.radiusMd
        color: control.enabled ? Theme.input : Theme.disabled
        border.width: control.activeFocus ? 2 : 1
        border.color: !control.enabled ? Theme.disabled : control.activeFocus ? Theme.focusRing
            : control.hovered ? Theme.accentBorder : Theme.border
    }
}
