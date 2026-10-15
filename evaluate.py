"""TransactAlloc performance evaluation harness.

Runs the same synthetic workload under each scheduling policy and reports the
numbers a reviewer actually asks about: throughput, average wait, average
turnaround, peak fragmentation index, defragmentation activity, and how many
deadlocks were detected and rolled back.

Usage:
    py -3 evaluate.py                 # 3 policies, 30 jobs each
    py -3 evaluate.py --jobs 60 --workers 4
    py -3 evaluate.py --out docs/EVALUATION.md

The engine is driven through the real ASGI app (Starlette TestClient), so the
harness exercises the same routes, transactions and worker pool the dashboard
uses -- no mocks.
"""

import argparse
import statistics
import time

from fastapi.testclient import TestClient

import app as app_module

POLICIES = ("fcfs", "priority", "round-robin")


def _wait_drain(client, timeout=300):
    """Poll /api/state until nothing is queued or running (or we time out).

    Round-Robin is slow to drain by construction: every quantum preempts and
    re-queues, and each resume waits for the 1s reconciler poll, so a run needs
    several multiples of the ideal work time before the last job completes.
    """
    deadline = time.time() + timeout
    peak_frag = 0.0
    while time.time() < deadline:
        state = client.get("/api/state").json()
        counts = state["counts"]
        for cl in state["clusters"]:
            peak_frag = max(peak_frag, float(cl["frag_index"] or 0))
        if counts["queued"] == 0 and counts["running"] == 0:
            return state, peak_frag
        time.sleep(0.5)
    return client.get("/api/state").json(), peak_frag


def run_policy(client, policy, n_jobs, workers, seed):
    client.post("/api/reset")
    client.post("/api/engine/start", params={"workers": workers, "policy": policy})
    client.post("/api/jobs/batch", json={"count": n_jobs, "seed": seed})
    state, peak_frag = _wait_drain(client)
    stats = client.get("/api/stats").json()
    client.post("/api/engine/stop")

    jobs = state["jobs"]
    done = [j for j in jobs if j["status"] == "COMPLETED"]
    waits = [j["wait_sec"] for j in done]
    turns = [j["turnaround_sec"] for j in done]

    alloc = next((t for t in stats["txn"] if t["op"] == "ALLOCATE" and t["outcome"] == "COMMIT"), None)
    deadlocks = sum(1 for e in state["system_events"] if e["kind"] == "DEADLOCK")
    defrag_runs = stats["defrag"]["runs"] or 0
    span = stats["throughput"]["span_sec"] or 0

    return {
        "policy": policy,
        "completed": len(done),
        "throughput_per_s": round(len(done) / span, 2) if span else None,
        "avg_wait_s": round(statistics.mean(waits), 2) if waits else None,
        "p95_wait_s": round(sorted(waits)[int(len(waits) * 0.95)], 2) if waits else None,
        "avg_turnaround_s": round(statistics.mean(turns), 2) if turns else None,
        "peak_frag_index": round(peak_frag, 4),
        "defrag_runs": defrag_runs,
        "avg_alloc_ms": alloc["avg_ms"] if alloc else None,
        "deadlocks": deadlocks,
    }


def render_markdown(rows, args):
    cols = [
        ("policy", "Policy"),
        ("completed", "Done"),
        ("throughput_per_s", "Jobs/s"),
        ("avg_wait_s", "Avg wait (s)"),
        ("p95_wait_s", "P95 wait (s)"),
        ("avg_turnaround_s", "Avg turn (s)"),
        ("peak_frag_index", "Peak frag"),
        ("defrag_runs", "Defrags"),
        ("avg_alloc_ms", "Alloc txn (ms)"),
        ("deadlocks", "Deadlocks"),
    ]
    head = "| " + " | ".join(label for _, label in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [head, sep]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(k, "-")) for k, _ in cols) + " |")
    note = (
        f"\nWorkload: {args.jobs} jobs per policy, {args.workers} workers, "
        f"seed {args.seed}, best-fit placement. "
        f"Generated {time.strftime('%Y-%m-%d %H:%M')}.\n"
    )
    return "## Performance evaluation\n\n" + "\n".join(lines) + "\n" + note


def main():
    ap = argparse.ArgumentParser(description="TransactAlloc evaluation harness")
    ap.add_argument("--jobs", type=int, default=30, help="jobs per policy run")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default=None, help="write a markdown table here")
    args = ap.parse_args()

    rows = []
    with TestClient(app_module.app) as client:
        for pol in POLICIES:
            print(f"[eval] running policy={pol} ...")
            rows.append(run_policy(client, pol, args.jobs, args.workers, args.seed))
        client.post("/api/engine/stop")

    table = render_markdown(rows, args)
    print("\n" + table)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(table)
        print(f"[eval] wrote {args.out}")


if __name__ == "__main__":
    main()
