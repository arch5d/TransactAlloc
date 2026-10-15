"""Database behaviour: block coalescing, fragmentation detection, defrag,
ACID rollback on insufficient resources, and the doc-string deadlock scenario.

    py -m pytest tests -q
"""
import psycopg

import banker
import db
import demo
import frag
import wfg


def setup_function():
    db.reset_with_job_seq(100)


def _new_job(conn, cpus, ram, pri=5, dur=5):
    return conn.execute(
        """INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec)
           VALUES (1,'t',%s,%s,%s,%s) RETURNING job_id""",
        (cpus, ram, pri, dur),
    ).fetchone()[0]


def test_seed_block_map_valid():
    with db.connect() as c:
        for m in (1, 2, 3, 4):
            assert c.execute("SELECT ValidateBlockMap(%s)", (m,)).fetchone()[0]


def test_allocation_carves_and_release_coalesces():
    with db.connect() as c:
        j = _new_job(c, 1, 1024)
        c.commit()
        c.execute("SELECT AllocateJobResources(%s,4,%s,%s)", (j, 1, 1024))
        c.commit()
        st = frag.cluster_status(c, 4)
        # exactly one allocated block carved off the front, one free remainder
        assert st["holes"] == 1
        assert st["largest_free_mb"] == st["total_ram_mb"] - 1024
        c.execute("SELECT ReleaseJobResources(%s)", (j,))
        c.commit()
        # release must heal back to a single span covering the whole cluster
        st = frag.cluster_status(c, 4)
        assert st["holes"] == 1
        assert st["largest_free_mb"] == st["total_ram_mb"]
        assert float(st["frag_index"]) == 0.0


def test_insufficient_resources_rolls_back_cleanly():
    with db.connect() as c:
        j = _new_job(c, 1, 10_000_000)
        c.commit()
        before = c.execute(
            "SELECT allocated_ram_mb FROM Memory_Resources WHERE machine_id=3"
        ).fetchone()[0]
        try:
            with c.transaction():
                c.execute("SELECT AllocateJobResources(%s,3,%s,%s)", (j, 1, 10_000_000))
            assert False, "should have raised"
        except psycopg.errors.RaiseException as exc:
            assert "INSUFFICIENT_RESOURCES" in str(exc)
        after = c.execute(
            "SELECT allocated_ram_mb FROM Memory_Resources WHERE machine_id=3"
        ).fetchone()[0]
        assert before == after, "a rolled-back allocation must leave no trace"


def test_fragmented_is_distinct_from_insufficient():
    """50% free, but nothing fits: the exact problem TransactAlloc exists for.

    cluster 3 is 32768 MB. Three 8192 MB allocations fill 24576 MB, leaving
    one 8192 MB hole at the top. Releasing the middle allocation leaves free
    RAM fragmented as [8192, 8192] while an allocation of 16000 MB -- less
    than the 16384 MB that is free -- fits in neither hole.
    """
    with db.connect() as c:
        js = [_new_job(c, 1, 8192) for _ in range(3)]
        c.commit()
        for j in js:
            c.execute("SELECT AllocateJobResources(%s,3,%s,%s)", (j, 1, 8192))
        c.execute("SELECT ReleaseJobResources(%s)", (js[1],))
        c.commit()

        st = frag.cluster_status(c, 3)
        assert st["free_ram_mb"] == 16384 and st["holes"] == 2
        assert st["largest_free_mb"] == 8192

        big = _new_job(c, 1, 16000)
        c.commit()
        try:
            with c.transaction():
                c.execute("SELECT AllocateJobResources(%s,3,%s,%s)", (big, 1, 16000))
            assert False, "no single hole is large enough, so this must fail"
        except psycopg.errors.RaiseException as exc:
            msg = str(exc)
            assert "FRAGMENTED" in msg, f"should be FRAGMENTED, not: {msg}"


def test_defrag_reduces_holes():
    with db.connect() as c:
        js = [_new_job(c, 1, 4096) for _ in range(4)]
        c.commit()
        for j in js:
            c.execute("SELECT AllocateJobResources(%s,3,%s,%s)", (j, 1, 4096))
        c.execute("SELECT ReleaseJobResources(%s)", (js[1],))
        c.execute("SELECT ReleaseJobResources(%s)", (js[2],))
        c.commit()
        before = frag.cluster_status(c, 3)
        moves = frag.defrag_plan(c, 3)
        frag.apply_defrag(c, 3, moves, triggered_by="AUTO")
        c.commit()
        after = frag.cluster_status(c, 3)
        assert after["holes"] <= before["holes"]
        assert float(after["frag_index"]) <= float(before["frag_index"]) + 1e-9
        assert c.execute("SELECT ValidateBlockMap(%s)", (3,)).fetchone()[0]


def test_three_adjacent_frees_coalesce_into_one_span():
    """Regression: freeing a middle block between two free neighbours.

    The old coalescer extended the RIGHT block in place, keeping its start,
    which left the left block's space uncovered and the two spans overlapping
    -- ValidateBlockMap() then fails. A correct coalescer takes the
    predecessor's address for the merged span.
    """
    with db.connect() as c:
        a, b, cc = [_new_job(c, 1, 4096) for _ in range(3)]
        c.commit()
        for j in (a, b, cc):
            c.execute("SELECT AllocateJobResources(%s,4,%s,%s)", (j, 1, 4096))
        c.commit()
        c.execute("SELECT ReleaseJobResources(%s)", (a,))
        c.execute("SELECT ReleaseJobResources(%s)", (cc,))
        c.commit()
        c.execute("SELECT ReleaseJobResources(%s)", (b,))  # middle, both sides free
        c.commit()
        assert c.execute("SELECT ValidateBlockMap(4)").fetchone()[0], "block map corrupted"
        st = frag.cluster_status(c, 4)
        assert st["holes"] == 1
        assert st["largest_free_mb"] == st["total_ram_mb"]
        assert float(st["frag_index"]) == 0.0


def test_deadlock_detection_scenario():
    """The report section 5 circular wait, against the live database."""
    with db.connect(autocommit=True) as conn:
        demo.reset_cluster(conn, restart_ids_at=demo.JOB_A)
        demo.create_job(conn, demo.JOB_A, "Tensor_Train", 12, 32768, priority=1)
        demo.create_job(conn, demo.JOB_B, "Graph_Rank", 10, 40960, priority=1)

        assert demo.request(conn, demo.JOB_A, demo.NODE2, 0, 32768)
        assert demo.request(conn, demo.JOB_B, demo.NODE1, 10, 0)

        graph = wfg.WaitForGraph()
        assert not demo.request(conn, demo.JOB_A, demo.NODE1, 12, 0)
        for h in demo.holders_on(conn, demo.NODE1):
            graph.add_wait(demo.JOB_A, h)
        assert not demo.request(conn, demo.JOB_B, demo.NODE2, 0, 40960)
        for h in demo.holders_on(conn, demo.NODE2):
            graph.add_wait(demo.JOB_B, h)

        cycle = graph.find_cycle()
        assert cycle is not None and set(cycle) == {demo.JOB_A, demo.JOB_B}
        victim = wfg.youngest(cycle, {demo.JOB_A: 1.0, demo.JOB_B: 2.0})
        assert victim == demo.JOB_B

        demo.rollback_job(conn, victim)
        graph.clear_job(victim)
        assert graph.find_cycle() is None
        assert demo.request(conn, demo.JOB_A, demo.NODE1, 12, 0)
        for m in (1, 2):
            conn.execute("SELECT ValidateBlockMap(%s)", (m,))
