import "Theme.js" as Theme
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AppScrollView {
    id: root
    required property var controller
    clip: true; contentWidth: availableWidth
    ColumnLayout {
        width: root.availableWidth; spacing: Theme.spaceSm
        Label { text: "文件差异"; color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold; leftPadding: Theme.spaceMd; topPadding: Theme.spaceMd }
        Label { text: "比较首次写入前的内容与任务完成后的文件。"; color: Theme.secondary; padding: Theme.spaceMd; Layout.fillWidth: true; wrapMode: Text.Wrap }
        Label { visible: !root.controller.diffFiles.length; text: "本任务尚无文件修改。任务结束后展示最终差异。"; Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.muted; padding: Theme.spaceMd }
        Repeater {
            model: root.controller.diffFiles
            delegate: Panel {
                id: file
                required property var modelData
                Layout.fillWidth: true; title: (modelData.created ? "新增 · " : "修改 · ") + modelData.path
                subtitle: modelData.restored ? "已恢复" : ""
                Label { visible: !!file.modelData.conflict || !!file.modelData.current_error; text: "文件状态冲突：" + (file.modelData.current_error || "检测到后续外部修改；下方为任务记录的差异"); color: Theme.danger; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText }
                LongText { objectName: "diff_" + file.modelData.path; Layout.fillWidth: true; title: "文件差异（原文）"; text: file.modelData.diff || "无净变化"; maximumHeight: 320 }
            }
        }
    }
}
