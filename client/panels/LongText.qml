import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: root
    property string text: ""
    property string title: "查看详情"
    property bool expanded: false
    property bool monospace: true
    property int maximumHeight: 260
    spacing: 4
    ActionButton {
        objectName: root.objectName ? root.objectName + "Toggle" : "detailsToggle"
        text: (root.expanded ? "▾  收起 · " : "▸  ") + root.title
        flat: true; enabled: root.text.length > 0
        onClicked: root.expanded = !root.expanded
        Accessible.name: text
    }
    Loader {
        objectName: root.objectName ? root.objectName + "Loader" : "detailsLoader"
        Layout.fillWidth: true; Layout.preferredHeight: active ? root.maximumHeight : 0
        active: root.expanded; visible: active
        sourceComponent: AppScrollView {
            clip: true
            TextArea {
                text: root.text; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere; font.family: root.monospace ? "Consolas" : "Microsoft YaHei UI"
                font.pixelSize: 12; color: Theme.text
                background: Rectangle { color: Theme.input; radius: 4 }
            }
        }
    }
}
