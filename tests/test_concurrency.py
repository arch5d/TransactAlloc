"""The load-bearing test: many threads racing for the same cluster must never
over-commit it. Row-level locks in AllocateJobResources() serialise them; any
that lose get INSUFFICIENT_RESOURCES and a real ROLLBACK.

Run under load:
    py -m pytest tests/test_concurrency.py -q
"""
import threading

import psycopg

import db


def _worker(machine_id, cpus, ram, results, idx):
    """One thread, one connection: exactly how a real worker behaves."""
    with db.connect(autocommit=True) as conn:
        # job must exist for the FK
        jid = conn.execute(
            """INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec)
               VALUES (1,'race',%s,%s,5,1) RETURNING job_id""",
            (cpus, ram),
        ).fetchone()[0]
        try:
            with conn.transaction():
                conn.execute("SELECT AllocateJobResources(%s,%s,%s,%s)", (jid, machine_id, cpus, ram))
            results[idx] = "ok"
        except psycopg.errors.RaiseException as exc:
            results[idx] = "rollback"


def test_no_overcommit_under_contention():
    db.reset_with_job_seq(200)
    # cluster 4: 48 cores, 131072 MB. Fire 64 threads each wanting 4 cores /
    # 8192 MB -> at most 12 can win on cores and 16 on RAM, so 12 succeed.
    threads_n = 64
    cpus, ram = 4, 8192
    results = [None] * threads_n
    threads = [
        threading.Thread(target=_worker, args=(4, cpus, ram, results, i)) for i in range(threads_n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = results.count("ok")
    with db.connect() as c:
        used_c, total_c, used_r, total_r = c.execute(
            """SELECT c.allocated_cores, c.total_cores, r.allocated_ram_mb, r.total_ram_mb
                 FROM CPU_Resources c JOIN Memory_Resources r USING(machine_id)
                WHERE machine_id=4"""
        ).fetchone()
    assert used_c <= total_c, "cores over-committed"
    assert used_r <= total_r, "ram over-committed"
    assert used_c == winners * cpus, f"accounting drift: {used_c} cores for {winners} winners"
    with db.connect() as c:
        assert c.execute("SELECT ValidateBlockMap(4)").fetchone()[0]
    # and every loser was a clean rollback, not a partial write
    assert results.count("rollback") == threads_n - winners
