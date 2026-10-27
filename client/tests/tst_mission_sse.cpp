#include <QtTest>
#include "HttpFixture.h"
#include "SseClient.h"

namespace {
QByteArray missionEvent(qint64 sequence, qint64 revision, const QString &task = A,
                        const QString &type = "node.started")
{
    const auto id = task + ':' + QString::number(sequence);
    const QJsonObject body{{"event_id", id}, {"task_id", task}, {"task_revision", revision},
        {"timestamp", "2026-10-27T00:00:00Z"}, {"event_type", type}, {"node_id", "analyze"},
        {"actor", "analyst.v1"}, {"data", QJsonObject{{"state", "running"}}}};
    return "id: " + id.toUtf8() + "\nevent: " + type.toUtf8() + "\ndata: "
        + QJsonDocument(body).toJson(QJsonDocument::Compact) + "\n\n";
}
}

class MissionSseTests : public QObject {
    Q_OBJECT
private slots:
    void duplicateAndLateRevisionsDoNotApplyTwice()
    {
        Source source;
        source.handle = [](const Request &r) {
            Source::stream(r, missionEvent(2, 2) + missionEvent(3, 3) + missionEvent(4, 4)
                + missionEvent(4, 4) + missionEvent(5, 4) + missionEvent(6, 5), false);
        };
        SseClient client; QVERIFY(client.setBaseUrl(source.url()));
        QSignalSpy events(&client, &SseClient::eventApplied), recovery(&client, &SseClient::snapshotRequired);
        client.connectToMission(A, A + ":3", 3);
        QTRY_COMPARE(events.count(), 2);
        QCOMPARE(client.lastEventId(), A + ":6"); QCOMPARE(client.missionRevision(), 5);
        QCOMPARE(events[0][0].toString(), A + ":4"); QCOMPARE(events[1][0].toString(), A + ":6");
        QCOMPARE(recovery.count(), 0);
        QCOMPARE(source.requests.first().path, "/api/v2/missions/" + A.toUtf8() + "/events");
    }

    void gapsRequireSnapshot_data()
    {
        QTest::addColumn<QByteArray>("frames");
        QTest::newRow("out-of-order-id") << (missionEvent(3, 3) + missionEvent(2, 2));
        QTest::newRow("revision-gap") << missionEvent(2, 4);
    }
    void gapsRequireSnapshot()
    {
        QFETCH(QByteArray, frames);
        Source source; int requests = 0;
        source.handle = [&](const Request &r) {
            Source::stream(r, requests++ == 0 ? "retry: 250\n\n" + frames : missionEvent(5, 5), false);
        };
        SseClient client; client.setBaseUrl(source.url());
        QSignalSpy events(&client, &SseClient::eventApplied), recovery(&client, &SseClient::snapshotRequired);
        client.connectToMission(A, A + ":1", 1);
        QTRY_COMPARE(recovery.count(), 1);
        QCOMPARE(client.connectionState(), "recovering"); QCOMPARE(client.lastEventId(), A + ":1");
        QCOMPARE(client.missionRevision(), 1); QCOMPARE(events.count(), 0); QCOMPARE(requests, 1);
        QVERIFY(client.adoptMissionSnapshot(A, A + ":4", 4));
        QTRY_COMPARE(events.count(), 1); QCOMPARE(client.lastEventId(), A + ":5");
        QVERIFY(source.requests.last().headers.toLower().contains("last-event-id: " + A.toUtf8() + ":4"));
    }

    void disconnectRecoversWithSnapshotAndDiscardsPartialFrame()
    {
        Source source; int requests = 0;
        source.handle = [&](const Request &r) {
            if (++requests == 1)
                Source::stream(r, "retry: 250\n\n" + missionEvent(1, 1) + "id: unfinished\ndata: partial", true);
            else Source::stream(r, missionEvent(2, 2) + missionEvent(3, 3), false);
        };
        SseClient client; client.setBaseUrl(source.url());
        QSignalSpy events(&client, &SseClient::eventApplied), recovery(&client, &SseClient::snapshotRequired);
        client.connectToMission(A);
        QTRY_COMPARE(recovery.count(), 1); QCOMPARE(events.count(), 1);
        // Snapshot accounts for revision 2; replayed revision 2 is not emitted again.
        QVERIFY(client.adoptMissionSnapshot(A, A + ":2", 2));
        QTRY_COMPARE(events.count(), 2); QCOMPARE(client.missionRevision(), 3);
        QCOMPARE(events.last()[0].toString(), A + ":3");
    }

    void authoritativeSnapshotAdvancesConnectedStreamWithoutReopening()
    {
        Source source; Request stream;
        source.handle = [&](const Request &r) { stream = r; Source::stream(r, ": heartbeat\n\n", false); };
        SseClient client; client.setBaseUrl(source.url()); QSignalSpy events(&client, &SseClient::eventApplied);
        client.connectToMission(A, A + ":1", 1);
        QTRY_VERIFY(stream.socket); QVERIFY(client.adoptMissionSnapshot(A, A + ":4", 4));
        QVERIFY(!client.adoptMissionSnapshot(A, A + ":3", 3));
        QVERIFY(!client.adoptMissionSnapshot(B, B + ":7", 7));
        stream.socket->write(missionEvent(2, 2) + missionEvent(3, 3) + missionEvent(4, 4) + missionEvent(5, 5));
        QTRY_COMPARE(events.count(), 1); QCOMPARE(source.requests.count(), 1); QCOMPARE(client.missionRevision(), 5);
    }

    void invalidCursorRefreshesWithoutLoopingOnOldCursor()
    {
        Source source; int requests = 0;
        source.handle = [&](const Request &r) {
            if (++requests == 1) Source::json(r, {{"error", QJsonObject{{"code", "event_cursor_ahead"},
                {"message", "untrusted backend prose"}, {"details", QJsonArray{}}, {"task_id", A}}}}, 422);
            else Source::stream(r, missionEvent(4, 4), false);
        };
        SseClient client; client.setBaseUrl(source.url());
        QSignalSpy events(&client, &SseClient::eventApplied), recovery(&client, &SseClient::snapshotRequired);
        client.connectToMission(A, A + ":8", 3);
        QTRY_COMPARE(recovery.count(), 1);
        QVERIFY(client.adoptMissionSnapshot(A, A + ":3", 3));
        QTRY_COMPARE(events.count(), 1);
        QVERIFY(source.requests.last().headers.toLower().contains("last-event-id: " + A.toUtf8() + ":3"));
    }

    void malformedEventsAreRejected_data()
    {
        QTest::addColumn<QByteArray>("frames");
        QTest::newRow("wrong-task") << missionEvent(1, 1, B);
        auto duplicate = missionEvent(1, 1); duplicate.replace("\"task_revision\":1", "\"task_revision\":1,\"task_revision\":2");
        QTest::newRow("duplicate-json-key") << duplicate;
        auto typed = missionEvent(1, 1); typed.replace("\"task_revision\":1", "\"task_revision\":\"1\"");
        QTest::newRow("string-revision") << typed;
        auto mismatch = missionEvent(1, 1); mismatch.replace("\"event_id\":\"" + A.toUtf8() + ":1\"", "\"event_id\":\"" + A.toUtf8() + ":2\"");
        QTest::newRow("mismatched-envelope") << mismatch;
    }
    void malformedEventsAreRejected()
    {
        QFETCH(QByteArray, frames);
        Source source; source.handle = [&](const Request &r) { Source::stream(r, frames, false); };
        SseClient client; client.setBaseUrl(source.url());
        QSignalSpy events(&client, &SseClient::eventApplied), errors(&client, &SseClient::connectionError);
        client.connectToMission(A);
        QTRY_COMPARE(client.connectionState(), "error"); QCOMPARE(events.count(), 0); QVERIFY(!errors.isEmpty());
        QCOMPARE(client.missionRevision(), 0); QVERIFY(client.lastEventId().isEmpty());
    }

    void switchingDisconnectAndDestructionInvalidateCallbacks()
    {
        Source source;
        source.handle = [](const Request &r) { Source::stream(r, ": heartbeat\n\n", false); };
        SseClient client; client.setBaseUrl(source.url()); QSignalSpy events(&client, &SseClient::eventApplied);
        client.connectToMission(A); QTRY_COMPARE(source.requests.count(), 1);
        client.connectToMission(B); QTRY_COMPARE(source.requests.count(), 2);
        if (source.requests[0].socket) source.requests[0].socket->write(missionEvent(1, 1));
        source.requests[1].socket->write(missionEvent(1, 1, B));
        QTRY_COMPARE(events.count(), 1); QCOMPARE(events.first()[0].toString(), B + ":1");
        client.disconnect(); QVERIFY(client.adoptMissionSnapshot(B, B + ":2", 2));
        QCOMPARE(client.connectionState(), "disconnected");
        auto *temporary = new SseClient; temporary->setBaseUrl(source.url());
        int callbacks = 0; connect(temporary, &SseClient::eventApplied, this, [&] { ++callbacks; });
        temporary->connectToMission(A); QTRY_COMPARE(source.requests.count(), 3); delete temporary;
        if (source.requests[2].socket) source.requests[2].socket->write(missionEvent(1, 1));
        QTest::qWait(50); QCOMPARE(callbacks, 0); QCOMPARE(source.requests.count(), 3);
    }

    void deletingClientFromEventHandlerIsSafe()
    {
        Source source; source.handle = [](const Request &r) { Source::stream(r, missionEvent(1, 1) + missionEvent(2, 2), false); };
        QPointer<SseClient> client = new SseClient; client->setBaseUrl(source.url());
        connect(client, &SseClient::eventApplied, this, [client] { delete client.data(); });
        client->connectToMission(A); QTRY_VERIFY(client.isNull());
    }

    void fullyAcknowledgedTerminalStreamEnds()
    {
        Source source; source.handle = [](const Request &r) { Source::json(r, {}, 204); };
        SseClient client; client.setBaseUrl(source.url());
        QSignalSpy ended(&client, &SseClient::streamEnded), recovery(&client, &SseClient::snapshotRequired);
        client.connectToMission(A, A + ":8", 8);
        QTRY_COMPARE(ended.count(), 1); QCOMPARE(client.connectionState(), "ended"); QCOMPARE(recovery.count(), 0);
    }
};

QTEST_GUILESS_MAIN(MissionSseTests)
#include "tst_mission_sse.moc"
