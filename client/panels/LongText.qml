pragma ComponentBehavior: Bound
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
    spacing: Theme.spaceXs
    RowLayout {
        Layout.fillWidth: true
        spacing: Theme.spaceXs
        ActionButton {
            objectName: root.objectName ? root.objectName + "Toggle" : "detailsToggle"
            text: (root.expanded ? "▾  收起 · " : "▸  查看全文 · ") + root.title
            flat: true; enabled: root.text.length > 0; Layout.fillWidth: true
            onClicked: root.expanded = !root.expanded
            Accessible.name: text
        }
        ActionButton {
            objectName: root.objectName ? root.objectName + "Copy" : "detailsCopy"
            text: "复制全文"; flat: true; enabled: root.text.length > 0
            onClicked: {
                if (!root.expanded) root.expanded = true
                Qt.callLater(function() { if (detailsLoader.item) detailsLoader.item.copyAll() })
            }
            Accessible.name: root.title + " · 复制全文"
        }
    }
    Loader {
        id: detailsLoader
        objectName: root.objectName ? root.objectName + "Loader" : "detailsLoader"
        Layout.fillWidth: true; Layout.preferredHeight: active ? root.maximumHeight : 0
        active: root.expanded; visible: active
        sourceComponent: AppScrollView {
            clip: true
            function copyAll() {
                detailText.selectAll()
                detailText.copy()
                detailText.deselect()
            }
            AppTextArea {
                id: detailText
                text: root.text; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere; font.family: root.monospace ? Theme.fontMono : Theme.fontFamily
                font.pixelSize: Theme.fontSizeSm
            }
        }
    }
}
