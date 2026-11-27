import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

ItemDelegate {
    id: control
    readonly property string styleRole: checked || highlighted ? "selected" : "flat"
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    palette.text: enabled ? Theme.text : Theme.disabledText
    background: Rectangle {
        implicitHeight: Theme.controlHeight
        radius: Theme.radiusSm
        color: Theme.buttonFill(control, control.styleRole)
        border.width: control.visualFocus || control.down ? 2 : 1
        border.color: Theme.buttonBorder(control, control.styleRole)
    }
}
