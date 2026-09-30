import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import "Ui.js" as Ui

Label {
    property string status: ""
    text: Ui.status(status); color: Ui.color(status)
    font.pixelSize: 12; font.bold: true
    padding: 5; leftPadding: 8; rightPadding: 8
    background: Rectangle { radius: 5; color: Qt.rgba(parent.color.r,parent.color.g,parent.color.b,0.08) }
}
