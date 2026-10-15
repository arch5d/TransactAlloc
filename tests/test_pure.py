"""Pure-logic tests: no database. Reuses each module's own self-check plus
the adaptive selector."""
import time

import adaptive
import policies
import wfg


def test_policies_self_check():
    policies._self_check()


def test_wfg_self_check():
    wfg._self_check()


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
