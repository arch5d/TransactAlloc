# TransactAlloc — Schema Reference

Database: PostgreSQL 17. SQL source of truth: [`../schema.sql`](../schema.sql),
seed data: [`../seed.sql`](../seed.sql).

The schema is normalised to **3NF**. Two columns are a documented, controlled
exception (see [Denormalisation](#denormalisation)).

---

## 1. Core entities

### `Users`
Who submits work, under what quota.

| Column | Type | Notes |
|---|---|---|
| `user_id` | `SERIAL PK` | |
| `username` | `TEXT UNIQUE NOT NULL` | |
| `role` | `TEXT` | `CHECK (role IN ('Admin','User'))` |
| `quota_cpus` | `INT NOT NULL` | `>= 0` |
| `quota_ram_mb` | `INT NOT NULL` | `>= 0` |

### `Machines` (the four clusters)
| Column | Type | Notes |
|---|---|---|
| `machine_id` | `SERIAL PK` | |
| `hostname` | `TEXT UNIQUE NOT NULL` | e.g. `cluster-alpha` |
| `ip_address` | `INET NOT NULL` | |
| `status` | `TEXT` | `ONLINE` / `OFFLINE` / `MAINTENANCE` |

### `CPU_Resources` (1:1 with `Machines`)
| Column | Type | Notes |
|---|---|---|
| `cpu_id` | `SERIAL PK` | |
| `machine_id` | `INT UNIQUE FK → Machines ON DELETE CASCADE` | |
| `total_cores` | `INT NOT NULL > 0` | |
| `allocated_cores` | `INT DEFAULT 0` | the `FOR UPDATE` lock target |
| `clock_speed_ghz` | `NUMERIC(4,2)` | |

Constraint: `CHECK (allocated_cores <= total_cores)` — **the database itself**
refuses to overcommit CPU.

### `Memory_Resources` (1:1 with `Machines`)
| Column | Type | Notes |
|---|---|---|
| `mem_id` | `SERIAL PK` | |
| `machine_id` | `INT UNIQUE FK → Machines ON DELETE CASCADE` | |
| `total_ram_mb` | `INT NOT NULL > 0` | |
| `allocated_ram_mb` | `INT DEFAULT 0` | the `FOR UPDATE` lock target |
| `swap_mb` | `INT DEFAULT 0` | |

Constraint: `CHECK (allocated_ram_mb <= total_ram_mb)`.

### `Jobs`
| Column | Type | Notes |
|---|---|---|
| `job_id` | `SERIAL PK` | |
| `user_id` | `INT FK → Users` | |
| `task_name` | `TEXT NOT NULL` | |
| `req_cpus` / `req_ram_mb` | `INT NOT NULL > 0` | the request |
| `priority` | `INT DEFAULT 5` | `BETWEEN 1 AND 10` |
| `duration_sec` | `INT NOT NULL > 0` | nominal run time |
| `remaining_sec` | `INT DEFAULT 0` | burned down one quantum at a time by Round-Robin |
| `status` | `TEXT DEFAULT 'QUEUED'` | `QUEUED` / `RUNNING` / `COMPLETED` / `FAILED` |
| `submitted_at` | `TIMESTAMPTZ DEFAULT NOW()` | wait-time base |
| `started_at` / `completed_at` | `TIMESTAMPTZ` | turnaround / throughput |

Trigger `trg_jobs_default_remaining`: on insert, if `remaining_sec = 0` set it
to `duration_sec`, so no `INSERT` site can forget to initialise it.

### `Allocations`
What a job currently (or previously) held on one cluster.

| Column | Type | Notes |
|---|---|---|
| `alloc_id` | `SERIAL PK` | |
| `job_id` | `INT FK → Jobs ON DELETE CASCADE` | |
| `machine_id` | `INT FK → Machines` | |
| `alloc_cpus` / `alloc_ram_mb` | `INT >= 0` | |
| `allocated_at` / `released_at` | `TIMESTAMPTZ` | `released_at IS NULL` = live |
| — | — | `CHECK (alloc_cpus + alloc_ram_mb > 0)` |

---

## 2. The block map — fragmentation made queryable

### `Memory_Blocks`
For each cluster, the rows of this table **partition** `[0, total_ram_mb)`:
ordered, contiguous, no gap, no overlap.

| Column | Type | Notes |
|---|---|---|
| `block_id` | `SERIAL PK` | |
| `machine_id` | `INT FK → Machines ON DELETE CASCADE` | |
| `start_mb` | `INT >= 0` | offset |
| `size_mb` | `INT > 0` | length |
| `state` | `TEXT` | `FREE` / `ALLOCATED` |
| `alloc_id` | `INT UNIQUE FK → Allocations ON DELETE CASCADE` | NULL when free |
| `job_id` | `INT FK → Jobs ON DELETE SET NULL` | convenience denorm |

Constraints:
- `CHECK ((state='ALLOCATED') = (alloc_id IS NOT NULL))` — state and link agree.
- `UNIQUE (machine_id, start_mb) DEFERRABLE INITIALLY DEFERRED` — defrag
  rewrites offsets in place, so two blocks may transiently share an offset
  mid-transaction; consistency is required only at `COMMIT`.

The partition invariant is verified on demand by `ValidateBlockMap()` (§4).

### `v_cluster_status` (view)
One row per cluster: capacity, free, `holes`, `largest_free_mb`, and

```
frag_index = 1 - (largest_free_mb / total_free_mb)     -- 0.0 = perfect
```

`total_free = 0` is defined as `frag_index = 0.0`: a *full* cluster is not a
*fragmented* one. `frag_index` climbing toward `1.0` means the free RAM is
shattered into pieces no job can use — the signal the defragmenter acts on.

---

## 3. Defragmentation history

### `Defrag_Events`
| Column | Type | Notes |
|---|---|---|
| `event_id` | `SERIAL PK` | |
| `machine_id` | `INT FK → Machines` | |
| `triggered_by` | `TEXT` | `AUTO` / `MANUAL` / `ON_DEMAND` |
| `status` | `TEXT` | `RUNNING` / `COMMITTED` / `ROLLED_BACK` |
| `reason` | `TEXT` | |
| `holes_before`, `largest_before_mb`, `frag_before` | | before snapshot |
| `holes_after`, `largest_after_mb`, `frag_after` | | after snapshot |
| `blocks_moved`, `mb_moved`, `duration_ms` | `INT` | cost of the run |
| `started_at`, `finished_at` | `TIMESTAMPTZ` | |

### `Defrag_Moves`
The ordered plan actually executed: `event_id`, `block_id`, `from_start_mb`,
`to_start_mb`, `size_mb`, `seq`.

---

## 4. Audit & observability

| Table | Purpose | Key columns |
|---|---|---|
| `Txn_Log` | **ACID evidence trail** — one row per resource transaction | `op` (`ALLOCATE`/`RELEASE`/`DEFRAG`/`DEADLOCK_ROLLBACK`/`RESET`), `outcome` (`COMMIT`/`ROLLBACK`), `reason`, `duration_ms` |
| `System_Events` | cluster narrative for the dashboard feed | `kind` (`ENGINE`/`POLICY`/`DEADLOCK`/`DEFRAG`/`SUBMIT`/`QUOTA`/`ERROR`), `severity` |
| `Policy_Changes` | why the adaptive scheduler switched | `from_policy`, `to_policy`, `reason` |
| `Execution_Logs` | per-job log lines | `job_id` (cascade), `log_level`, `message` |
| `Metrics_Samples` | time series per cluster for sparklines / evaluation | `used_cores`, `used_ram_mb`, `frag_index` |
| `Engine_Sample` | time series of the engine itself | `queue_depth`, `running_jobs`, `policy` |

`Txn_Log.job_id` is `ON DELETE SET NULL` and `machine_id` is a loose reference
(no FK) on purpose: the audit trail must outlive the rows it describes.

Indexes: `idx_jobs_status_time`, `idx_alloc_active` (partial, `released_at IS NULL`),
`idx_blocks_lookup`, `idx_events_ts`, `idx_joblogs_ts`, `idx_metrics_ts`, `idx_txn_ts`.

---

## 5. Functions (the transactional core)

| Function | Signature | Contract |
|---|---|---|
| `AllocateJobResources` | `(job_id, machine_id, cpus, ram) → alloc_id` | One ACID unit. `FOR UPDATE` locks both aggregate rows, checks capacity, best-fits a contiguous free hole `FOR UPDATE`, carves it (remainder becomes a new FREE block), bumps aggregates and job state. Raises `INSUFFICIENT_RESOURCES` or `FRAGMENTED` — never returns `FALSE`, so the caller's transaction truly rolls back. |
| `ReleaseJobResources` | `(job_id) → bool` | Mirror image. Locks the aggregate rows `CPU → Memory` **first** (same order as allocate, so the two can never form a lock cycle), then marks blocks FREE, decrements aggregates, stamps `released_at`, and `CoalesceFreeBlocks`. Deliberately does **not** touch job status — completion, RR preemption and deadlock rollback all release but differ in what the job becomes. |
| `CoalesceFreeBlocks` | `(machine_id) → void` | Merges adjacent FREE blocks to a fixed point. Runs after every release so free space heals instead of shattering. |
| `ValidateBlockMap` | `(machine_id) → bool` | Enforces the partition invariant (total, contiguous, allocated-sum matches aggregate). Raises `BLOCK_MAP_INVALID` rather than returning false. |
| `ResetCluster` | `(restart_jobs_at INT DEFAULT NULL) → void` | Returns every cluster to a single FREE block, clears history, optionally reseeds `jobs_job_id_seq`. The single reset path for CLI, demo and API. |
| `jobs_default_remaining` | trigger fn | Initialises `remaining_sec` on insert. |

`RAISE EXCEPTION` is load-bearing: it is what makes the Python side's
`INSUFFICIENT_RESOURCES:` / `FRAGMENTED:` prefix test work and what guarantees
the transaction rolls back rather than committing a half-done carve.

---

## Denormalisation

`CPU_Resources.allocated_cores` and `Memory_Resources.allocated_ram_mb` are
derivable (`SUM` over live `Allocations`). They are kept because:

1. They are written **in the same transaction** as the detail rows they
   summarise, so committed state can never disagree.
2. They are the rows `AllocateJobResources` takes its `FOR UPDATE` lock on —
   the serialisation point that closes the lost-update race under concurrent
   workers.
3. `ValidateBlockMap` cross-checks them against the block map.

`Memory_Blocks.job_id` is likewise a convenience denormalisation of
`Allocations.job_id`; it is kept consistent by the allocation functions and
nulled automatically (`ON DELETE SET NULL`) when a job disappears.

This is *controlled* redundancy with an in-database consistency check, not an
unmanaged cache.
