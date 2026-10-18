import argparse
import sys

import main


def test_cmd_run_starts_scheduler_with_bounded_policy_run(monkeypatch):
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeDatabase:
        def connect(self):
            return FakeConnection()

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.submitted = []
            self.rounds = 2
            self.queue = type("Queue", (), {"empty": lambda self: True})()
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def submit_job(self, priority, job):
            self.submitted.append((priority, job))

        def stop(self):
            self.stopped = True

    runners = []

    class FakeScheduler:
        worker = type("Worker", (), {"active_count": staticmethod(lambda: 0)})

        @staticmethod
        def JobScheduler(**kwargs):
            runner = FakeRunner(**kwargs)
            runners.append(runner)
            return runner

        @staticmethod
        def load_queued(conn):
            return [
                {"job_id": 2, "submitted_at": 2, "priority": 5},
                {"job_id": 1, "submitted_at": 1, "priority": 5},
            ]

    monkeypatch.setattr(main, "db", FakeDatabase())
    monkeypatch.setattr(main, "scheduler", FakeScheduler)
    main.cmd_run(argparse.Namespace(
        policy="fcfs", workers=3, max_rounds=4, avoidance="off",
    ))

    runner = runners[0]
    assert runner.kwargs == {"num_workers": 3, "policy": "fcfs", "max_rounds": 4}
    assert [job["job_id"] for _, job in runner.submitted] == [1, 2]
    assert runner.started and runner.stopped


def test_run_parser_accepts_expected_arguments(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "run", "--policy", "best-fit", "--workers", "3",
         "--max-rounds", "7", "--avoidance", "off"],
    )

    args = main.build_parser().parse_args()

    assert args.cmd == "run"
    assert args.policy == "best-fit"
    assert args.workers == 3
    assert args.max_rounds == 7
    assert args.avoidance == "off"


def test_console_main_dispatches_run(monkeypatch):
    expected = argparse.Namespace(
        cmd="run", policy="fcfs", workers=2, max_rounds=4, avoidance="off",
    )
    called = []

    monkeypatch.setattr(main, "build_parser", lambda: argparse.Namespace(
        parse_args=lambda: expected,
    ))
    monkeypatch.setattr(main, "cmd_run", lambda args: called.append(args))

    main.console_main()

    assert called == [expected]
