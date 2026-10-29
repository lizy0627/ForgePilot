import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../panels" as Controls
import "../panels/Theme.js" as Theme

Controls.Panel {
    id: root
    required property var mission
    title: "证据"
    subtitle: mission.evidence.count + " 条"
    objectName: "evidencePanel"
    StateNotice { mission: root.mission; hasData: root.mission.evidence.count > 0; emptyText: "尚无证据。"; Layout.fillWidth: true }
    Repeater { model: root.mission.evidence
        delegate: ColumnLayout {
            required property var record
            Layout.fillWidth: true; spacing: 3
            Label { text: record.evidenceId + " · " + record.claim; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
            Label { text: (record.path || "来源：" + record.source) + (record.startLine ? ":" + record.startLine + "–" + record.endLine : ""); color: Theme.secondary; Layout.fillWidth: true; elide: Text.ElideMiddle; textFormat: Text.PlainText }
            Label { visible: !!record.reason; text: record.reason || ""; color: Theme.secondary; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        }
    }
}
