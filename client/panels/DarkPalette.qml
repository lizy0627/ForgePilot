import QtQuick
import "Theme.js" as Theme

Palette {
    window: Theme.panel
    windowText: Theme.text
    base: Theme.input
    alternateBase: Theme.raised
    text: Theme.text
    button: Theme.raised
    buttonText: Theme.text
    brightText: Theme.text
    highlight: Theme.selection
    highlightedText: Theme.text
    placeholderText: Theme.muted
    light: Theme.raised
    midlight: Theme.hover
    mid: Theme.scroll
    dark: Theme.border
    shadow: Theme.shadow
    toolTipBase: Theme.raised
    toolTipText: Theme.text
    link: Theme.accent
    linkVisited: Theme.accent
    accent: Theme.accent
    disabled {
        text: Theme.disabledText
        windowText: Theme.disabledText
        buttonText: Theme.disabledText
        brightText: Theme.disabledText
        base: Theme.disabled
        button: Theme.disabled
        highlight: Theme.disabled
        highlightedText: Theme.disabledText
        placeholderText: Theme.disabledText
    }
}
