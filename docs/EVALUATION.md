## Performance evaluation

The repository has two evaluation modes. The database-backed mode below is a
historical live-application result and requires PostgreSQL. The pure mode is
reproducible and database-free.

### Pure policy and Banker comparison

```powershell
py -3 evaluate.py --pure --jobs 12 --seed 11
```

The default pure workload uses 12 jobs, seed 11, resources `[10, 10]`, a
`[6, 6]` claim per job, and alternating two-phase requests `[6, 0]` then
`[0, 6]`. It compares FCFS, priority, round-robin with a one-tick quantum,
and demo-only Banker avoidance using the same dispatch model.

Metrics include response time, resource wait for initial and follow-up
requests, nearest-rank P95 resource wait, turnaround, service ticks,
makespan, throughput in jobs per tick, and Banker unsafe-grant refusal
attempts. Pure deadlock and rollback counts are explicitly **synthetic
simulation events**, not live PostgreSQL, WFG, or database observations.
Invalid or stalled workloads are returned as controlled results.

The database-free regression command is:

```powershell
py -3 -m pytest tests\test_evaluate.py tests\test_pure.py tests\test_cli.py tests\test_engine.py -q
```

### Database-backed results

| Policy | Done | Jobs/s | Avg wait (s) | P95 wait (s) | Avg turn (s) | Peak frag | Defrags | Alloc txn (ms) | Deadlocks |
|---|---|---|---|---|---|---|---|---|---|
| fcfs | 20 | 0.45 | 18.63 | 40.94 | 28.2 | 0.5 | 0 | 17.95 | 0 |
| priority | 20 | 0.4 | 18.13 | 40.13 | 27.72 | 0.4286 | 0 | 17.65 | 0 |
| round-robin | 20 | 0.38 | 16.77 | 41.4 | 28.79 | 0.5 | 1 | 16.7 | 0 |

Workload: 20 jobs per policy, 4 workers, seed 11, best-fit placement. Generated 2026-10-16 00:30.
