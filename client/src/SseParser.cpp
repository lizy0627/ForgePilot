#include "SseParser.h"
#include <algorithm>
#include <utility>

void SseParser::reset() { *this = SseParser{m_maxEventBytes}; }
QVector<SseFrame> SseParser::feed(const QByteArray &bytes)
{
    QVector<SseFrame> frames;
    for (char c : bytes) {
        if (!m_error.isEmpty()) break;
        if (m_skipLf) { m_skipLf = false; if (c == '\n') continue; }
        if (c == '\r' || c == '\n') { line(frames); m_skipLf = c == '\r'; }
        else {
            m_line += c;
            if (m_line.size() + m_eventBytes > m_maxEventBytes) m_error = "单个事件超过安全上限。";
        }
    }
    return frames;
}
void SseParser::line(QVector<SseFrame> &frames)
{
    auto bytes = std::exchange(m_line, {});
    if (m_firstLine) { m_firstLine = false; if (bytes.startsWith("\xEF\xBB\xBF")) bytes.remove(0, 3); }
    if (bytes.isEmpty()) {
        if (!m_data.isEmpty()) {
            m_data.chop(1);
            frames.append({m_event.isEmpty() ? "message" : m_event, m_id, m_data});
        }
        m_data.clear(); m_event.clear(); m_eventBytes = 0;
        return;
    }
    if (bytes.startsWith(':')) return;
    const auto colon = bytes.indexOf(':');
    const auto name = colon < 0 ? bytes : bytes.left(colon);
    auto value = colon < 0 ? QByteArray{} : bytes.mid(colon + 1);
    if (value.startsWith(' ')) value.remove(0, 1);
    m_eventBytes += bytes.size();
    if (name == "data") { m_data += value; m_data += '\n'; }
    else if (name == "event") m_event = QString::fromUtf8(value);
    else if (name == "id" && !value.contains('\0')) m_id = QString::fromUtf8(value);
    else if (name == "retry" && !value.isEmpty()
             && std::all_of(value.begin(), value.end(), [](char c) { return c >= '0' && c <= '9'; })) {
        bool ok = false;
        const auto retry = value.toLongLong(&ok);
        if (ok) frames.append({{}, {}, {}, int(std::clamp<qint64>(retry, 250, 30000))});
    }
}
