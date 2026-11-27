import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

TabButton {
    id: control
    readonly property string styleRole: checked ? "selected" : "flat"
    hoverEnabled: true
    padding: Theme.spaceSm
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeSm
    font.weight: checked ? Font.DemiBold : Font.Normal
    contentItem: Label {
        text: control.text
        font: control.font
        color: !control.enabled ? Theme.disabledText : control.checked ? Theme.accent : Theme.secondary
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        implicitHeight: Theme.controlHeight
        radius: Theme.radiusSm
        color: Theme.buttonFill(control, control.styleRole)
        border.width: control.visualFocus || control.down ? 2 : 1
        border.color: Theme.buttonBorder(control, control.styleRole)
        Rectangle {
            visible: control.checked
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            width: Math.min(parent.width / 3, 28)
            height: 2
            color: control.enabled ? Theme.accent : Theme.disabledText
        }
    }
}
