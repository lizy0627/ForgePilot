import QtQuick
import QtQuick.Controls
import "Theme.js" as Theme

DialogButtonBox {
    padding: Theme.spaceMd
    spacing: Theme.spaceSm
    delegate: ActionButton {}
    background: Item {}
}
