.pragma library
.import "../panels/Theme.js" as Base

// Use the shared palette. Defaults keep this view compatible with installations
// whose palette predates the shared typography and spacing tokens.
function token(name, fallback) { return Base[name] === undefined ? fallback : Base[name]; }
var background = Base.background;
var panel = Base.panel;
var sidebar = Base.sidebar;
var raised = Base.raised;
var input = Base.input;
var border = Base.border;
var text = Base.text;
var secondary = Base.secondary;
var muted = Base.muted;
var hover = Base.hover;
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
