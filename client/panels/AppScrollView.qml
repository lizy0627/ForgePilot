import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

ScrollView {
    id: control
    rightPadding: Theme.spaceMd
    ScrollBar.vertical: AppScrollBar {
        objectName: "verticalScrollBar"
        parent: control
        x: control.width - width
        y: control.topPadding
        height: control.availableHeight
    }
    ScrollBar.horizontal: AppScrollBar {
        objectName: "horizontalScrollBar"
        parent: control
        x: control.leftPadding
        y: control.height - height
        width: control.availableWidth
    }
}
