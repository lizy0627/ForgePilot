pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "WorkflowTheme.js" as Theme
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
    readonly property int columns: width >= 1000 ? 4 : width >= 720 ? 3 : width >= 460 ? 2 : 1
    readonly property real gap: 32
    readonly property real inset: 20
    readonly property real tileWidth: Math.max(140, (viewport.width - inset * 2 - gap * (columns - 1)) / columns)
    readonly property int rows: Math.ceil(stages.length / columns)
    function rowHeight(row) {
        var height = 150
        for (var i = row * columns; i < Math.min(stages.length, (row + 1) * columns); ++i)
            if (expandedStages[stages[i].id]) height = Math.max(height, 150 + Math.min(300, stages[i].nodes.length * 58) + 12)
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
        return inset + col * (tileWidth + gap)
    }
    function stageHeight(index) {
        var stage = stages[index]
        return 150 + (expandedStages[stage.id] ? Math.min(300, stage.nodes.length * 58) + 12 : 0)
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
            if (stages[i].id === stageId) { ensureVisible(stageY(i), stageY(i) + 145); return }
    }
    function revealNode(nodeId) {
        for (var i = 0; i < stages.length; ++i) {
            if (!stages[i].nodes.some(function(n) { return n.nodeId === nodeId })) continue
            var card = tiles.itemAt(i)
            var rect = card ? card.revealStep(nodeId) : null
            if (rect) ensureVisible(rect.y, rect.y + rect.height)
            else ensureVisible(stageY(i), stageY(i) + 145)
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
    function stageColor(status) { return status === "needs_repair" ? Theme.warning : W.color(status === "ready" ? "pending" : status) }
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
                            x1 += forward ? root.tileWidth : 0; y1 += 60
                            x2 += forward ? 0 : root.tileWidth; y2 += 60
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
                    radius: Theme.radiusLg
                    color: selected ? Theme.accentSubtle : Theme.panel
                    border.width: selected ? 2 : 1
                    border.color: selected ? Theme.accentBorder : Theme.border
                    Controls.ActionButton {
                        id: stageButton
                        objectName: "workflowStage"
                        property string stageId: tile.modelData.id
                        property bool expanded: tile.expanded
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                        anchors.margins: 4
                        height: 108
                        flat: true
                        enabled: tile.modelData.nodes.length > 0
                        Accessible.name: tile.modelData.title + "，" + (tile.modelData.summary || W.label(tile.modelData.status)) + "，查看步骤详情"
                        onActiveFocusChanged: if (activeFocus) root.revealStage(tile.modelData.id)
                        onClicked: root.nodeSelected(tile.modelData.currentNodeId || tile.modelData.nodes[0].nodeId)
                        contentItem: ColumnLayout {
                            spacing: Theme.spaceSm
                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: (tile.index < 9 ? "0" : "") + (tile.index + 1); color: Theme.muted; font.pixelSize: Theme.fontSizeSm }
                                Item { Layout.fillWidth: true }
                                Label {
                                    text: Flow.statusLabels[tile.modelData.status] || W.label(tile.modelData.status)
                                    color: root.stageColor(tile.modelData.status); font.pixelSize: Theme.fontSizeSm
                                }
                            }
                            Label { text: tile.modelData.title; color: Theme.text; font.pixelSize: Theme.fontSizeLg; font.weight: Font.DemiBold; Layout.fillWidth: true }
                            Label {
                                text: tile.modelData.summary || "等待前置步骤"
                                color: Theme.secondary; font.pixelSize: Theme.fontSizeSm
                                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                                maximumLineCount: 2; elide: Text.ElideRight; textFormat: Text.PlainText
                            }
                        }
                    }
                    Controls.ActionButton {
                        objectName: "workflowStageExpand"
                        property string stageId: tile.modelData.id
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: stageButton.bottom
                        anchors.leftMargin: 8; anchors.rightMargin: 8
                        height: 32; flat: true
                        text: (tile.expanded ? "收起" : "展开") + " " + tile.modelData.nodes.length + " 个步骤"
                        Accessible.name: text + "，" + tile.modelData.title
                        onActiveFocusChanged: if (activeFocus) root.revealStage(tile.modelData.id)
                        onClicked: root.toggleStage(tile.modelData.id)
                    }
                    ListView {
                        id: steps
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: stageButton.bottom
                        anchors.topMargin: 36; anchors.bottom: parent.bottom
                        anchors.leftMargin: 8; anchors.rightMargin: 8; anchors.bottomMargin: 8
                        visible: tile.expanded; clip: true
                        model: tile.modelData.nodes
                        spacing: 4
                        boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: Controls.AppScrollBar {}
                        delegate: Controls.ActionButton {
                            id: nodeButton
                            required property var modelData
                            property string nodeId: modelData.nodeId
                            readonly property var visual: Flow.visualStatus(modelData, root.artifactModel)
                            objectName: "workflowNode"
                            width: steps.width; height: 54; flat: true
                            checked: root.selectedNodeId === nodeId
                            Accessible.name: Flow.nodeTitle(modelData) + "，" + visual.label + "，查看详情"
                            onActiveFocusChanged: if (activeFocus) root.revealNode(nodeId)
                            onClicked: root.nodeSelected(nodeId)
                            contentItem: ColumnLayout {
                                spacing: 3
                                Label { text: Flow.nodeTitle(nodeButton.modelData); Layout.fillWidth: true; elide: Text.ElideRight; color: Theme.text; font.pixelSize: Theme.fontSizeSm }
                                Label { text: nodeButton.visual.label; color: nodeButton.visual.needsRepair ? Theme.warning : W.color(nodeButton.modelData.status); font.pixelSize: Theme.fontSizeXs }
                            }
                        }
                    }
                }
            }
        }
    }
}
