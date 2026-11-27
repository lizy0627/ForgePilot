.pragma library
// Shared color, type and geometry values. Text tokens meet at least 4.5:1
// against the principal dark surfaces.
var background = "#1a1c1e";
var panel = "#232628";
var sidebar = "#151719";
var raised = "#303436";
var input = "#1c1f21";
var border = "#44494c";
var text = "#edf1ef";
var secondary = "#c3cbc8";
var muted = "#9da8a4";
var disabledText = "#8e9994";
var disabled = "#292d2e";
var hover = "#41494a";
var pressed = "#222728";
var accent = "#9ed7bd";
var accentFill = "#99d2b7";
var accentHover = "#b9efd3";
var accentPressed = "#76b396";
var accentSubtle = "#2b4038";
var accentBorder = "#638976";
var onAccent = "#122b20";
var selection = "#385546";
var success = "#9fd4b5";
var danger = "#f4b3ab";
var dangerFill = "#442d2c";
var dangerHover = "#5d3b36";
var warning = "#eac690";
var purple = "#c4b6dc";
var silver = "#d7dedb";
var greenMuted = "#adcbbd";
var focusRing = "#bcf1db";
var shadow = "#0c0d0e";
var scroll = "#697570";
var transparent = "transparent";

var fontFamily = "Microsoft YaHei UI";
var fontMono = "Consolas";
var fontSizeXs = 11;
var fontSizeSm = 12;
var fontSizeMd = 14;
var fontSizeLg = 17;
var fontSizeXl = 22;

var spaceXs = 4;
var spaceSm = 8;
var spaceMd = 12;
var spaceLg = 16;
var spaceXl = 24;
var radiusSm = 6;
var radiusMd = 7;
var radiusLg = 12;
var controlHeight = 38;

var dangerPressed = "#392321";
var dangerBorder = "#935f58";
var selectedHover = "#39564a";
var selectedPressed = "#23372e";
var controlEdge = "#58615f";
var brandMark = Qt.resolvedUrl("../assets/forge-mark.svg");

function icon(name) {
    return Qt.resolvedUrl("../assets/fluent/" + name + ".svg");
}

function buttonFill(control, role) {
    if (!control.enabled) return disabled;
    if (role === "primary") return control.down ? accentPressed : control.hovered ? accentHover : accentFill;
    if (role === "danger") return control.down ? dangerPressed : control.hovered ? dangerHover : dangerFill;
    if (role === "selected") return control.down ? selectedPressed : control.hovered ? selectedHover : accentSubtle;
    if (control.down) return pressed;
    if (control.hovered) return hover;
    return role === "flat" ? transparent : raised;
}

function buttonBorder(control, role) {
    if (!control.enabled) return transparent;
    if (control.visualFocus) return focusRing;
    if (control.down) return role === "danger" ? danger : accent;
    if (role === "selected") return accentBorder;
    if (role === "danger") return dangerBorder;
    if (control.hovered) return role === "primary" ? focusRing : controlEdge;
    return role === "secondary" ? border : transparent;
}
