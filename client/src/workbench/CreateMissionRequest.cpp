#include "CreateMissionRequest.h"
#include "MissionProtocol.h"
#include <QJsonArray>
#include <QRegularExpression>
#include <QSet>
#include <algorithm>

namespace {
// The schema is the exported CreateMission request, with SelectedSkill inlined.
bool matchesSchema(const QJsonValue &value, const QJsonObject &schema)
{
    if (schema.contains("anyOf")) {
        for (const auto &option : schema.value("anyOf").toArray())
            if (matchesSchema(value, option.toObject())) return true;
        return false;
    }
    const auto type = schema.value("type").toString();
    if (type == "object") {
        if (!value.isObject()) return false;
        const auto object = value.toObject();
        for (const auto &name : schema.value("required").toArray())
            if (!object.contains(name.toString())) return false;
        const auto fields = schema.value("properties").toObject();
        for (auto it = object.begin(); it != object.end(); ++it)
            if (!matchesSchema(it.value(), fields.value(it.key()).toObject())) return false;
        return true;
    }
    if (type == "array") {
        if (!value.isArray()) return false;
        const auto array = value.toArray();
        if (schema.contains("maxItems") && array.size() > schema.value("maxItems").toInt()) return false;
        const auto item = schema.value("items").toObject();
        for (const auto &entry : array) if (!matchesSchema(entry, item)) return false;
        return true;
    }
    if (type == "boolean") return value.isBool();
    if (type == "null") return value.isNull();
    if (type == "integer") {
        // SelectedSkill.version is bounded by 2^63 - 2. OpenAPI rounds that
        // maximum to a double, so keep the exact check used by the backend.
        return MissionProtocol::validRevision(value)
            && value.toInteger() >= schema.value("minimum").toDouble()
            && value.toInteger() <= schema.value("maximum").toDouble();
    }
    if (type != "string" || !value.isString()) return false;
    const auto string = value.toString();
    if (!string.isValidUtf16()) return false;
    const auto length = string.toUcs4().size();
    if (length < schema.value("minLength").toInt()
        || (schema.contains("maxLength") && length > schema.value("maxLength").toInt())
        || (schema.contains("const") && value != schema.value("const"))
        || (schema.contains("enum") && !schema.value("enum").toArray().contains(value))) return false;
    if (!schema.contains("pattern")) return true;
    auto pattern = schema.value("pattern").toString();
    // Pydantic's end anchor rejects a trailing newline. PCRE's $ permits one.
    if (pattern.endsWith('$')) pattern.replace(pattern.size() - 1, 1, "\\z");
    return QRegularExpression(pattern).match(string).hasMatch();
}

bool control(QChar ch) { return ch.unicode() < 32 || ch.unicode() == 127; }
bool pythonSpace(QChar ch)
{
    return ch.isSpace() || (ch.unicode() >= 0x1c && ch.unicode() <= 0x1f);
}

bool localAbsolutePath(const QString &path)
{
    if (path.startsWith("\\\\") || path.startsWith("//") || std::any_of(path.begin(), path.end(), control))
        return false;
    const auto normalized = QString(path).replace('\\', '/');
    const auto parts = normalized.split('/');
    if (parts.contains(".") || parts.contains("..")) return false;
    bool absolute = path.startsWith('/');
    qsizetype driveEnd = 0;
    if (normalized.startsWith("//")) {
        // Mixed leading separators survive the raw-prefix check above.
        // PureWindowsPath includes the server and share in the drive.
        const bool uncDevice = normalized.startsWith("//?/UNC/", Qt::CaseInsensitive);
        const auto serverStart = uncDevice ? 8 : 2;
        const auto serverEnd = normalized.indexOf('/', serverStart);
        const auto shareEnd = serverEnd < 0 ? -1 : normalized.indexOf('/', serverEnd + 1);
        driveEnd = shareEnd < 0 ? normalized.size() : shareEnd;
        const bool rootedShare = serverEnd >= 0 && !normalized.endsWith('/')
            && (uncDevice || !QStringLiteral("?.").contains(parts.at(2)));
        absolute |= shareEnd >= 0 || rootedShare;
    } else if (!normalized.startsWith('/') && !normalized.isEmpty()) {
        // pathlib recognizes any single Unicode scalar followed by a colon.
        const auto colon = normalized.front().isHighSurrogate() ? 2 : 1;
        if (normalized.size() > colon && normalized[colon] == ':') {
            driveEnd = colon + 1;
            absolute = normalized.size() > driveEnd && normalized[driveEnd] == '/';
        }
    }
    return absolute && normalized.indexOf(':', driveEnd) < 0;
}

bool uniqueStrings(const QJsonArray &entries, const QString &key = {})
{
    QSet<QString> seen;
    for (const auto &entry : entries) {
        const auto id = key.isEmpty() ? entry.toString() : entry.toObject().value(key).toString();
        if (seen.contains(id)) return false;
        seen.insert(id);
    }
    return true;
}
}

bool validCreateMissionRequest(const QJsonObject &body)
{
    if (!matchesSchema(body, CreateMissionContract::schema())) return false;

    // CreateMission's field validators add these rules to its OpenAPI schema.
    if (!localAbsolutePath(body.value("repository_path").toString())) return false;
    const auto request = body.value("request").toString();
    if (request.contains(QChar::Null) || std::all_of(request.begin(), request.end(), pythonSpace)) return false;
    const auto ref = body.value("base_ref").toString();
    if (ref.startsWith('-') || ref.contains('\\') || ref.contains(':')) return false;
    for (const auto ch : ref) if (control(ch) || pythonSpace(ch)) return false;

    const auto paths = body.value("baseline_paths").toArray();
    for (const auto &entry : paths) {
        const auto path = entry.toString();
        if (path.isEmpty() || path.contains(QChar::Null) || path.toUcs4().size() > 4096) return false;
    }
    return uniqueStrings(paths) && uniqueStrings(body.value("mcp_server_ids").toArray())
        && uniqueStrings(body.value("selected_skills").toArray(), "skill_id");
}
