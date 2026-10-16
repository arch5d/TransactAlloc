"""End-to-end API checks driven through the ASGI app in-process.

No network, no ports, no orphan processes: Starlette's TestClient calls the
app directly, so this exercises the real routes, the real database, and the
real engine threads. Run with:

    py -3 -m pytest tests/test_api_e2e.py -q
"""

import time

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c
    # stop the engine and clear jobs so nothing RUNNING leaks into the next run
    c.post("/api/engine/stop")
    c.post("/api/reset")


@pytest.fixture(autouse=True)
def _clean(client):
    """Every test starts from a stopped engine and an empty, seeded database."""
    client.post("/api/engine/stop")
    client.post("/api/reset")
    yield
    client.post("/api/engine/stop")


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_state_shape(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    for key in ("clusters", "jobs", "counts", "policy", "engine", "wfg", "maps"):
        assert key in body, f"/api/state missing {key}"
    assert len(body["clusters"]) == 4
    for key in ("queued", "running", "completed", "failed"):
        assert key in body["counts"]
    # every cluster has a memory map whose blocks cover its total RAM
    for cl in body["clusters"]:
        blocks = body["maps"][str(cl["machine_id"])]
        assert blocks, f"no memory map for cluster {cl['machine_id']}"
        assert sum(b["size_mb"] for b in blocks) == cl["total_ram_mb"]


def test_users_endpoint(client):
    body = client.get("/api/users").json()
    names = {u["username"] for u in body["users"]}
    assert {"admin", "Archita", "student"} <= names, names
    for u in body["users"]:
        for key in ("role", "quota_cpus", "quota_ram_mb", "used_cpus", "used_ram_mb",
                    "cpu_pct", "ram_pct", "queued", "running", "completed",
                    "failed", "active_clusters"):
            assert key in u, f"/api/users missing {key}"
        assert 0 <= u["used_cpus"] <= u["quota_cpus"]

    # a queued job shows up in the account's in-flight usage
    client.post("/api/jobs", json={
        "user": "Archita", "task": "Matrix_Mult", "cpus": 4, "ram_mb": 4096,
        "priority": 5, "duration_sec": 3,
    })
    archita = next(u for u in client.get("/api/users").json()["users"] if u["username"] == "Archita")
    assert archita["used_cpus"] == 4 and archita["queued"] == 1, archita


def test_history_endpoint(client):
    body = client.get("/api/history?minutes=5").json()
    for key in ("minutes", "metrics", "engine"):
        assert key in body, f"/api/history missing {key}"

    client.post("/api/engine/start", params={"workers": 2, "policy": "fcfs"})
    client.post("/api/jobs/batch", json={"count": 4, "seed": 1})
    time.sleep(6)  # at least one sampler tick
    eng = client.get("/api/history?minutes=5").json()["engine"]
    assert eng, "expected engine samples after starting the engine"
    assert any("queue_depth" in s and "running_jobs" in s for s in eng)


def test_reset_then_submit_and_run(client):
    assert client.post("/api/reset").json()["ok"] is True

    started = client.post("/api/engine/start", params={"workers": 4, "policy": "auto"})
    assert started.status_code == 200 and started.json()["ok"] is True

    single = client.post("/api/jobs", json={
        "user": "Archita", "task": "Web_Crawl", "cpus": 2, "ram_mb": 2048,
        "priority": 2, "duration_sec": 2,
    })
    assert single.status_code == 200, single.text

    batch = client.post("/api/jobs/batch", json={"count": 5, "seed": 3})
    assert batch.status_code == 200
    assert batch.json()["count"] == 5

    time.sleep(3)
    state = client.get("/api/state").json()
    total = state["counts"]["queued"] + state["counts"]["running"] + state["counts"]["completed"]
    assert total >= 6, state["counts"]

    tx = client.get("/api/stats").json()["txn"]
    assert any(row["op"] == "ALLOCATE" and row["outcome"] == "COMMIT" for row in tx), tx


def test_policy_switch(client):
    r = client.post("/api/policy", json={"mode": "round-robin"})
    assert r.status_code == 200 and r.json()["mode"] == "round-robin"
    assert client.get("/api/state").json()["policy"]["mode"] == "round-robin"


def test_defrag_without_body(client):
    r = client.post("/api/defrag")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_deadlock_demo(client):
    r = client.post("/api/deadlock-demo")
    assert r.status_code == 200, r.text
    transcript = "\n".join(r.json()["transcript"])
    assert "circular wait detected" in transcript, transcript
    assert "ROLLBACK committed" in transcript, transcript


def test_engine_stop(client):
    assert client.post("/api/engine/stop").json()["ok"] is True
    assert client.get("/api/state").json()["engine"]["running"] is False


def test_stop_aborts_and_requeues_inflight_jobs(client):
    """Stopping must park in-flight jobs as QUEUED, not leave them RUNNING."""
    client.post("/api/reset")
    client.post("/api/engine/start", params={"workers": 2, "policy": "fcfs"})
    for _ in range(2):
        client.post("/api/jobs", json={
            "user": "Archita", "task": "Matrix_Mult", "cpus": 2, "ram_mb": 2048,
            "priority": 5, "duration_sec": 6,
        })
    time.sleep(1.5)  # let the two workers pick the jobs up

    client.post("/api/engine/stop")
    counts = client.get("/api/state").json()["counts"]
    assert counts["running"] == 0, counts
    assert counts["queued"] == 2, counts

    # A fresh engine resumes the parked jobs and drains them.
    client.post("/api/engine/start", params={"workers": 4, "policy": "fcfs"})
    deadline = time.time() + 40
    while time.time() < deadline:
        counts = client.get("/api/state").json()["counts"]
        if counts["queued"] == 0 and counts["running"] == 0:
            break
        time.sleep(0.5)
    assert counts["completed"] == 2, counts

    # Re-running must not see stale jobs from the last run.
    client.post("/api/reset")
    assert client.get("/api/state").json()["counts"]["completed"] == 0

