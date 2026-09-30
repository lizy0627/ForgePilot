import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: panel
    property string title
    property string subtitle
    default property alias content: body.data
    implicitHeight: layout.implicitHeight + 28
    color: Theme.panel; radius: 8; border.color: Theme.border
    ColumnLayout {
        id: layout
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 14 }
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Label { text: panel.title; color: Theme.text; font.pixelSize: 16; font.bold: true; textFormat: Text.PlainText; elide: Text.ElideMiddle; Layout.fillWidth: true }
            Label { text: panel.subtitle; color: Theme.muted; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight; textFormat: Text.PlainText }
        }
        ColumnLayout { id: body; Layout.fillWidth: true; spacing: 8 }
    }
}
