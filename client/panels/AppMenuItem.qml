import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

MenuItem {
    id: control
    property bool danger: false
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    contentItem: Label {
        text: control.text
        font: control.font
        color: !control.enabled ? Theme.disabledText : control.danger ? Theme.danger : Theme.text
        elide: Text.ElideRight
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        implicitWidth: 128
        implicitHeight: 34
        radius: Theme.radiusSm
        color: control.highlighted && !control.hovered && !control.down && control.enabled ? Theme.accentSubtle
            : Theme.buttonFill(control, control.danger ? "danger" : "flat")
        border.width: control.visualFocus || control.down ? 2 : 1
        border.color: Theme.buttonBorder(control, "flat")
    }
}
