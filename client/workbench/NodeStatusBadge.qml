import QtQuick
import QtQuick.Controls
import "WorkbenchUi.js" as W

Rectangle {
    id: root
    required property string status
    readonly property string statusText: W.icon(status) + " " + W.label(status)
    implicitWidth: label.implicitWidth + 16
    implicitHeight: 26
    radius: 13
    color: Qt.alpha(W.color(status), 0.12)
    border.color: W.color(status)
    Accessible.role: Accessible.StaticText
    Accessible.name: "节点状态：" + W.label(status)
    Label { id: label; anchors.centerIn: parent; text: root.statusText; color: W.color(root.status); font.pixelSize: 11; font.weight: Font.DemiBold }
    ToolTip.visible: hover.hovered
    ToolTip.text: Accessible.name
    HoverHandler { id: hover }
}
