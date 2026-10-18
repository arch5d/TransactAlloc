import evaluate


def _job(job_id, priority=5, initial=(6, 0), followup=(0, 6), duration=1):
    return {
        "job_id": job_id,
        "submitted_at": 0,
        "priority": priority,
        "duration": duration,
        "initial": list(initial),
        "followup": list(followup),
        "claim": [initial[0] + followup[0], initial[1] + followup[1]],
    }


def test_dispatch_order_matches_fcfs_and_priority():
    jobs = [_job(1, priority=9), _job(2, priority=1)]

    fcfs = evaluate._pure_row("fcfs", jobs, [10, 10])
    priority = evaluate._pure_row("priority", jobs, [10, 10])

    assert fcfs["initial_dispatch_order"][:2] == [1, 2]
    assert priority["initial_dispatch_order"][:2] == [2, 1]


def test_round_robin_requeues_unfinished_jobs():
    jobs = [_job(1, duration=3), _job(2, duration=1)]

    row = evaluate._pure_row("round-robin", jobs, [10, 10])

    assert row["status"] == "completed"
    assert row["dispatch_trace"][:4] == [1, 2, 1, 2]
    assert row["dispatch_trace"].count(1) > 1


def test_resource_conservation_and_metrics_use_makespan():
    row = evaluate._pure_row(
        "fcfs", [_job(1, duration=2, initial=(3, 0), followup=(0, 3))], [10, 10]
    )

    assert row["status"] == "completed"
    assert row["final_available"] == [10, 10]
    assert min(row["minimum_available"]) >= 0
    assert row["service_ticks"] == 2
    assert row["makespan_ticks"] == 3
    assert row["avg_response"] == 0
    assert row["avg_turnaround"] == 3
    assert row["throughput_per_tick"] == round(1 / 3, 4)
    assert row["avg_wait"] == 1
    assert row["p95_wait"] == 1


def test_banker_refuses_unsafe_request_then_makes_progress():
    result = evaluate.run_pure_comparison(n_jobs=8, seed=11)
    rows = {row["policy"]: row for row in result["rows"]}

    assert rows["banker"]["status"] == "completed"
    assert rows["banker"]["completed"] == 8
    assert rows["banker"]["synthetic_deadlocks"] == 0
    assert rows["banker"]["synthetic_rollbacks"] == 0
    assert rows["banker"]["unsafe_grants_prevented"] > 0
    assert rows["fcfs"]["synthetic_deadlocks"] > 0
    assert rows["fcfs"]["synthetic_rollbacks"] == rows["fcfs"]["synthetic_deadlocks"]


def test_invalid_workload_returns_controlled_result():
    result = evaluate.run_pure_comparison(n_jobs=2, seed=11, total_resources=(1, 1))

    assert result["status"] == "invalid"
    assert result["rows"] == []
    assert "claims exceed total resources" in result["error"]

    empty = evaluate.run_pure_comparison(n_jobs=0, seed=11)
    assert empty["status"] == "invalid"
    assert empty["error"] == "jobs must be positive"


def test_pure_comparison_is_reproducible_and_reports_workload():
    first = evaluate.run_pure_comparison(n_jobs=8, seed=19)
    second = evaluate.run_pure_comparison(n_jobs=8, seed=19)

    assert first == second
    assert [row["policy"] for row in first["rows"]] == [
        "fcfs", "priority", "round-robin", "banker",
    ]
    assert first["workload"]["seed"] == 19
    assert first["workload"]["total_resources"] == [10, 10]


def test_pure_markdown_identifies_synthetic_metrics_and_definitions():
    output = evaluate.render_pure_markdown(
        evaluate.run_pure_comparison(n_jobs=4, seed=23)
    )

    assert "Synthetic deadlocks" in output
    assert "Synthetic rollbacks" in output
    assert "synthetic simulation events" in output
    assert "Resource wait includes both initial and follow-up requests" in output
    assert "P95 uses nearest-rank" in output
