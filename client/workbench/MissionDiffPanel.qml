import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme

Controls.Panel {
    id: root
    required property var mission
    property int selectedFile: -1
    property int offset: 0
    readonly property var file: selectedFile >= 0 ? mission.diff.get(selectedFile) : ({})
    readonly property string patch: file.unifiedDiff || ""
    readonly property string fileDetails: "路径：" + (file.path || "—")
        + "\n变更前：" + (file.beforeHash || "无")
        + "\n变更后：" + (file.afterHash || "无")
    title: "Diff · 文件差异"
    subtitle: mission.diff.count + " 个文件"
    objectName: "missionDiffPanel"
    onMissionChanged: { selectedFile = -1; offset = 0 }
    StateNotice { mission: root.mission; hasData: root.mission.diff.count > 0; emptyText: "尚无文件修改。"; Layout.fillWidth: true }
    ListView {
        id: files; objectName: "missionDiffFiles"; visible: root.mission.diff.count > 0
        Layout.fillWidth: true; Layout.preferredHeight: Math.min(210, contentHeight + 2); clip: true; spacing: Theme.spaceXs
        model: root.mission.diff
        delegate: Controls.ActionButton {
            id: fileButton
            required property var record
            required property int index
            width: files.width; height: Math.max(Theme.controlHeight, contentItem.implicitHeight + topPadding + bottomPadding)
            text: record.changeType + " · " + record.path; flat: true
            checked: root.selectedFile === index; Accessible.name: "查看差异 " + record.path
            contentItem: Label {
                text: fileButton.text; textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
                color: fileButton.checked ? Theme.onAccent : Theme.text
            }
            onClicked: { root.selectedFile = index; root.offset = 0 }
        }
        ScrollBar.vertical: Controls.AppScrollBar {}
    }
    Label {
        visible: root.selectedFile >= 0; text: root.file.path || ""
        font.weight: Font.DemiBold; Layout.fillWidth: true
        wrapMode: Text.WrapAnywhere; maximumLineCount: 2; elide: Text.ElideMiddle
        textFormat: Text.PlainText
    }
    Label {
        visible: root.selectedFile >= 0
        text: "变更前：" + (root.file.beforeHash || "无") + "\n变更后：" + (root.file.afterHash || "无")
        font.pixelSize: Theme.fontSizeSm; color: Theme.secondary; Layout.fillWidth: true
        wrapMode: Text.WrapAnywhere; maximumLineCount: 3; elide: Text.ElideRight
        textFormat: Text.PlainText
    }
    Controls.LongText {
        objectName: "missionDiffPathFullText"; visible: root.selectedFile >= 0
        Layout.fillWidth: true; title: "完整路径与版本"; text: root.fileDetails
        maximumHeight: 180
    }
    Controls.AppScrollView {
        id: diffScroll
        objectName: "missionDiffScroll"
        visible: root.selectedFile >= 0; Layout.fillWidth: true; Layout.preferredHeight: 310
        clip: true
        Controls.AppTextArea {
            id: diffText
            objectName: "missionDiffText"
            readOnly: true; selectByMouse: true; wrapMode: TextEdit.NoWrap; textFormat: TextEdit.PlainText
            text: root.patch.substring(root.offset, root.offset + 12000)
            font.family: Theme.fontMono; font.pixelSize: Theme.fontSizeSm
            Accessible.name: "文件差异，当前显示第 " + (root.offset + 1) + " 至 " + Math.min(root.patch.length, root.offset + 12000) + " 个字符"
        }
    }
    ColumnLayout { visible: root.selectedFile >= 0; Layout.fillWidth: true
        Label {
            text: root.patch.length ? "已显示 " + (root.offset + 1) + "–" + Math.min(root.patch.length, root.offset + 12000) + " / " + root.patch.length + " 字符" : "无文本差异"
            color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
        }
        RowLayout { Layout.fillWidth: true
            Controls.ActionButton { text: "上一段"; enabled: root.offset > 0; onClicked: root.offset = Math.max(0, root.offset - 12000) }
            Controls.ActionButton {
                objectName: "missionDiffCopyButton"; text: "复制当前段"; enabled: root.patch.length > 0
                onClicked: { diffText.selectAll(); diffText.copy(); diffText.deselect() }
            }
            Controls.ActionButton { objectName: "missionDiffMore"; text: "下一段"; enabled: root.offset + 12000 < root.patch.length; onClicked: root.offset += 12000 }
        }
    }
}
