import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Panel {
    id: root
    required property var controller
    title: "仓库"; subtitle: "本地 · 只读"
    RowLayout {
        Layout.fillWidth: true
        Label { text: root.controller.repository.loading ? "扫描中…" : "文件树"; color: Theme.secondary; Layout.fillWidth: true }
        ActionButton { text: "刷新"; icon.source: Theme.icon("refresh"); enabled: !root.controller.repository.loading && root.controller.repositoryPath.length > 0; onClicked: root.controller.repository.refresh() }
    }
    Label { text: root.controller.repository.error || (!root.controller.repositoryPath ? "尚未选择仓库" : ""); visible: text.length > 0; color: root.controller.repository.error ? Theme.danger : Theme.muted; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
    LongText { objectName: "repositoryErrorFullText"; visible: !!root.controller.repository.error && root.controller.repository.error.length > 100; Layout.fillWidth: true; title: "仓库错误"; text: root.controller.repository.error; monospace: false }
    ListView {
        id: tree; objectName: "repositoryTree"; Layout.fillWidth: true; Layout.preferredHeight: root.controller.repositoryPath ? 200 : 0; visible: root.controller.repositoryPath.length > 0; clip: true
        model: root.controller.repository
        delegate: AppItemDelegate {
            id: entry
            required property string name
            required property string path
            required property bool directory
            required property bool expanded
            required property int depth
            required property int index
            width: tree.width - Theme.spaceMd; height: Theme.controlHeight; leftPadding: Theme.spaceSm + Math.min(depth * Theme.spaceMd, tree.width / 3)
            text: name
            Accessible.name: (directory ? (expanded ? "已展开目录 " : "已折叠目录 ") : "文件 ") + path
            highlighted: root.controller.repository.selectedPath === path
            onClicked: root.controller.repository.activate(index)
            ToolTip.visible: hovered
            ToolTip.text: path
            contentItem: RowLayout {
                spacing: Theme.spaceSm
                Image {
                    Layout.preferredWidth: 16; Layout.preferredHeight: 16
                    source: Theme.icon(entry.directory ? "chevron-right" : "document")
                    rotation: entry.directory && entry.expanded ? 90 : 0
                }
                Label {
                    Layout.fillWidth: true; text: entry.text; elide: Text.ElideRight
                    font.family: Theme.fontMono; font.pixelSize: Theme.fontSizeSm
                    color: entry.highlighted ? Theme.accent : Theme.text
                    textFormat: Text.PlainText; verticalAlignment: Text.AlignVCenter
                }
            }
        }
        ScrollBar.vertical: AppScrollBar {}
    }
    Label {
        id: selectedPathPreview
        text: root.controller.repository.selectedPath || root.controller.repositoryPath || ""
        Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; maximumLineCount: 2
        elide: Text.ElideMiddle; textFormat: Text.PlainText; color: Theme.secondary
        font.pixelSize: Theme.fontSizeXs
    }
    LongText { objectName: "repositoryPathFullText"; visible: selectedPathPreview.truncated || selectedPathPreview.text.length > 80; Layout.fillWidth: true; title: "仓库路径"; text: selectedPathPreview.text }
    Label { text: root.controller.repository.truncated ? "树已达到展示上限，请缩小仓库范围。" : "已过滤构建、环境、依赖及敏感文件。"; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.Wrap; font.pixelSize: Theme.fontSizeXs }
}
