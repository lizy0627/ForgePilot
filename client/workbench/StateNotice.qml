import QtQuick
import QtQuick.Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W

Label {
    required property var mission
    property bool hasData: false
    property string emptyText: "暂无内容。"
    readonly property string viewState: W.state(mission, hasData)
    visible: viewState !== "ready" && (viewState !== "completed" || !hasData)
    text: W.stateText(viewState, emptyText)
    color: viewState === "error" ? Theme.danger : viewState === "interrupted" || viewState === "degraded" ? Theme.warning : Theme.secondary
    topPadding: Theme.spaceSm
    bottomPadding: Theme.spaceSm
    font.pixelSize: Theme.fontSizeSm
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    Accessible.name: text
}
