## Performance evaluation

| Policy | Done | Jobs/s | Avg wait (s) | P95 wait (s) | Avg turn (s) | Peak frag | Defrags | Alloc txn (ms) | Deadlocks |
|---|---|---|---|---|---|---|---|---|---|
| fcfs | 16 | 0.37 | 14.45 | 34.71 | 24.29 | 0.375 | 1 | 12.88 | 0 |
| priority | 16 | 0.38 | 14.7 | 34.17 | 24.63 | 0.5 | 0 | 14.06 | 0 |
| round-robin | 16 | 0.4 | 12.25 | 30.32 | 26.04 | 0.4545 | 2 | 16.39 | 0 |

Workload: 16 jobs per policy, 4 workers, seed 11, best-fit placement. Generated 2026-10-16 00:26.
