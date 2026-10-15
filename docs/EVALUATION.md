## Performance evaluation

| Policy | Done | Jobs/s | Avg wait (s) | P95 wait (s) | Avg turn (s) | Peak frag | Defrags | Alloc txn (ms) | Deadlocks |
|---|---|---|---|---|---|---|---|---|---|
| fcfs | 20 | 0.45 | 18.63 | 40.94 | 28.2 | 0.5 | 0 | 17.95 | 0 |
| priority | 20 | 0.4 | 18.13 | 40.13 | 27.72 | 0.4286 | 0 | 17.65 | 0 |
| round-robin | 20 | 0.38 | 16.77 | 41.4 | 28.79 | 0.5 | 1 | 16.7 | 0 |

Workload: 20 jobs per policy, 4 workers, seed 11, best-fit placement. Generated 2026-10-16 00:30.
