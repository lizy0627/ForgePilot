pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

ComboBox {
    id: control
    hoverEnabled: true
    font.family: Theme.fontFamily
    font.pixelSize: Theme.fontSizeMd
    leftPadding: Theme.spaceMd
    rightPadding: Theme.spaceXl + Theme.spaceMd
    topPadding: Theme.spaceSm
    bottomPadding: Theme.spaceSm

    contentItem: Label {
        text: control.displayText
        font: control.font
        color: control.enabled ? Theme.text : Theme.disabledText
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: Label {
        x: control.width - width - Theme.spaceMd
        y: (control.height - height) / 2
        text: "⌄"
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLg
        color: control.enabled ? Theme.silver : Theme.disabledText
    }
    background: Rectangle {
        implicitWidth: 180
        implicitHeight: Theme.controlHeight
        radius: Theme.radiusMd
        color: control.enabled ? Theme.input : Theme.disabled
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.focusRing
            : control.hovered ? Theme.accentBorder : Theme.border
    }
    delegate: ItemDelegate {
        id: option
        required property int index
        width: control.width - Theme.spaceXs * 2
        text: control.textAt(index)
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeMd
        palette.text: Theme.text
        highlighted: control.highlightedIndex === index
        background: Rectangle {
            radius: Theme.radiusSm
            color: option.highlighted ? Theme.accentSubtle : option.hovered ? Theme.hover : Theme.transparent
        }
        onClicked: {
            control.currentIndex = index
            control.activated(index)
            control.popup.close()
        }
    }
    popup: Popup {
        y: control.height + Theme.spaceXs
        width: control.width
        implicitHeight: Math.min(contentItem.implicitHeight + topPadding + bottomPadding, 280)
        padding: Theme.spaceXs
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.delegateModel
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
        background: Rectangle {
            color: Theme.raised
            border.color: Theme.accentBorder
            radius: Theme.radiusMd
        }
    }
}
