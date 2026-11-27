import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

Dialog {
    id: control
    palette: DarkPalette {}
    padding: Theme.spaceLg
    spacing: Theme.spaceSm
    header: Label {
        visible: control.title.length > 0
        text: control.title
        textFormat: Text.PlainText
        elide: Text.ElideRight
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLg
        font.weight: Font.DemiBold
        leftPadding: Theme.spaceLg
        rightPadding: Theme.spaceLg
        topPadding: Theme.spaceLg
    }
    background: Rectangle {
        color: Theme.panel
        radius: Theme.radiusLg
        border.color: Theme.controlEdge
    }
    footer: DialogButtonBox {
        visible: count > 0
        delegate: ActionButton {}
        padding: Theme.spaceMd
        background: Item {}
    }
    Overlay.modal: Rectangle { color: "#a0090b0d" }
}
