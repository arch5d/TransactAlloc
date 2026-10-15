"""Report section 4.1 -- the four scheduling / placement policies.

Plain functions, no class hierarchy: three of them order a list of queued jobs,
the fourth picks a node for one job. A job is a plain dict as loaded by
scheduler.load_queued().

Run this file directly to check the policy logic without a database:
    py -3 policies.py
"""
import time

QUANTUM_SEC = 5        # report 4.1: Round Robin time slice
AGING_STEP_SEC = 10    # every 10s spent waiting, a job climbs one priority level


def fcfs(jobs):
    """First-Come First-Served: arrival order, non-preemptive."""
    return sorted(jobs, key=lambda j: j["submitted_at"])


def effective_priority(job, now=None):
    """Priority with aging. Lower number = higher priority (report 4.1: 1..10).

    Without aging, an unbroken stream of priority-1 jobs starves a priority-10
    job forever. Aging lets a long-waiting job climb until it outranks them,
    which bounds the wait instead of merely making it unlikely.
    """
    now = time.time() if now is None else now
    waited = max(0.0, now - job["submitted_at"])
    return max(1, job["priority"] - int(waited // AGING_STEP_SEC))


def priority(jobs, now=None):
    now = time.time() if now is None else now
    return sorted(jobs, key=lambda j: (effective_priority(j, now), j["submitted_at"]))


def round_robin(jobs, quantum=QUANTUM_SEC):
    """Yield (job, slice_seconds) until every job has burned its remaining time.

    ponytail: this preempts the *simulated* duration, not a real process. Real
    preemption would mean SIGSTOP/SIGCONT on a child process; upgrade there if
    the workers ever stop being time.sleep().
    """
    pending = [dict(j) for j in fcfs(jobs)]
    while pending:
        job = pending.pop(0)
        slice_sec = min(quantum, job["remaining_sec"])
        job["remaining_sec"] -= slice_sec
        yield job, slice_sec
        if job["remaining_sec"] > 0:
            pending.append(job)          # back of the queue


def best_fit(job, machines):
    """Report 4.1: place the job on the node leaving the SMALLEST leftover.

    machines: [{machine_id, total_cores, free_cores, total_ram_mb, free_ram_mb}, ...]
    Returns a machine_id, or None if the job fits nowhere.

    Cores and megabytes are divided by that node's own capacity before being
    added, so "1 core left over" is not weighed against "1 MB left over".
    """
    fits = [m for m in machines
            if m["free_cores"] >= job["req_cpus"]
            and m["free_ram_mb"] >= job["req_ram_mb"]]
    if not fits:
        return None

    def leftover(m):
        return ((m["free_cores"] - job["req_cpus"]) / m["total_cores"]
                + (m["free_ram_mb"] - job["req_ram_mb"]) / m["total_ram_mb"])

    return min(fits, key=leftover)["machine_id"]


def first_fit(job, machines):
    """Placement used by every policy except best-fit: first node that fits."""
    for m in machines:
        if m["free_cores"] >= job["req_cpus"] and m["free_ram_mb"] >= job["req_ram_mb"]:
            return m["machine_id"]
    return None


def _self_check():
    now = 1_000_000.0

    a = dict(job_id=1, priority=1, submitted_at=now,        req_cpus=4, req_ram_mb=8192,
             remaining_sec=12)
    b = dict(job_id=2, priority=9, submitted_at=now - 100,  req_cpus=4, req_ram_mb=8192,
             remaining_sec=7)

    # FCFS ignores priority entirely: b arrived 100s earlier, so b goes first.
    assert [j["job_id"] for j in fcfs([a, b])] == [2, 1]

    # Priority without aging would put a (priority 1) first. b has waited 100s,
    # which is 10 aging steps, so 9 - 10 clamps to 1 and its earlier arrival wins.
    assert effective_priority(b, now) == 1
    assert effective_priority(a, now) == 1
    assert [j["job_id"] for j in priority([a, b], now)] == [2, 1]

    # A freshly submitted low-priority job stays behind a high-priority one.
    fresh = dict(job_id=3, priority=9, submitted_at=now, req_cpus=1, req_ram_mb=1,
                 remaining_sec=1)
    assert [j["job_id"] for j in priority([fresh, a], now)] == [1, 3]

    # Round Robin: 12s and 7s at a 5s quantum -> 5,5,5,2,2 across interleaved turns.
    slices = [(j["job_id"], s) for j, s in round_robin([a, b], quantum=5)]
    assert slices == [(2, 5), (1, 5), (2, 2), (1, 5), (1, 2)], slices
    assert sum(s for _, s in slices) == 12 + 7

    # Best-Fit vs First-Fit: the job fits on both nodes, but node 2 is the
    # tighter fit, so first-fit and best-fit must disagree here.
    nodes = [dict(machine_id=1, total_cores=20, free_cores=20,
                  total_ram_mb=65536, free_ram_mb=65536),
             dict(machine_id=2, total_cores=20, free_cores=6,
                  total_ram_mb=65536, free_ram_mb=16384)]
    assert first_fit(a, nodes) == 1
    assert best_fit(a, nodes) == 2

    # Nothing fits -> None, not an exception.
    huge = dict(job_id=4, req_cpus=999, req_ram_mb=1)
    assert best_fit(huge, nodes) is None
    assert first_fit(huge, nodes) is None

    print("policies self-check OK "
          "(fcfs, aging, priority ordering, round-robin slicing, best-fit vs first-fit)")


if __name__ == "__main__":
    _self_check()
