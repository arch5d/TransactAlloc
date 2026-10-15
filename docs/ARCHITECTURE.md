# TransactAlloc — Architecture

An adaptive, fragmentation-aware cloud resource manager. Four clusters; jobs
arrive, are scheduled by a policy that adapts to load, placed on a cluster by
best-fit, allocated through real ACID transactions, and monitored live.

---

## Component map

```
                       browser (static/index.html + app.js)
                                     │  poll /api/state every 1s
                                     ▼
        ┌──────────────────────────────────────────────────────┐
        │                     FastAPI  (app.py, api.py)         │
        │  /api/state /api/jobs /api/jobs/batch /api/policy     │
        │  /api/defrag /api/reset /api/stats /api/events        │
        │  /api/engine/start|stop   /api/deadlock-demo          │
        └───────────────┬──────────────────────────────────────┘
                        │
         ┌──────────────┴───────────────┐
         ▼                              ▼
 ┌───────────────┐             ┌──────────────────┐
 │  Engine       │  adaptive   │  worker pool      │
 │  engine.py    │◄──select───►│  worker.py        │
 │  PriorityQueue│  adaptive.py│  execute_job()    │
 │  mutex+sem    │             │  allocate/run/    │
 │  reconciler   │             │  release/defrag   │
 │  deadlock mon.│             └────────┬──────────┘
 │  sampler      │                      │
 └───────┬───────┘                      │
         │  WaitForGraph (wfg.py)        │
         └──────────────┬───────────────┘
                        ▼
        ┌──────────────────────────────────────┐
        │   PostgreSQL 17   (schema.sql)        │
        │   AllocateJobResources / Release...   │
        │   Memory_Blocks map, v_cluster_status │
        │   audit: Txn_Log, System_Events, ...  │
        └──────────────────────────────────────┘
```

| File | Responsibility |
|---|---|
| `db.py` | single place that knows the DSN; `connect()`, `run_sql_file()`, reset helpers |
| `schema.sql` / `seed.sql` | 3NF schema + transactional functions; 4-cluster seed |
| `engine.py` | the engine: bounded queue, worker pool, adaptive policy selection, reconciler, deadlock monitor, metrics sampler |
| `worker.py` | one job's lifecycle: pick cluster → allocate → run → release, with FRAGMENTED→defrag→retry |
| `frag.py` | fragmentation queries, defrag plan, apply-defrag |
| `adaptive.py` | pure rule function: given queue/running → which policy |
| `policies.py` | FCFS, Priority, Round-Robin (scheduling); best-fit / first-fit (placement) |
| `wfg.py` | wait-for graph, cycle detection, youngest-victim selection |
| `banker.py` | Banker's algorithm (avoidance), kept for the report's comparison |
| `api.py` / `app.py` | HTTP layer and ASGI app |
| `evaluate.py` | drives the app through a workload per policy, emits metrics |
| `demo.py` / `test_deadlock.py` | the report §5 scripted walkthrough |
| `static/` | the dashboard (vanilla HTML/CSS/JS) |

---

## Job lifecycle (happy path)

1. `POST /api/jobs` validates quota, `INSERT`s a `QUEUED` row, and hands the
   in-memory dict to the engine queue (`Engine.submit`).
2. A worker acquires the queue mutex + semaphore, decides the active policy
   (`adaptive.choose_policy` when in `auto`), and calls `worker.execute_job`.
3. `pick_machine` runs **best-fit** over a live `v_cluster_status` snapshot.
4. `try_allocate` calls `AllocateJobResources(...)` inside a transaction. The
   function locks the cluster's aggregate rows `FOR UPDATE`, checks capacity,
   best-fits a contiguous hole `FOR UPDATE`, carves it, and updates the job —
   all or nothing. The outcome (`COMMIT`/`ROLLBACK`) is written to `Txn_Log`.
5. The worker simulates the run in heartbeat ticks, then `ReleaseJobResources`
   frees the block, coalesces neighbours, and marks the job `COMPLETED`.

## Fragmentation path

If `AllocateJobResources` raises `FRAGMENTED` (capacity exists, but no single
hole fits), the worker:

1. logs a `WARN` and a `DEFRAG` system event;
2. calls `frag.defrag_plan` + `apply_defrag` on that cluster (compaction that
   records before/after fragmentation in `Defrag_Events`/`Defrag_Moves`);
3. retries the allocation once;
4. if it still fails, re-queues the job — the reconciler will retry it after
   its blockers finish.

## The in-flight reconciler (why re-queued jobs never strand)

A job that cannot be placed is parked as `QUEUED` in the database. A background
reconciler thread every second re-reads `Jobs WHERE status='QUEUED'` and
re-admits any job the engine is not already tracking (deduped by an in-flight
set). It reads values from the database, so a preempted Round-Robin job resumes
with its *reduced* `remaining_sec`, not its original duration. This also makes
engine restarts lossless: anything `QUEUED` is picked back up.

## Deadlock handling — two layers

**Avoidance** (`banker.py`) — before a grant, check whether some completion
order still exists. The report §5 scenario is exactly a safe-looking grant that
makes the cluster unsafe; the Banker refuses it. Demonstrative, kept for the
avoidance-vs-detection comparison.

**Detection** (`wfg.py` + engine monitor) — let it happen, then recover. Every
time a job is blocked behind holders, `worker` adds edges
`waiter → holder` to the engine's `WaitForGraph`. A monitor thread runs a
three-colour DFS every second; a cycle is a deadlock. The **youngest** job in
the cycle is the victim (least work lost, and a total order so victim choice
cannot itself livelock). The engine rolls it back in one transaction —
`ReleaseJobResources` + status `QUEUED` + `Txn_Log(DEADLOCK_ROLLBACK)` +
`System_Events(DEADLOCK)` — and the survivor proceeds.

Because the wait is *not* a PostgreSQL lock (the job's transaction already
rolled back and it is parked in the application queue while possibly holding
resources elsewhere), only the application can see the cycle. The graph is
therefore maintained at application level, not read from `pg_locks`.

`POST /api/deadlock-demo` runs the report §5 circular wait live through the
real database, the real WFG and a real rollback, and returns the transcript.

## Concurrency & consistency

- **Lost update** is closed by the `FOR UPDATE` locks inside
  `AllocateJobResources`; `_check` + `CHECK` constraints are the backstop.
- **Lock ordering**: allocate and release both take the aggregate rows in the
  same order (`CPU_Resources` → `Memory_Resources`, then block/allocation rows).
  With a consistent order two transactions cannot form a wait cycle, so a
  concurrent allocate and release on one cluster never deadlock at row level.
- **Reentrancy**: the engine serialises queue mutation with a mutex; the WFG
  has its own lock; the deadlock resolver holds a dedicated lock.
- **Graceful stop**: workers share the engine's stop event and abort their
  current job within one heartbeat, releasing resources and parking it
  `QUEUED`. `stop()` then joins every thread, so a following `reset` can never
  race a stale worker.
- **DB-enforced invariants**: `CHECK (allocated <= total)`, the block-map
  partition check, and `CHECK ((state='ALLOCATED') = (alloc_id IS NOT NULL))`.

## Adaptivity

`adaptive.choose_policy(Context)` is a pure function of queue depth, running
count, the queued jobs and the current time:

| Condition | Policy chosen |
|---|---|
| `queue_depth <= 2` | **FCFS** — tiny queue, no reason to reorder |
| ≥ 2 jobs with `priority >= 8` waiting > 30 s | **Priority** (aging) — rescues starvation |
| `queue_depth >= 6` | **Round-Robin** — fairness, avoids head-of-line blocking |
| otherwise | **FCFS** (default) |

Every actual switch is audited in `Policy_Changes` and announced as a
`System_Events` row, so the dashboard's "why did it change?" is answerable from
committed data. Placement (best-fit) is orthogonal to the scheduling policy.
