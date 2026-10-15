"""Adaptive policy selector: chooses per dispatch round."""

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Context:
    queue_depth: int
    running: int
    queued_jobs: list[dict]
    now: float


def choose_policy(ctx: Context) -> tuple[str, str]:
    """Return (policy, reason). policy in {fcfs, priority, round-robin}.
    Best-fit placement is orthogonal.
    """
    if ctx.queue_depth <= 2:
        return "fcfs", "queue very small (<=2) → FCFS"
    # starvation hint: any very old low-priority?
    old = 0
    for j in ctx.queued_jobs:
        waited = max(0.0, ctx.now - j.get("submitted_at", ctx.now))
        if waited > 30 and j.get("priority", 5) >= 8:
            old += 1
    if old >= 2:
        return "priority", f"{old} long-waiting low-priority jobs → PRIORITY (aging enabled)"
    # if many jobs, prefer RR to avoid head-of-line blocking
    if ctx.queue_depth >= 6:
        # heuristic: if avg duration long-ish, RR fairer; keep simple
        return "round-robin", "deep queue (>=6) → Round-Robin for fairness"
    # default
    return "fcfs", "default → FCFS"
