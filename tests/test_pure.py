"""Pure-logic tests: no database. Reuses each module's own self-check plus
the adaptive selector."""
import time

import adaptive
import api
import policies
import worker
import wfg


def test_policies_self_check():
    policies._self_check()


def test_wfg_self_check():
    wfg._self_check()


def test_wfg_edge_details_persist_and_cleanup():
    graph = wfg.WaitForGraph()
    graph.set_waits(10, [{
        "holder": 20,
        "machine_id": 3,
        "requested_cpus": 4,
        "requested_ram_mb": 8192,
        "reason": "INSUFFICIENT_RESOURCES",
    }])

    assert graph.edges_as_list() == [[10, 20]]
    detail = graph.edge_details()[0]
    assert detail["waiter"] == 10
    assert detail["holder"] == 20
    assert detail["machine_id"] == 3
    assert detail["requested_cpus"] == 4
    assert detail["requested_ram_mb"] == 8192
    assert detail["reason"] == "INSUFFICIENT_RESOURCES"
    assert "updated_at" in detail

    graph.set_waits(10, [{
        "holder": 30,
        "machine_id": 4,
        "requested_cpus": 4,
        "requested_ram_mb": 8192,
        "reason": "INSUFFICIENT_RESOURCES",
    }])
    assert graph.edges_as_list() == [[10, 30]]
    graph.add_wait(40, 10, {"machine_id": 5, "reason": "INSUFFICIENT_RESOURCES"})
    graph.set_waits(10, [{
        "holder": 31,
        "machine_id": 4,
        "requested_cpus": 4,
        "requested_ram_mb": 8192,
        "reason": "INSUFFICIENT_RESOURCES",
    }])
    assert graph.edges_as_list() == [[10, 31], [40, 10]]
    graph.clear_job(30)
    assert graph.edges_as_list() == [[10, 31], [40, 10]]
    graph.clear_job(10)
    assert graph.edges_as_list() == []
    assert graph.edge_details() == []


def test_worker_wait_edges_replace_and_clear():
    class Connection:
        def execute(self, query, params):
            class Result:
                def fetchall(self):
                    return [(20,), (21,)]
            return Result()

    graph = wfg.WaitForGraph()
    previous = worker._graph
    worker.set_graph(graph)
    try:
        job = {"job_id": 10, "req_cpus": 4, "req_ram_mb": 8192}
        holders = worker._graph_add_waits(Connection(), job, 3, "INSUFFICIENT_RESOURCES")
        assert holders == [20, 21]
        assert graph.edges_as_list() == [[10, 20], [10, 21]]
        assert all(d["machine_id"] == 3 for d in graph.edge_details())

        graph.set_waits(10, [{
            "holder": 30,
            "machine_id": 4,
            "requested_cpus": 4,
            "requested_ram_mb": 8192,
            "reason": "INSUFFICIENT_RESOURCES",
        }])
        assert graph.edges_as_list() == [[10, 30]]
        worker._graph_clear(10)
        assert graph.edges_as_list() == []
    finally:
        worker.set_graph(previous)


def test_api_wfg_state_serialization_preserves_compatibility():
    graph = wfg.WaitForGraph()
    graph.set_waits(10, [{
        "holder": 20,
        "machine_id": 3,
        "requested_cpus": 4,
        "requested_ram_mb": 8192,
        "reason": "INSUFFICIENT_RESOURCES",
    }])
    engine = type("Engine", (), {"wfg": graph})()

    payload = api._wfg_state(engine)

    assert payload["edges"] == [[10, 20]]
    assert payload["edge_details"][0]["holder"] == 20
    assert payload["edge_details"][0]["machine_id"] == 3


def test_adaptive_small_queue_is_fcfs():
    ctx = adaptive.Context(queue_depth=1, running=1, queued_jobs=[], now=time.time())
    pol, reason = adaptive.choose_policy(ctx)
    assert pol == "fcfs"


def test_adaptive_deep_queue_is_round_robin():
    now = time.time()
    jobs = [dict(job_id=i, priority=5, submitted_at=now) for i in range(8)]
    ctx = adaptive.Context(queue_depth=8, running=4, queued_jobs=jobs, now=now)
    pol, reason = adaptive.choose_policy(ctx)
    assert pol == "round-robin"


def test_adaptive_starvation_is_priority():
    now = time.time()
    jobs = [
        dict(job_id=1, priority=9, submitted_at=now - 120),
        dict(job_id=2, priority=10, submitted_at=now - 90),
        dict(job_id=3, priority=5, submitted_at=now),
    ]
    ctx = adaptive.Context(queue_depth=3, running=1, queued_jobs=jobs, now=now)
    pol, reason = adaptive.choose_policy(ctx)
    assert pol == "priority"
