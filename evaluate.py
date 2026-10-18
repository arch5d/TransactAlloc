"""TransactAlloc performance evaluation harness.

Runs the same synthetic workload under each scheduling policy and reports the
numbers a reviewer actually asks about: throughput, average wait, average
turnaround, peak fragmentation index, defragmentation activity, and how many
deadlocks were detected and rolled back.

The optional ``--pure`` mode compares the scheduling policies with the
demo-only Banker avoidance algorithm without requiring PostgreSQL. It uses a
deterministic two-phase workload so unsafe grants and recovery are measurable.

Usage:
    py -3 evaluate.py                 # 3 policies, 30 jobs each
    py -3 evaluate.py --jobs 60 --workers 4
    py -3 evaluate.py --out docs/EVALUATION.md

The engine is driven through the real ASGI app (Starlette TestClient), so the
harness exercises the same routes, transactions and worker pool the dashboard
uses -- no mocks.
"""

import argparse
import copy
import math
import random
import statistics
import time

from fastapi.testclient import TestClient

import app as app_module
import banker
import policies

POLICIES = ("fcfs", "priority", "round-robin")
PURE_POLICIES = POLICIES + ("banker",)
PURE_QUANTUM = 1


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


def generate_workload(n_jobs=12, seed=11):
    """Create a reproducible two-phase workload for pure policy comparison."""
    if n_jobs < 1:
        raise ValueError("n_jobs must be positive")
    rng = random.Random(seed)
    jobs = []
    for job_id in range(1, n_jobs + 1):
        if job_id % 2:
            initial, followup = [6, 0], [0, 6]
        else:
            initial, followup = [0, 6], [6, 0]
        jobs.append({
            "job_id": job_id,
            "submitted_at": 0,
            "priority": rng.randint(1, 10),
            "duration": rng.randint(1, 3),
            "initial": initial,
            "followup": followup,
            "claim": [6, 6],
        })
    return jobs


def _validate_workload(workload, total_resources):
    if len(total_resources) != 2 or any(resource <= 0 for resource in total_resources):
        return "total_resources must contain two positive values"
    for job in workload:
        requests = [job["initial"], job["followup"]]
        claim = job["claim"]
        if len(claim) != 2 or any(value < 0 for value in claim):
            return f"job {job['job_id']} has an invalid claim"
        if any(len(request) != 2 or any(value < 0 for value in request) for request in requests):
            return f"job {job['job_id']} has an invalid request vector"
        if any(sum(requests[phase][index] for phase in range(2)) > claim[index]
               for index in range(2)):
            return f"job {job['job_id']} requests exceed its claim"
        if any(value > total_resources[index] for index, value in enumerate(claim)):
            return f"job {job['job_id']} claims exceed total resources"
    return None


def _pure_row(policy, workload, total_resources):
    invalid = _validate_workload(workload, total_resources)
    if invalid:
        return {"policy": policy, "status": "invalid", "error": invalid}

    jobs = copy.deepcopy(workload)
    available = list(total_resources)
    allocation = {job["job_id"]: [0, 0] for job in jobs}
    max_claim = {job["job_id"]: list(job["claim"]) for job in jobs}
    states = {
        job["job_id"]: {
            "phase": "initial",
            "start": None,
            "finish": None,
            "remaining": job["duration"],
            "rollbacks": 0,
        }
        for job in jobs
    }
    queue = [job["job_id"] for job in jobs]
    job_by_id = {job["job_id"]: job for job in jobs}
    resource_wait = {job["job_id"]: 0 for job in jobs}
    dispatch_trace = []
    initial_dispatch_order = []
    synthetic_deadlocks = 0
    synthetic_rollbacks = 0
    unsafe_grants_prevented = 0
    tick = 0
    blocked_attempts = set()
    minimum_available = list(available)

    # A dispatch advances the clock by one request tick or service quantum.
    # Response is the first initial grant; resource wait counts time queued
    # for either request; service is deterministic run duration; turnaround is
    # completion minus submission; makespan is the last completion tick.
    def fits(request):
        return all(want <= free for want, free in zip(request, available))

    def grant(job, request):
        nonlocal available, unsafe_grants_prevented
        if not fits(request):
            return False
        if policy == "banker" and not banker.grant_is_safe(
                job["job_id"], request, available, max_claim, allocation):
            unsafe_grants_prevented += 1
            return False
        available = [free - want for free, want in zip(available, request)]
        minimum_available[:] = [
            min(low, free) for low, free in zip(minimum_available, available)
        ]
        allocation[job["job_id"]] = [
            held + want for held, want in zip(allocation[job["job_id"]], request)
        ]
        return True

    def request_for(job_id):
        job = job_by_id[job_id]
        return job["initial"] if states[job_id]["phase"] == "initial" else job["followup"]

    while queue:
        if policy == "priority":
            selected = min(
                queue,
                key=lambda job_id: (
                    policies.effective_priority(job_by_id[job_id], now=tick),
                    job_by_id[job_id]["submitted_at"],
                    job_id,
                ),
            )
            queue.remove(selected)
        else:
            selected = queue.pop(0)
        job = job_by_id[selected]
        state = states[selected]
        dispatch_trace.append(selected)
        if state["phase"] == "initial":
            initial_dispatch_order.append(selected)
        granted = True

        if state["phase"] == "initial":
            granted = grant(job, job["initial"])
            if granted:
                state["phase"] = "followup"
                state["start"] = tick
        elif state["phase"] == "followup":
            granted = grant(job, job["followup"])
            if granted:
                state["phase"] = "running"

        if granted:
            blocked_attempts.clear()
            if state["phase"] == "running":
                quantum = PURE_QUANTUM if policy == "round-robin" else state["remaining"]
                elapsed = min(quantum, state["remaining"])
                state["remaining"] -= elapsed
                tick += elapsed
                if state["remaining"] == 0:
                    available = [
                        free + held
                        for free, held in zip(available, allocation[selected])
                    ]
                    allocation[selected] = [0, 0]
                    state["phase"] = "done"
                    state["finish"] = tick
                else:
                    queue.append(selected)
            else:
                elapsed = 1
                queue.append(selected)
                tick += elapsed
        else:
            elapsed = 1
            queue.append(selected)
            blocked_attempts.add(selected)
            tick += elapsed

        for job_id in queue:
            resource_wait[job_id] += elapsed

        circular_wait = (
            queue
            and all(not fits(request_for(job_id)) for job_id in queue)
            and any(allocation[job_id] != [0, 0] for job_id in queue)
        )
        if not granted and (len(blocked_attempts) >= len(queue) or circular_wait):
            blocked = [
                job_id for job_id in queue
                if allocation[job_id] != [0, 0]
            ]
            if not blocked:
                return {
                    "policy": policy,
                    "status": "stalled",
                    "error": "no runnable job can satisfy its resource request",
                    "dispatch_trace": dispatch_trace,
                }
            if policy == "banker":
                return {
                    "policy": policy,
                    "status": "stalled",
                    "error": "Banker refused all pending grants",
                    "dispatch_trace": dispatch_trace,
                    "unsafe_grants_prevented": unsafe_grants_prevented,
                }
            victim = max(
                blocked,
                key=lambda job_id: (
                    job_by_id[job_id]["submitted_at"],
                    -states[job_id]["rollbacks"],
                    -job_id,
                ),
            )
            available = [
                free + held for free, held in zip(available, allocation[victim])
            ]
            allocation[victim] = [0, 0]
            states[victim]["phase"] = "initial"
            states[victim]["rollbacks"] += 1
            queue.remove(victim)
            queue.append(victim)
            blocked_attempts.clear()
            synthetic_deadlocks += 1
            synthetic_rollbacks += 1

        if tick > 10000:
            return {
                "policy": policy,
                "status": "stalled",
                "error": "evaluation exceeded the tick limit",
                "dispatch_trace": dispatch_trace,
                "initial_dispatch_order": initial_dispatch_order,
            }

    makespan = max(state["finish"] for state in states.values())
    waits = [resource_wait[job["job_id"]] for job in jobs]
    responses = [states[job["job_id"]]["start"] - job["submitted_at"] for job in jobs]
    turnarounds = [states[job["job_id"]]["finish"] - job["submitted_at"] for job in jobs]
    p95_index = max(0, math.ceil(0.95 * len(waits)) - 1)
    return {
        "policy": policy,
        "status": "completed",
        "completed": len(jobs),
        "throughput_per_tick": round(len(jobs) / max(makespan, 1), 4),
        "avg_response": round(statistics.mean(responses), 4),
        "avg_wait": round(statistics.mean(waits), 4),
        "p95_wait": round(sorted(waits)[p95_index], 4),
        "avg_turnaround": round(statistics.mean(turnarounds), 4),
        "service_ticks": sum(job["duration"] for job in jobs),
        "makespan_ticks": makespan,
        "final_available": available,
        "minimum_available": minimum_available,
        "synthetic_deadlocks": synthetic_deadlocks,
        "synthetic_rollbacks": synthetic_rollbacks,
        "unsafe_grants_prevented": unsafe_grants_prevented,
        "dispatch_trace": dispatch_trace,
        "initial_dispatch_order": initial_dispatch_order,
    }


def run_pure_comparison(n_jobs=12, seed=11, total_resources=(10, 10)):
    """Compare live-policy semantics with demo-only Banker avoidance."""
    if n_jobs < 1:
        return {
            "status": "invalid",
            "error": "jobs must be positive",
            "workload": {"jobs": n_jobs, "seed": seed},
            "rows": [],
        }
    workload = generate_workload(n_jobs, seed)
    invalid = _validate_workload(workload, total_resources)
    rows = [] if invalid else [
        _pure_row(policy, workload, total_resources) for policy in PURE_POLICIES
    ]
    return {
        "status": "invalid" if invalid else "completed",
        "error": invalid,
        "workload": {
            "jobs": n_jobs,
            "seed": seed,
            "total_resources": list(total_resources),
            "claim_per_job": [6, 6],
            "phase_requests": [[6, 0], [0, 6]],
        },
        "rows": rows,
    }


def render_pure_markdown(result):
    cols = [
        ("policy", "Policy"), ("completed", "Done"),
        ("throughput_per_tick", "Jobs/tick"), ("avg_response", "Avg response"),
        ("avg_wait", "Resource wait"),
        ("p95_wait", "P95 resource wait"), ("avg_turnaround", "Turnaround"),
        ("service_ticks", "Service ticks"), ("makespan_ticks", "Makespan"),
        ("synthetic_deadlocks", "Synthetic deadlocks"),
        ("synthetic_rollbacks", "Synthetic rollbacks"),
        ("unsafe_grants_prevented", "Unsafe grant refusal attempts"),
    ]
    lines = [
        "| " + " | ".join(label for _, label in cols) + " |",
        "|" + "|".join("---" for _ in cols) + "|",
    ]
    if result["status"] != "completed":
        return (
            "## Pure policy and Banker comparison\n\n"
            f"Status: {result['status']} — {result['error']}\n"
        )
    for row in result["rows"]:
        lines.append("| " + " | ".join(str(row.get(key, "-")) for key, _ in cols) + " |")
    workload = result["workload"]
    parameters = (
        f"Workload: {workload['jobs']} jobs, seed {workload['seed']}, "
        f"resources {workload['total_resources']}, claim {workload['claim_per_job']}, "
        f"two-phase requests {workload['phase_requests']}. "
        "Deadlock and rollback counts are synthetic simulation events, not "
        "live-engine, WFG, or database observations. Resource wait includes "
        "both initial and follow-up requests; P95 uses nearest-rank."
    )
    return "## Pure policy and Banker comparison\n\n" + "\n".join(lines) + "\n\n" + parameters + "\n"


def main():
    ap = argparse.ArgumentParser(description="TransactAlloc evaluation harness")
    ap.add_argument("--jobs", type=int, default=30, help="jobs per policy run")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", default=None, help="write a markdown table here")
    ap.add_argument("--pure", action="store_true",
                    help="run database-free policy/Banker comparison")
    args = ap.parse_args()

    if args.pure:
        table = render_pure_markdown(run_pure_comparison(args.jobs, args.seed))
        print(table)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(table)
            print(f"[eval] wrote {args.out}")
        return

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
