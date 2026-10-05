#pragma once
#include <QByteArray>
#include <QString>
#include <QVector>

struct SseFrame {
    QString event;
    QString id;
    QByteArray data;
    int retry = -1;
};
// Parse complete lines as bytes; UTF-8 is decoded only after the full field arrives.
class SseParser {
public:
    explicit SseParser(qsizetype maxEventBytes = 32 * 1024 * 1024) : m_maxEventBytes(maxEventBytes) {}
    QVector<SseFrame> feed(const QByteArray &bytes);
    void reset(); // Discards unterminated data on EOF/reconnect.
    QString error() const { return m_error; }
private:
    void line(QVector<SseFrame> &frames);
    QByteArray m_line, m_data;
    QString m_event, m_id, m_error;
    bool m_skipLf = false, m_firstLine = true;
    qsizetype m_eventBytes = 0;
    qsizetype m_maxEventBytes;
};
