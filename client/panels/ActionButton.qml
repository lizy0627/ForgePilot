import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Button {
    id: control
    property bool danger: false
    property real cornerRadius: Theme.radiusMd
    readonly property string styleRole: danger ? "danger" : highlighted ? "primary" : checked ? "selected" : flat ? "flat" : "secondary"
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    font.weight: highlighted || checked ? Font.DemiBold : Font.Normal
    padding: Theme.spaceSm
    horizontalPadding: display === AbstractButton.IconOnly ? Theme.spaceSm : Theme.spaceMd
    icon.width: 18
    icon.height: 18
    readonly property color foreground: !enabled ? Theme.disabledText : danger ? Theme.danger : highlighted ? Theme.onAccent : checked ? Theme.accent : Theme.text
    icon.color: foreground
    palette.buttonText: foreground
    Accessible.name: text
    ToolTip.visible: enabled && hovered && display === AbstractButton.IconOnly
    ToolTip.text: Accessible.name
    ToolTip.delay: 650
    background: Rectangle {
        implicitWidth: control.display === AbstractButton.IconOnly ? Theme.controlHeight : 68
        implicitHeight: Theme.controlHeight
        radius: control.cornerRadius
        color: Theme.buttonFill(control, control.styleRole)
        border.width: control.visualFocus || control.down ? 2 : 1
        border.color: Theme.buttonBorder(control, control.styleRole)
    }
}
