pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme
import "WorkbenchUi.js" as W
import "WorkflowUi.js" as Flow

Item {
    id: root
    objectName: "workflowGraph"
    property var stages: []
    property var artifactModel: null
    property string selectedNodeId: ""
    property var expandedStages: ({})
    signal nodeSelected(string nodeId)
    readonly property int columns: width >= 872 ? 4 : width >= 658 ? 3 : width >= 420 ? 2 : 1
    readonly property real gap: columns === 1 ? 18 : 24
    readonly property real inset: width >= 658 ? 24 : 16
    readonly property real tileWidth: Math.max(0, Math.min(280, (width - inset * 2 - gap * (columns - 1)) / columns))
    readonly property real boardLeft: (width - columns * tileWidth - (columns - 1) * gap) / 2
    readonly property int headerHeight: 54
    readonly property int toggleHeight: 34
    readonly property int stepHeight: 48
    readonly property int collapsedHeight: headerHeight + toggleHeight + 6
    readonly property int rows: Math.ceil(stages.length / columns)
    function rowHeight(row) {
        var height = collapsedHeight
        for (var i = row * columns; i < Math.min(stages.length, (row + 1) * columns); ++i)
            height = Math.max(height, stageHeight(i))
        return height
    }
    function stageY(index) {
        var y = inset
        for (var r = 0; r < Math.floor(index / columns); ++r) y += rowHeight(r) + gap
        return y
    }
    function stageX(index) {
        var col = index % columns
        if (Math.floor(index / columns) % 2) col = columns - 1 - col
        return boardLeft + col * (tileWidth + gap)
    }
    function stageHeight(index) {
        var stage = stages[index]
        return collapsedHeight + (expandedStages[stage.id] ? Math.min(5, stage.nodes.length) * stepHeight + 8 : 0)
    }
    function boardHeight() {
        var height = inset * 2
        for (var r = 0; r < rows; ++r) height += rowHeight(r) + (r ? gap : 0)
        return height
    }
    function toggleStage(stageId) {
        var next = Object.assign({}, expandedStages)
        next[stageId] = !next[stageId]
        expandedStages = next
        Qt.callLater(function() { root.revealStage(stageId) })
    }
    function ensureVisible(top, bottom) {
        var next = viewport.contentY
        if (top < next) next = top - 8
        else if (bottom > next + viewport.height) next = bottom - viewport.height + 8
        viewport.contentY = Math.max(0, Math.min(next, board.height - viewport.height))
    }
    function revealStage(stageId) {
        for (var i = 0; i < stages.length; ++i)
            if (stages[i].id === stageId) { ensureVisible(stageY(i), stageY(i) + collapsedHeight); return }
    }
    function revealNode(nodeId) {
        for (var i = 0; i < stages.length; ++i) {
            if (!stages[i].nodes.some(function(n) { return n.nodeId === nodeId })) continue
            var card = tiles.itemAt(i)
            var rect = card ? card.revealStep(nodeId) : null
            if (rect) ensureVisible(rect.y, rect.y + rect.height)
            else ensureVisible(stageY(i), stageY(i) + collapsedHeight)
            return
        }
    }
    function focusNode(nodeId) {
        revealNode(nodeId)
        for (var i = 0; i < stages.length; ++i)
            if (stages[i].nodes.some(function(n) { return n.nodeId === nodeId })) {
                var card = tiles.itemAt(i)
                if (card) card.focusStep(nodeId)
                return
            }
    }
    function stageColor(status) {
        if (status === "needs_repair" || status === "awaiting_human" || status === "interrupted") return Theme.warning
        if (status === "succeeded") return Theme.secondary
        if (status === "pending" || status === "ready" || status === "skipped" || status === "cancelled") return Theme.muted
        return W.color(status)
    }
    onStagesChanged: connectors.requestPaint()
    onExpandedStagesChanged: connectors.requestPaint()
    onWidthChanged: { connectors.requestPaint(); Qt.callLater(function() { root.revealNode(root.selectedNodeId) }) }
    Flickable {
        id: viewport
        anchors.fill: parent
        contentWidth: width; contentHeight: board.height
        clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: Controls.AppScrollBar {}
        Item {
            id: board
            width: viewport.width; height: root.boardHeight()
            Canvas {
                id: connectors
                anchors.fill: parent
                antialiasing: true
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                onPaint: {
                    var ctx = getContext("2d")
                    ctx.reset()
                    ctx.lineWidth = 1.5
                    for (var i = 0; i < root.stages.length - 1; ++i) {
                        var sameRow = Math.floor(i / root.columns) === Math.floor((i + 1) / root.columns)
                        var forward = Math.floor(i / root.columns) % 2 === 0
                        var x1 = root.stageX(i), y1 = root.stageY(i)
                        var x2 = root.stageX(i + 1), y2 = root.stageY(i + 1)
                        if (sameRow) {
                            x1 += forward ? root.tileWidth : 0; y1 += root.headerHeight / 2
                            x2 += forward ? 0 : root.tileWidth; y2 += root.headerHeight / 2
                        } else {
                            x1 += root.tileWidth / 2; y1 += root.stageHeight(i)
                            x2 += root.tileWidth / 2
                        }
                        ctx.strokeStyle = root.stages[i].status === "succeeded" ? Theme.accentBorder : Theme.border
                        ctx.fillStyle = ctx.strokeStyle
                        ctx.beginPath(); ctx.moveTo(x1, y1)
                        if (!sameRow && x1 !== x2) {
                            ctx.lineTo(x1, y2 - root.gap / 2); ctx.lineTo(x2, y2 - root.gap / 2)
                        }
                        ctx.lineTo(x2, y2); ctx.stroke()
                        ctx.beginPath(); ctx.moveTo(x2, y2)
                        if (sameRow) {
                            ctx.lineTo(x2 + (forward ? -6 : 6), y2 - 4)
                            ctx.lineTo(x2 + (forward ? -6 : 6), y2 + 4)
                        } else { ctx.lineTo(x2 - 4, y2 - 6); ctx.lineTo(x2 + 4, y2 - 6) }
                        ctx.closePath(); ctx.fill()
                    }
                }
            }
            Repeater {
                id: tiles
                model: root.stages
                delegate: Rectangle {
                    id: tile
                    required property var modelData
                    required property int index
                    readonly property bool expanded: !!root.expandedStages[modelData.id]
                    readonly property bool selected: modelData.nodes.some(function(n) { return n.nodeId === root.selectedNodeId })
                    readonly property string statusLabel: Flow.statusLabels[modelData.status] || W.label(modelData.status)
                    readonly property color statusColor: root.stageColor(modelData.status)
                    function revealStep(nodeId) {
                        if (!expanded) return null
                        for (var i = 0; i < modelData.nodes.length; ++i)
                            if (modelData.nodes[i].nodeId === nodeId) {
                                steps.positionViewAtIndex(i, ListView.Contain)
                                steps.forceLayout()
                                var item = steps.itemAtIndex(i)
                                return item ? item.mapToItem(board, 0, 0, item.width, item.height) : null
                            }
                        return null
                    }
                    function focusStep(nodeId) {
                        if (expanded) {
                            for (var i = 0; i < modelData.nodes.length; ++i) {
                                if (modelData.nodes[i].nodeId !== nodeId) continue
                                var item = steps.itemAtIndex(i)
                                if (item) { item.forceActiveFocus(Qt.OtherFocusReason); return }
                            }
                        }
                        stageButton.forceActiveFocus(Qt.OtherFocusReason)
                    }
                    x: root.stageX(index); y: root.stageY(index)
                    width: root.tileWidth; height: root.stageHeight(index)
                    radius: Theme.radiusMd
                    color: modelData.status === "running" ? Theme.accentSubtle : selected ? Theme.raised : Theme.panel
                    border.width: 1
                    border.color: modelData.status === "awaiting_human" ? Theme.warning
                        : modelData.status === "running" ? Theme.accentBorder : selected ? Theme.controlEdge : Theme.border
                    Rectangle {
                        anchors.left: parent.left; anchors.top: parent.top
                        anchors.leftMargin: Theme.spaceSm; anchors.topMargin: Theme.spaceSm
                        width: 3; height: root.headerHeight - 2 * Theme.spaceSm
                        radius: 1.5; visible: tile.selected
                        color: Theme.accent
                    }
                    Controls.ActionButton {
                        id: stageButton
                        objectName: "workflowStage"
                        property string stageId: tile.modelData.id
                        property bool expanded: tile.expanded
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                        anchors.margins: 4
                        height: root.headerHeight - 4
                        flat: true
                        horizontalPadding: 10
                        enabled: tile.modelData.nodes.length > 0
                        Accessible.selected: tile.selected
                        Accessible.name: tile.modelData.title + "，" + (tile.modelData.summary || W.label(tile.modelData.status)) + "，查看步骤详情"
                        onActiveFocusChanged: if (activeFocus) root.revealStage(tile.modelData.id)
                        onClicked: root.nodeSelected(tile.modelData.currentNodeId || tile.modelData.nodes[0].nodeId)
                        ToolTip.visible: hovered && !!tile.modelData.summary
                        ToolTip.delay: 650
                        ToolTip.text: tile.modelData.summary
                        contentItem: RowLayout {
                            spacing: 6
                            Label {
                                Layout.fillWidth: true
                                text: tile.modelData.title
                                font.pixelSize: Theme.fontSizeLg
                                font.weight: Font.DemiBold
                                color: stageButton.enabled ? Theme.text : Theme.disabledText
                                elide: Text.ElideRight
                            }
                            Rectangle {
                                readonly property bool complete: tile.modelData.status === "succeeded"
                                implicitWidth: complete ? 13 : 7
                                implicitHeight: implicitWidth
                                radius: width / 2
                                color: complete ? Theme.muted : tile.modelData.status === "pending" || tile.modelData.status === "ready" ? Theme.transparent : tile.statusColor
                                border.color: complete ? Theme.muted : tile.statusColor
                                Image {
                                    anchors.centerIn: parent
                                    width: 12; height: width
                                    visible: parent.complete
                                    source: Theme.icon("check")
                                }
                            }
                            Label {
                                text: tile.statusLabel
                                color: tile.statusColor; font.pixelSize: Theme.fontSizeXs
                            }
                        }
                    }
                    Controls.ActionButton {
                        id: expandButton
                        objectName: "workflowStageExpand"
                        property string stageId: tile.modelData.id
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: stageButton.bottom
                        anchors.leftMargin: 4; anchors.rightMargin: 4
                        height: root.toggleHeight; flat: true; horizontalPadding: 10
                        enabled: tile.modelData.nodes.length > 0
                        text: tile.modelData.nodes.length + " 个步骤"
                        Accessible.checkable: true
                        Accessible.checked: tile.expanded
                        Accessible.name: (tile.expanded ? "收起" : "展开") + text + "，" + tile.modelData.title
                        onActiveFocusChanged: if (activeFocus) root.revealStage(tile.modelData.id)
                        onClicked: root.toggleStage(tile.modelData.id)
                        contentItem: RowLayout {
                            Label { text: expandButton.text; Layout.fillWidth: true; color: Theme.muted; font.pixelSize: Theme.fontSizeSm }
                            Image { source: Theme.icon("chevron-right"); sourceSize.width: 16; sourceSize.height: 16; rotation: tile.expanded ? 90 : 0; opacity: expandButton.enabled ? 0.9 : 0.4 }
                        }
                    }
                    ListView {
                        id: steps
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: expandButton.bottom
                        anchors.topMargin: 6; anchors.bottom: parent.bottom
                        anchors.leftMargin: 8; anchors.rightMargin: 8; anchors.bottomMargin: 8
                        visible: tile.expanded; clip: true
                        model: tile.modelData.nodes
                        boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: Controls.AppScrollBar {}
                        delegate: Controls.ActionButton {
                            id: nodeButton
                            required property var modelData
                            property string nodeId: modelData.nodeId
                            readonly property var visual: Flow.visualStatus(modelData, root.artifactModel)
                            objectName: "workflowNode"
                            width: steps.width; height: root.stepHeight; flat: true; horizontalPadding: 8
                            checked: root.selectedNodeId === nodeId
                            Accessible.name: Flow.nodeTitle(modelData) + "，" + visual.label + "，查看详情"
                            onActiveFocusChanged: if (activeFocus) root.revealNode(nodeId)
                            onClicked: root.nodeSelected(nodeId)
                            contentItem: Column {
                                spacing: 2
                                Label { text: Flow.nodeTitle(nodeButton.modelData); width: parent.width; elide: Text.ElideRight; color: Theme.text; font.pixelSize: Theme.fontSizeSm }
                                Label { text: nodeButton.visual.label; color: root.stageColor(nodeButton.visual.status); font.pixelSize: Theme.fontSizeXs }
                            }
                        }
                    }
                }
            }
        }
    }
}
