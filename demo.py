"""Report section 5 -- the demonstration script.

Two acts:

  ACT 1  a batch of jobs is scheduled across both nodes and the dashboard is
         rendered from live database state.

  ACT 2  the cross-node deadlock from the report:
             Job#121 holds 32 GB on node 2, and wants 12 cores on node 1
             Job#122 holds 10 cores on node 1, and wants 40 GB on node 2
         Neither can proceed. The wait-for graph finds the cycle, the youngest
         transaction is rolled back, and the survivor is unblocked.

  py -3 demo.py                    detection: let the deadlock happen, recover
  py -3 demo.py --avoidance on     avoidance: Banker refuses, it never happens
  py -3 demo.py --skip-batch       jump straight to act 2

Nothing here is printed theatre. Every line follows a real transaction against
PostgreSQL; the dashboard reads committed rows.
"""
import argparse
import time

import psycopg

import banker
import dashboard
import db
import main as cli
import wfg

NODE1, NODE2 = 1, 2
JOB_A, JOB_B = 121, 122          # ids fixed so the output matches the report

# What each job needs in total, as a Banker vector over
# [cores_node1, ram_node1, cores_node2, ram_node2].
CLAIM = {
    JOB_A: [12, 0, 0, 32768],    # 12 cores on node 1 + 32 GB on node 2
    JOB_B: [10, 0, 0, 40960],    # 10 cores on node 1 + 40 GB on node 2
}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")


# --------------------------------------------------------------------------
# database helpers
# --------------------------------------------------------------------------

def reset_cluster(conn, restart_ids_at=None):
    # ResetCluster() tears down the block map as well as the counters, which
    # the phase 3 direct DELETEs could not know about. Same call the CLI and
    # the API use, so the three can never disagree about what "empty" means.
    if restart_ids_at is None:
        conn.execute("SELECT ResetCluster(NULL::int)")
    else:
        conn.execute("SELECT ResetCluster(%s)", (restart_ids_at,))


def create_job(conn, job_id, task, cores, ram, priority=5, duration=30):
    conn.execute("""
        INSERT INTO Jobs (job_id, user_id, task_name, req_cpus, req_ram_mb,
                          priority, duration_sec)
        VALUES (%s, 1, %s, %s, %s, %s, %s)
    """, (job_id, task, cores, ram, priority, duration))


def cluster_vector(conn):
    """Free capacity as [cores_n1, ram_n1, cores_n2, ram_n2]."""
    rows = conn.execute("""
        SELECT c.total_cores - c.allocated_cores, r.total_ram_mb - r.allocated_ram_mb
        FROM Machines m
        JOIN CPU_Resources    c ON c.machine_id = m.machine_id
        JOIN Memory_Resources r ON r.machine_id = m.machine_id
        ORDER BY m.machine_id
    """).fetchall()
    return [value for row in rows for value in row]


def held_vector(conn, job_id):
    """What one job currently holds, in the same vector layout."""
    vector = [0, 0, 0, 0]
    for machine_id, cores, ram in conn.execute("""
        SELECT machine_id, alloc_cpus, alloc_ram_mb FROM Allocations
        WHERE job_id = %s AND released_at IS NULL
    """, (job_id,)).fetchall():
        base = (machine_id - 1) * 2
        vector[base] += cores
        vector[base + 1] += ram
    return vector


def holders_on(conn, machine_id):
    return [job_id for (job_id,) in conn.execute("""
        SELECT DISTINCT job_id FROM Allocations
        WHERE machine_id = %s AND released_at IS NULL
        ORDER BY job_id
    """, (machine_id,)).fetchall()]


def request(conn, job_id, machine_id, cores, ram):
    """One transaction. True on COMMIT, False after a real ROLLBACK."""
    try:
        with conn.transaction():
            conn.execute("SELECT AllocateJobResources(%s, %s, %s, %s)",
                         (job_id, machine_id, cores, ram))
        return True
    except psycopg.errors.RaiseException:
        return False


def rollback_job(conn, job_id):
    """Abort a transaction's work: hand every resource back, re-queue the job."""
    with conn.transaction():
        conn.execute("SELECT ReleaseJobResources(%s)", (job_id,))
        conn.execute("UPDATE Jobs SET status = 'QUEUED' WHERE job_id = %s", (job_id,))
        conn.execute("INSERT INTO Execution_Logs (job_id, log_level, message) "
                     "VALUES (%s, 'ERROR', 'rolled back: deadlock victim')", (job_id,))


# --------------------------------------------------------------------------
# ACT 1 -- batch scheduling and the live dashboard
# --------------------------------------------------------------------------

def act_one(count, workers):
    with db.connect(autocommit=True) as conn:
        reset_cluster(conn)

    dashboard.banner(f">> Submitting a batch of {count} concurrent jobs")
    cli.cmd_batch(argparse.Namespace(count=count, seed=7))
    cli.cmd_run(argparse.Namespace(policy="best-fit", workers=workers,
                                   max_rounds=25, avoidance="off"))

    print()
    with db.connect(autocommit=True) as conn:
        dashboard.render(conn)
        dashboard.job_summary(conn)


# --------------------------------------------------------------------------
# ACT 2 -- the deadlock
# --------------------------------------------------------------------------

def act_two(avoidance):
    mode = "AVOIDANCE (Banker's algorithm)" if avoidance else "DETECTION (Wait-For Graph)"
    dashboard.banner(f"DEADLOCK {mode} TEST")

    # Autocommit plus explicit conn.transaction() blocks, for the same reason
    # worker.py does it: a preceding SELECT on a non-autocommit connection would
    # turn every "transaction" here into a savepoint, and a savepoint unwind is
    # not the ROLLBACK this demo claims to be showing.
    with db.connect(autocommit=True) as conn:
        reset_cluster(conn, restart_ids_at=JOB_A)
        create_job(conn, JOB_A, "Tensor_Train", 12, 32768, priority=1)
        create_job(conn, JOB_B, "Graph_Rank",   10, 40960, priority=1)

        graph = wfg.WaitForGraph()
        submitted_at = {JOB_A: 1.0, JOB_B: 2.0}      # 122 is the younger job

        def guarded(job_id, machine_id, cores, ram, description):
            """Grant a request, consulting the Banker first when avoidance is on."""
            if avoidance:
                free = cluster_vector(conn)
                want = [0, 0, 0, 0]
                base = (machine_id - 1) * 2
                want[base], want[base + 1] = cores, ram
                allocation = {j: held_vector(conn, j) for j in (JOB_A, JOB_B)}
                if not banker.grant_is_safe(job_id, want, free, CLAIM, allocation):
                    log(f"[BANKER] Job#{job_id} {description} -> REFUSED, "
                        "grant would leave the cluster in an UNSAFE state")
                    return False
                log(f"[BANKER] Job#{job_id} {description} -> safe, granting")
            return request(conn, job_id, machine_id, cores, ram)

        # --- step 1: Job#121 takes its 32 GB on node 2 -------------------
        ok = guarded(JOB_A, NODE2, 0, 32768, "requests 32GB RAM on Node 02")
        log(f"Job#{JOB_A} acquires 32GB RAM on Node 02 ... "
            f"{'GRANTED' if ok else 'REFUSED'}")

        # --- step 2: Job#122 takes 10 cores on node 1 --------------------
        # There IS free capacity here, so a plain allocator says yes. This is
        # the exact grant that creates the circular wait.
        ok_b = guarded(JOB_B, NODE1, 10, 0, "requests 10 Cores on Node 01")
        log(f"Job#{JOB_B} acquires 10 Cores on Node 01 ... "
            f"{'GRANTED' if ok_b else 'REFUSED'}")

        if avoidance and not ok_b:
            print()
            log("[BANKER] circular wait prevented before it could form.")
            log(f"[SCHEDULER] Job#{JOB_B} stays QUEUED until Job#{JOB_A} completes.")
            finish_and_report(conn, avoidance=True)
            return

        # --- step 3 and 4: each job now waits on the other ---------------
        log(f"Job#{JOB_A} requests 12 Cores on Node 01 (Holds 32GB RAM on Node 02)")
        if not request(conn, JOB_A, NODE1, 12, 0):
            for holder in holders_on(conn, NODE1):
                graph.add_wait(JOB_A, holder)
            log(f"         -> BLOCKED: Node 01 has fewer than 12 free cores, "
                f"held by {holders_on(conn, NODE1)}")

        log(f"Job#{JOB_B} requests 40GB RAM on Node 02 (Holds 10 Cores on Node 01)")
        if not request(conn, JOB_B, NODE2, 0, 40960):
            for holder in holders_on(conn, NODE2):
                graph.add_wait(JOB_B, holder)
            log(f"         -> BLOCKED: Node 02 has less than 40GB free, "
                f"held by {holders_on(conn, NODE2)}")

        # --- step 5: detection -------------------------------------------
        cycle = graph.find_cycle()
        if cycle is None:
            log("[WFG ENGINE] no cycle found -- no deadlock this run")
            finish_and_report(conn, avoidance=False)
            return

        arrow = " -> ".join(f"Job#{j}" for j in cycle)
        log(f"[WFG ENGINE] Circular Wait Detected: ({arrow})")

        victim = wfg.youngest(cycle, submitted_at)
        survivor = next(j for j in cycle if j != victim)
        log(f"[DBMS ENGINE] Executing Transaction ROLLBACK on Job#{victim} "
            "(youngest in the cycle)")

        rollback_job(conn, victim)
        graph.clear_job(victim)
        log("[TRANSACTION] ROLLBACK SUCCESSFUL. Resources released on Node 01.")

        # --- step 6: the survivor is unblocked ---------------------------
        if request(conn, survivor, NODE1, 12, 0):
            log(f"[SCHEDULER] Job#{survivor} unblocked and allocated. "
                f"Job#{victim} re-queued.")
        else:
            log(f"[SCHEDULER] Job#{survivor} still cannot be placed")

        assert graph.find_cycle() is None, "the cycle must be gone after the rollback"
        finish_and_report(conn, avoidance=False)


def finish_and_report(conn, avoidance):
    print()
    dashboard.render(conn, "POST-RESOLUTION CLUSTER STATE")
    dashboard.job_summary(conn)

    over = conn.execute("""
        SELECT COUNT(*) FROM CPU_Resources c
        JOIN Memory_Resources r ON r.machine_id = c.machine_id
        WHERE c.allocated_cores > c.total_cores OR r.allocated_ram_mb > r.total_ram_mb
    """).fetchone()[0]
    assert over == 0, "capacity invariant broken"
    print("Invariant holds on every node: allocated <= total.")
    print("Deadlock handled by: "
          + ("AVOIDANCE -- the unsafe grant was never made."
             if avoidance else
             "DETECTION -- the cycle was found and the youngest job rolled back."))


def main():
    parser = argparse.ArgumentParser(description="Cloud Resource Manager - phase 3 demo")
    parser.add_argument("--avoidance", choices=("on", "off"), default="off",
                        help="on = Banker's algorithm prevents the deadlock; "
                             "off = let it form, then detect and roll back")
    parser.add_argument("--batch", type=int, default=20,
                        help="jobs submitted in act 1 (report section 5 uses 20)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-batch", action="store_true",
                        help="skip act 1 and go straight to the deadlock")
    args = parser.parse_args()

    if not args.skip_batch:
        act_one(args.batch, args.workers)
        print()
    act_two(avoidance=args.avoidance == "on")


if __name__ == "__main__":
    main()
