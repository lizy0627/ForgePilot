#include <QtTest>
#include <QFile>
#include "HttpFixture.h"
#include "workbench/MissionProtocol.h"
#include "workbench/MissionSnapshotLoader.h"

namespace {
QJsonObject mission(const QString &task = A, qint64 revision = 8)
{
    QFile file(QString(FORGEPILOT_SOURCE_DIR) + "/protocol/examples.json");
    if (!file.open(QIODevice::ReadOnly)) qFatal("Cannot open canonical protocol examples");
    auto body = QJsonDocument::fromJson(file.readAll()).object().value("mission_query_response").toObject();
    body.insert("task_id", task); body.insert("current_revision", revision);
    body.insert("last_event_id", task + ':' + QString::number(revision));
    auto review = body.value("review").toObject();
    review.insert("task_id", task); review.insert("revision", revision);
    body.insert("review", review);
    return body;
}
QJsonObject graph(const QJsonObject &m)
{
    QJsonObject node{{"node_id", "execution_approval"}, {"node_type", "human_gate"},
        {"status", m.value("status")}, {"attempt", 1}, {"result", QJsonValue::Null}};
    QJsonObject definition{{"node_id", "execution_approval"}, {"node_type", "human_gate"},
        {"handler", "execution_approval"}, {"dependencies", QJsonArray{}}, {"agent_id", QJsonValue::Null},
        {"agent_definition_hash", QJsonValue::Null}, {"settings", QJsonObject{}}};
    return {{"task_id", m.value("task_id")}, {"graph_id", m.value("graph_id")}, {"graph_version", 1},
        {"definition_hash", QString(64, 'a')}, {"status", m.value("status")}, {"current_revision", m.value("current_revision")},
        {"nodes", QJsonArray{node}}, {"definition", QJsonObject{{"graph_id", m.value("graph_id")}, {"version", 1},
        {"nodes", QJsonArray{definition}}, {"edges", QJsonArray{}}}}};
}
QJsonObject error(const QString &code)
{
    return {{"error", QJsonObject{{"code", code}, {"message", "Fixture failure"},
        {"details", QJsonArray{}}, {"task_id", QJsonValue::Null}}}};
}
struct Fixture {
    Source source;
    QJsonObject current = mission();
    std::function<bool(const Request &)> intercept;
    Fixture()
    {
        source.handle = [this](const Request &r) {
            if (!intercept || !intercept(r)) respond(r);
        };
    }
    void respond(const Request &r)
    {
        const auto task = current.value("task_id");
        if (r.path.endsWith("/graph")) Source::json(r, graph(current));
        else if (r.path.endsWith("/artifacts")) Source::json(r, {{"task_id", task}, {"items", QJsonArray{}}});
        else if (r.path.endsWith("/diff")) Source::json(r, {{"task_id", task}, {"patch", "fixture patch"}, {"change_sets", QJsonArray{}}});
        else if (r.path.endsWith("/validation")) Source::json(r, {{"task_id", task}, {"items", QJsonArray{}}, {"latest", QJsonValue::Null}});
        else Source::json(r, current);
    }
    int missionReads(const QString &task = A) const
    {
        int count = 0;
        for (const auto &r : source.requests)
            if (r.path == "/api/v2/missions/" + task.toUtf8()) ++count;
        return count;
    }
};
MissionSnapshot bundle(const QSignalSpy &spy, int index = 0)
{ return spy.at(index).at(0).value<MissionSnapshot>(); }
}

class MissionSnapshotTests : public QObject {
    Q_OBJECT
private slots:
    void initTestCase()
    {
        qRegisterMetaType<MissionSnapshot>();
        QVERIFY(MissionProtocol::validMission(mission()));
        QVERIFY(MissionProtocol::validGraph(graph(mission())));
    }
    void fenceWaitsForEveryProjectionAndCoalescesRefreshes()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        QList<Request> projections;
        f.intercept = [&](const Request &r) {
            if (r.path == "/api/v2/missions/" + A.toUtf8()) return false;
            projections.append(r); return true;
        };
        loader.selectTask(A); loader.refresh(10); loader.refresh(); loader.refresh();
        QTRY_COMPARE(projections.size(), 4);
        loader.refresh(); loader.refresh();
        QCOMPARE(f.missionReads(), 1); QCOMPARE(ready.count(), 0);
        for (int i = 0; i < 3; ++i) f.respond(projections.at(i));
        QTest::qWait(30); QCOMPARE(f.missionReads(), 1); QCOMPARE(ready.count(), 0);
        f.respond(projections.last());
        QTRY_COMPARE(ready.count(), 1);
        QCOMPARE(f.source.requests.size(), 6); QCOMPARE(f.missionReads(), 2);
        QCOMPARE(f.source.requests.first().path, "/api/v2/missions/" + A.toUtf8());
        QCOMPARE(f.source.requests.last().path, f.source.requests.first().path);
        const auto loaded = bundle(ready);
        QCOMPARE(loaded.mission, f.current); QCOMPARE(loaded.graph, graph(f.current));
        QCOMPARE(loaded.artifacts.value("task_id").toString(), A);
        QCOMPARE(loaded.diff.value("patch").toString(), "fixture patch");
        QCOMPARE(loaded.validation.value("task_id").toString(), A);
    }
    void inconsistentFenceDiscardsTheWholeBundle_data()
    {
        QTest::addColumn<QString>("change");
        QTest::newRow("mission-revision") << QString("mission");
        QTest::newRow("graph-revision") << QString("graph");
        QTest::newRow("cursor-with-same-revision") << QString("cursor");
    }
    void inconsistentFenceDiscardsTheWholeBundle()
    {
        QFETCH(QString, change);
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        bool changed = false;
        f.intercept = [&](const Request &r) {
            if (changed || !r.path.endsWith("/graph")) return false;
            changed = true;
            const auto oldGraph = graph(f.current);
            if (change == "mission") f.current = mission(A, 9);
            if (change == "cursor") f.current.insert("last_event_id", A + ":9");
            Source::json(r, change == "graph" ? graph(mission(A, 7)) : oldGraph);
            return true;
        };
        loader.selectTask(A); loader.refresh();
        QTRY_COMPARE(ready.count(), 1);
        QCOMPARE(f.missionReads(), 4);
        QCOMPARE(bundle(ready).mission, f.current);
        QCOMPARE(bundle(ready).graph, graph(f.current));
        QTest::qWait(40); QCOMPARE(ready.count(), 1);
    }
    void observedRevisionRejectsAStableButOutdatedBundle()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        Request held; bool hold = true;
        f.intercept = [&](const Request &r) {
            if (hold && r.path.endsWith("/graph")) { held = r; return true; }
            if (f.missionReads() == 2 && r.path == "/api/v2/missions/" + A.toUtf8()) {
                // The first fence is still internally consistent at revision 8.
                Source::json(r, f.current); f.current = mission(A, 9); return true;
            }
            return false;
        };
        loader.selectTask(A); loader.refresh(); QTRY_VERIFY(held.socket);
        loader.observeRevision(9); loader.observeRevision(7);
        hold = false; Source::json(held, graph(f.current));
        QTRY_COMPARE(ready.count(), 1);
        QCOMPARE(bundle(ready).mission.value("current_revision").toInteger(), 9);
        QCOMPARE(f.missionReads(), 4);
    }
    void switchingTaskCancelsOldReadsAndResetsTheRevisionFloor()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        Request held;
        f.intercept = [&](const Request &r) {
            if (r.path == "/api/v2/missions/" + A.toUtf8() + "/graph") { held = r; return true; }
            return false;
        };
        loader.selectTask(A); loader.observeRevision(30); loader.refresh(); QTRY_VERIFY(held.socket);
        f.current = mission(B, 2); loader.selectTask(B); loader.refresh();
        QTRY_COMPARE(ready.count(), 1);
        Source::json(held, graph(mission(A, 30))); QTest::qWait(50);
        QCOMPARE(ready.count(), 1); QCOMPARE(bundle(ready).mission, f.current);
        QCOMPARE(bundle(ready).graph.value("task_id").toString(), B);
    }
    void invalidationDiscardsAnOldFenceEvenForTheSameTask()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        Request held;
        f.intercept = [&](const Request &r) {
            if (r.path == "/api/v2/missions/" + A.toUtf8() && f.missionReads() == 2) { held = r; return true; }
            return false;
        };
        loader.selectTask(A); loader.refresh(); QTRY_VERIFY(held.socket);
        loader.invalidate(9); f.current = mission(A, 9); loader.refresh();
        QTRY_COMPARE(ready.count(), 1);
        Source::json(held, mission()); QTest::qWait(50);
        QCOMPARE(ready.count(), 1); QCOMPARE(bundle(ready).mission, f.current);
    }
    void invalidationAndEmptySelectionCancelScheduledReads()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        loader.selectTask(A); loader.refresh(20); loader.invalidate();
        QTest::qWait(40); QCOMPARE(f.source.requests.size(), 0);
        loader.refresh(20); loader.selectTask({}); loader.refresh();
        QTest::qWait(40); QCOMPARE(f.source.requests.size(), 0); QCOMPARE(ready.count(), 0);
    }
    void onlyTransientReadErrorsRetry_data()
    {
        QTest::addColumn<int>("status"); QTest::addColumn<bool>("retry"); QTest::addColumn<QString>("code");
        QTest::newRow("network") << 0 << true << QString("network_error");
        QTest::newRow("rate-limit") << 429 << true << QString("task_capacity");
        QTest::newRow("server") << 503 << true << QString("mission_internal");
        QTest::newRow("missing") << 404 << false << QString("unknown_mission");
        QTest::newRow("malformed-projection") << 200 << false << QString("invalid_response");
    }
    void onlyTransientReadErrorsRetry()
    {
        QFETCH(int, status); QFETCH(bool, retry); QFETCH(QString, code);
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        QSignalSpy failed(&loader, &MissionSnapshotLoader::loadFailed);
        bool fail = true;
        f.intercept = [&](const Request &r) {
            if (!fail || !r.path.endsWith("/graph")) return false;
            if (status == 0) r.socket->disconnectFromHost();
            else Source::json(r, status == 200 ? QJsonObject{{"task_id", A}} : error(code), status);
            return true;
        };
        connect(&loader, &MissionSnapshotLoader::loadFailed, this, [&] { fail = false; });
        loader.selectTask(A); loader.refresh(); QTRY_COMPARE(failed.count(), 1);
        QCOMPARE(failed.first().at(0).toInt(), status); QCOMPARE(failed.first().at(1).toString(), code);
        if (retry) {
            QTRY_COMPARE(ready.count(), 1); QCOMPARE(bundle(ready).mission, f.current);
        } else {
            const auto count = f.source.requests.size();
            QTest::qWait(400); QCOMPARE(ready.count(), 0); QCOMPARE(f.source.requests.size(), count);
            loader.refresh(); QTRY_COMPARE(ready.count(), 1);
        }
        for (const auto &r : f.source.requests) QCOMPARE(r.method, QByteArray("GET"));
    }
    void destructionDetachesOnlyTheLoadersRequests()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        Request heldGraph, heldMutation;
        f.intercept = [&](const Request &r) {
            if (r.path.endsWith("/graph")) { heldGraph = r; return true; }
            if (r.path.endsWith("/cancel")) { heldMutation = r; return true; }
            return false;
        };
        auto *loader = new MissionSnapshotLoader(&api);
        loader->selectTask(A); loader->refresh(); QTRY_VERIFY(heldGraph.socket);
        QSignalSpy responses(&api, &AgentApiClient::responseReceived);
        const auto mutation = api.cancelMission(A, 8); QTRY_VERIFY(heldMutation.socket);
        QPointer<MissionSnapshotLoader> guard(loader); delete loader; QVERIFY(!guard);
        QTRY_VERIFY(!heldGraph.socket || heldGraph.socket->state() == QAbstractSocket::UnconnectedState);
        QVERIFY(heldMutation.socket); QCOMPARE(heldMutation.socket->state(), QAbstractSocket::ConnectedState);
        Source::json(heldMutation, mission(A, 9));
        QTRY_VERIFY([&] {
            for (const auto &response : responses) if (response.at(0).toULongLong() == mutation) return true;
            return false;
        }());
    }
    void readySignalCanDestroyTheLoader()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        QPointer<MissionSnapshotLoader> loader = new MissionSnapshotLoader(&api);
        connect(loader, &MissionSnapshotLoader::snapshotReady, this, [loader](const MissionSnapshot &loaded) {
            QCOMPARE(loaded.mission.value("task_id").toString(), A);
            delete loader.data();
        });
        loader->selectTask(A); loader->refresh(); QTRY_VERIFY(loader.isNull());
    }
    void readySignalCanSwitchTaskAndStartAnotherLoad()
    {
        Fixture f; AgentApiClient api; QVERIFY(api.setBaseUrl(f.source.url()));
        MissionSnapshotLoader loader(&api); QSignalSpy ready(&loader, &MissionSnapshotLoader::snapshotReady);
        connect(&loader, &MissionSnapshotLoader::snapshotReady, this, [&](const MissionSnapshot &loaded) {
            if (loaded.mission.value("task_id").toString() != A) return;
            f.current = mission(B, 2); loader.selectTask(B); loader.refresh();
        });
        loader.selectTask(A); loader.refresh(); QTRY_COMPARE(ready.count(), 2);
        QCOMPARE(bundle(ready, 0).mission.value("task_id").toString(), A);
        QCOMPARE(bundle(ready, 1).mission, f.current); QCOMPARE(f.missionReads(B), 2);
    }
};
QTEST_GUILESS_MAIN(MissionSnapshotTests)
#include "tst_mission_snapshot.moc"
