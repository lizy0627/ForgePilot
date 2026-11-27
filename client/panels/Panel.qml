import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: panel
    property string title
    property string subtitle
    default property alias content: body.data
    implicitHeight: layout.implicitHeight + Theme.spaceLg * 2
    color: Theme.panel; radius: Theme.radiusMd
    ColumnLayout {
        id: layout
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.spaceLg }
        spacing: Theme.spaceMd
        ColumnLayout {
            Layout.fillWidth: true
            spacing: Theme.spaceXs
            Label {
                id: titleLabel
                visible: !!text; Layout.fillWidth: true
                text: panel.title; color: Theme.text; font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold
                textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere
                maximumLineCount: 3; elide: Text.ElideRight
            }
            LongText {
                objectName: panel.objectName ? panel.objectName + "TitleFullText" : "panelTitleFullText"
                visible: titleLabel.truncated || panel.title.length > 120
                Layout.fillWidth: true; title: "完整标题"; text: panel.title; monospace: false
                maximumHeight: 220
            }
            Label {
                id: subtitleLabel
                visible: !!text; Layout.fillWidth: true
                text: panel.subtitle; color: Theme.muted; font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm; textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
            }
            LongText {
                objectName: panel.objectName ? panel.objectName + "SubtitleFullText" : "panelSubtitleFullText"
                visible: subtitleLabel.truncated || panel.subtitle.length > 48
                Layout.fillWidth: true; title: "完整元信息"; text: panel.subtitle
                maximumHeight: 180
            }
        }
        ColumnLayout { id: body; Layout.fillWidth: true; spacing: Theme.spaceSm }
    }
}
