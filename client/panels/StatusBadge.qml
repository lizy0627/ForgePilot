import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import "Ui.js" as Ui

Label {
    id: badge
    property string status: ""
    text: Ui.status(status); color: Ui.color(status)
    font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSm; font.weight: Font.DemiBold
    padding: Theme.spaceXs; leftPadding: Theme.spaceSm; rightPadding: Theme.spaceSm
    background: Rectangle {
        radius: Theme.radiusSm
        color: Qt.rgba(badge.color.r, badge.color.g, badge.color.b, 0.13)
        border.color: Qt.rgba(badge.color.r, badge.color.g, badge.color.b, 0.4)
    }
}
