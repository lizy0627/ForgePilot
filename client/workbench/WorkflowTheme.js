.pragma library
.import "../panels/Theme.js" as Base

// Neutral workspace surfaces keep the shared accent and status colors distinct.
function token(name, fallback) { return Base[name] === undefined ? fallback : Base[name]; }
var background = "#191a1b";
var panel = "#222324";
var sidebar = "#151617";
var raised = "#2b2d2e";
var input = "#202122";
var border = "#3a3d3e";
var text = "#f2f2ef";
var secondary = "#c4c6c5";
var muted = "#a1a5a3";
var hover = "#303233";
var selection = Base.selection;
var warning = Base.warning;
var dangerFill = Base.dangerFill;
var accentBorder = token("accentBorder", Base.accent);
var accentSubtle = token("accentSubtle", Base.raised);
var focusRing = token("focusRing", Base.accent);
var fontFamily = token("fontFamily", "Microsoft YaHei UI");
var fontMono = token("fontMono", "Consolas");
var fontSizeXs = token("fontSizeXs", 11);
var fontSizeSm = token("fontSizeSm", 12);
var fontSizeMd = token("fontSizeMd", 14);
var fontSizeLg = token("fontSizeLg", 17);
var fontSizeXl = token("fontSizeXl", 22);
var spaceXs = token("spaceXs", 4);
var spaceSm = token("spaceSm", 8);
var spaceMd = token("spaceMd", 12);
var spaceLg = token("spaceLg", 16);
var spaceXl = token("spaceXl", 24);
var radiusSm = token("radiusSm", 6);
var radiusMd = token("radiusMd", 10);
var radiusLg = token("radiusLg", 14);
var controlHeight = token("controlHeight", 38);
