import engine
import wfg


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows, fail_release=False):
        self.rows = rows
        self.fail_release = fail_release
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if self.fail_release and "ReleaseJobResources" in query:
            raise RuntimeError("rollback failed")
        if "SELECT job_id, EXTRACT" in query:
            return FakeResult(self.rows)
        return FakeResult()


class FakeDatabase:
    def __init__(self, rows, fail_release=False):
        self.connections = [FakeConnection(rows), FakeConnection(rows)]
        if fail_release:
            self.connections[1] = FakeConnection(rows, fail_release=True)
        self._pending = list(self.connections)

    def connect(self, autocommit=False):
        return self._pending.pop(0)


def test_monitor_detects_cycle_selects_youngest_and_recovers(monkeypatch):
    graph = wfg.WaitForGraph()
    graph.add_wait(121, 122, {"machine_id": 1, "reason": "blocked"})
    graph.add_wait(122, 121, {"machine_id": 2, "reason": "blocked"})
    db = FakeDatabase([(121, 100.0), (122, 101.0)])
    monkeypatch.setattr(engine, "db", db)

    eng = engine.Engine(num_workers=1, policy_mode="fcfs")
    eng.wfg = graph
    eng._inflight.update({121, 122})

    victim = eng._monitor_once()

    assert victim == 122
    assert eng.wfg.find_cycle() is None
    assert eng.wfg.edges_as_list() == []
    assert 122 not in eng._inflight
    queries = [query for conn in db.connections for query, _ in conn.calls]
    assert any("ReleaseJobResources" in query for query in queries)
    assert any("UPDATE Jobs SET status='QUEUED'" in query for query in queries)
    assert any("DEADLOCK_ROLLBACK" in query for query in queries)
    assert any("System_Events" in query and "DEADLOCK" in query for query in queries)


def test_monitor_leaves_acyclic_wait_chain_unchanged(monkeypatch):
    graph = wfg.WaitForGraph()
    graph.add_wait(1, 2, {"machine_id": 1, "reason": "blocked"})
    graph.add_wait(2, 3, {"machine_id": 2, "reason": "blocked"})

    eng = engine.Engine(num_workers=1, policy_mode="fcfs")
    eng.wfg = graph

    assert eng._monitor_once() is None
    assert eng.wfg.edges_as_list() == [[1, 2], [2, 3]]


def test_failed_rollback_keeps_cycle_for_retry(monkeypatch):
    graph = wfg.WaitForGraph()
    graph.add_wait(121, 122, {"machine_id": 1, "reason": "blocked"})
    graph.add_wait(122, 121, {"machine_id": 2, "reason": "blocked"})
    db = FakeDatabase([(121, 100.0), (122, 101.0)], fail_release=True)
    monkeypatch.setattr(engine, "db", db)

    eng = engine.Engine(num_workers=1, policy_mode="fcfs")
    eng.wfg = graph
    eng._inflight.update({121, 122})

    assert eng._monitor_once() is None
    assert eng.wfg.find_cycle() == [121, 122, 121]
    assert 122 in eng._inflight
