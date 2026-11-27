import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

CheckBox {
    id: control
    hoverEnabled: true
    spacing: Theme.spaceSm
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    indicator: Rectangle {
        x: control.leftPadding
        y: (control.height - height) / 2
        implicitWidth: 18
        implicitHeight: 18
        radius: 4
        color: !control.enabled && control.checked ? Theme.disabledText
            : Theme.buttonFill(control, control.checked ? "primary" : "secondary")
        border.color: !control.enabled ? Theme.border : control.checked ? Theme.accentBorder : Theme.controlEdge
        Image {
            visible: control.checked
            anchors.centerIn: parent
            width: 16
            height: 16
            source: Theme.icon("check")
        }
    }
    contentItem: Label {
        leftPadding: control.indicator.width + control.spacing
        text: control.text
        textFormat: Text.PlainText
        font: control.font
        wrapMode: Text.WrapAnywhere
        verticalAlignment: Text.AlignVCenter
        color: control.enabled ? Theme.text : Theme.disabledText
    }
    background: Rectangle {
        radius: Theme.radiusSm
        color: control.enabled ? Theme.buttonFill(control, "flat") : Theme.transparent
        border.width: control.visualFocus || control.down ? 2 : 1
        border.color: Theme.buttonBorder(control, "flat")
    }
}
