import QtQuick
import QtQuick.Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Rectangle {
    id: root
    required property string status
    readonly property string statusText: W.label(status)
    implicitWidth: Math.min(180, label.implicitWidth + Theme.spaceSm * 2)
    implicitHeight: Theme.controlHeight - Theme.spaceMd
    radius: implicitHeight / 2
    color: Theme.raised
    border.width: 0
    Accessible.role: Accessible.StaticText
    Accessible.name: "节点状态：" + W.label(status)
    Label {
        id: label; anchors.fill: parent; anchors.leftMargin: Theme.spaceSm; anchors.rightMargin: Theme.spaceSm
        text: root.statusText; color: W.color(root.status); font.pixelSize: Theme.fontSizeSm
        font.weight: Font.DemiBold; verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight; textFormat: Text.PlainText
    }
    ToolTip.visible: hover.hovered
    ToolTip.text: Accessible.name
    HoverHandler { id: hover }
}
