"""TransactAlloc: FastAPI entry (replaces console launcher).

Also preserves the old console interface under `console()` for bonus/demo.
"""

import argparse
import random
import sys

# Windows consoles default to cp1252, which cannot encode the arrows and
# box-drawing characters the log lines use; a worker thread printing one would
# raise UnicodeEncodeError and die. Reconfigure once, at the entry point.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    import uvicorn

    import api
    import db
    import dashboard as dash
    import policies
    import scheduler
    _HAS_WEB = True
except Exception as e:
    _HAS_WEB = False


POLICIES = ("fcfs", "priority", "round-robin", "best-fit", "auto")
ORDERING = {
    "fcfs": policies.fcfs,
    "best-fit": policies.fcfs,
    "round-robin": policies.fcfs,
    "priority": policies.priority,
}
TASKS = ("Matrix_Mult", "Monte_Carlo", "Image_Filter", "PageRank", "KMeans_Fit", "FFT_Batch", "Ray_Trace", "Nbody_Sim")


def _insert_job(conn, user, task, cpus, ram, priority, duration):
    return conn.execute(
        """
        INSERT INTO Jobs (user_id, task_name, req_cpus, req_ram_mb, priority, duration_sec)
        VALUES ((SELECT user_id FROM Users WHERE username = %s), %s, %s, %s, %s, %s)
        RETURNING job_id
        """,
        (user, task, cpus, ram, priority, duration),
    ).fetchone()[0]


def cmd_init(args):
    db.create_database()
    db.run_sql_file("schema.sql")
    db.run_sql_file("seed.sql")
    print("[INIT] transactalloc_p3 ready (blocks + defrag + txn-log + metrics)")


def cmd_reset(args):
    db.reset_all()
    print("[RESET] jobs/allocations cleared, one FREE block per cluster")


def cmd_submit(args):
    with db.connect(autocommit=True) as conn:
        jid = _insert_job(conn, args.user, args.task, args.cpus, args.ram, args.priority, args.duration)
    print(f"[PRODUCER] Job #{jid} queued ({args.task}: {args.cpus}c/{args.ram}MB prio {args.priority})")


def cmd_batch(args):
    rng = random.Random(args.seed)
    with db.connect(autocommit=True) as conn:
        ids = []
        for _ in range(args.count):
            ids.append(
                _insert_job(
                    conn,
                    "archu",
                    rng.choice(TASKS),
                    rng.choice((1, 2, 2, 4, 4, 6, 8)),
                    rng.choice((2048, 4096, 4096, 8192, 12288, 16384, 32768)),
                    rng.randint(1, 10),
                    rng.randint(2, 12),
                )
            )
    print(f"[PRODUCER] queued {len(ids)} jobs: #{ids[0]}..#{ids[-1]}")


def cmd_list(args):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT job_id, task_name, req_cpus, req_ram_mb, priority, remaining_sec, status FROM Jobs ORDER BY job_id DESC LIMIT 200"
        ).fetchall()
    print(f"{'JOB':>5} {'TASK':<14} {'CPU':>4} {'RAM':>8} {'PRI':>4} {'LEFT':>5} STATUS")
    for job_id, task, cpus, ram, pri, left, status in rows:
        print(f"{job_id:>5} {task:<14} {cpus:>4} {ram:>8} {pri:>4} {left:>5} {status}")


def cmd_stats(args):
    with db.connect() as conn:
        for mid, host, ac, tc, ar, tr, fr in conn.execute(
            "SELECT machine_id,hostname,allocated_cores,total_cores,allocated_ram_mb,total_ram_mb,frag_index FROM v_cluster_status ORDER BY machine_id"
        ).fetchall():
            print(f"[{mid:02d}] {host:<18} cores {ac:>3}/{tc:<3} ram {ar/1024:.1f}/{tr/1024:.1f}GB frag={fr}")
        counts = conn.execute("SELECT status,COUNT(*) FROM Jobs GROUP BY status").fetchall()
        print("jobs: " + ", ".join(f"{s}={n}" for s, n in counts))


def cmd_dashboard(args):
    with db.connect() as conn:
        dash.render(conn)
        dash.job_summary(conn)


def build_parser():
    p = argparse.ArgumentParser(description="TransactAlloc")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("reset")
    sub.add_parser("list")
    sub.add_parser("stats")
    sub.add_parser("dashboard")
    s = sub.add_parser("submit")
    s.add_argument("--user", default="archu")
    s.add_argument("--task", default="Matrix_Mult")
    s.add_argument("--cpus", type=int, default=2)
    s.add_argument("--ram", type=int, default=4096)
    s.add_argument("--priority", type=int, default=5)
    s.add_argument("--duration", type=int, default=6)
    b = sub.add_parser("batch")
    b.add_argument("--count", type=int, default=20)
    b.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("run")
    r.add_argument("--policy", choices=("fcfs", "priority", "round-robin", "best-fit"), default="fcfs")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--max-rounds", type=int, default=25)
    w = sub.add_parser("serve")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    return p


def console_main():
    parsed = build_parser().parse_args()
    {
        "init": cmd_init,
        "reset": cmd_reset,
        "submit": cmd_submit,
        "batch": cmd_batch,
        "list": cmd_list,
        "stats": cmd_stats,
        "dashboard": cmd_dashboard,
    }[parsed.cmd](parsed)


def web_main():
    if not _HAS_WEB:
        raise RuntimeError("FastAPI/uvicorn not installed")
    parsed = build_parser().parse_args()
    uvicorn.run("app:app", host=parsed.host, port=parsed.port, reload=False)


if __name__ == "__main__":
    p = build_parser()
    parsed = p.parse_args()
    if parsed.cmd == "serve":
        web_main()
    else:
        console_main()
