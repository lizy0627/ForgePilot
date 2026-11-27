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
    indicator: Image {
        x: control.width - width - Theme.spaceMd
        y: (control.height - height) / 2
        width: 16
        height: 16
        opacity: control.enabled ? 1 : 0.45
        source: Theme.icon("chevron-down")
    }
    background: Rectangle {
        implicitWidth: 180
        implicitHeight: Theme.controlHeight
        radius: Theme.radiusMd
        color: !control.enabled ? Theme.disabled : control.down ? Theme.pressed : control.hovered ? Theme.raised : Theme.input
        border.width: control.activeFocus ? 2 : 1
        border.color: !control.enabled ? Theme.disabled : control.activeFocus ? Theme.focusRing
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
        hoverEnabled: true
        highlighted: control.highlightedIndex === index
        readonly property string styleRole: control.currentIndex === index ? "selected" : "flat"
        background: Rectangle {
            radius: Theme.radiusSm
            color: option.highlighted && !option.hovered && !option.down ? Theme.accentSubtle : Theme.buttonFill(option, option.styleRole)
            border.width: option.visualFocus || option.down ? 2 : 1
            border.color: Theme.buttonBorder(option, option.styleRole)
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
            ScrollBar.vertical: AppScrollBar {}
        }
        background: Rectangle {
            color: Theme.raised
            border.color: Theme.accentBorder
            radius: Theme.radiusMd
        }
    }
}
