# TransactAlloc — ER Diagram

Two ways to read the same model: a Mermaid diagram (renders on GitHub / in
any Mermaid-aware viewer) and a plain-text version for the report.

## Mermaid

```mermaid
erDiagram
    USERS ||--o{ JOBS : "submits"
    MACHINES ||--|| CPU_RESOURCES : "has 1:1"
    MACHINES ||--|| MEMORY_RESOURCES : "has 1:1"
    MACHINES ||--o{ MEMORY_BLOCKS : "is partitioned into"
    MACHINES ||--o{ ALLOCATIONS : "hosts"
    MACHINES ||--o{ DEFRAG_EVENTS : "is defragmented by"
    MACHINES ||--o{ METRICS_SAMPLES : "sampled by"
    JOBS ||--o{ ALLOCATIONS : "holds"
    JOBS ||--o{ EXECUTION_LOGS : "emits"
    JOBS ||--o{ MEMORY_BLOCKS : "owns (nullable)"
    ALLOCATIONS ||--|| MEMORY_BLOCKS : "carves (1 alloc = 1 block)"
    DEFRAG_EVENTS ||--o{ DEFRAG_MOVES : "records"
    MEMORY_BLOCKS ||--o{ DEFRAG_MOVES : "is moved by"

    USERS {
        int  user_id PK
        text username UK
        text role
        int  quota_cpus
        int  quota_ram_mb
    }
    MACHINES {
        int  machine_id PK
        text hostname UK
        inet ip_address
        text status
    }
    CPU_RESOURCES {
        int  cpu_id PK
        int  machine_id FK,UK
        int  total_cores
        int  allocated_cores
        numeric clock_speed_ghz
    }
    MEMORY_RESOURCES {
        int  mem_id PK
        int  machine_id FK,UK
        int  total_ram_mb
        int  allocated_ram_mb
        int  swap_mb
    }
    JOBS {
        int  job_id PK
        int  user_id FK
        text task_name
        int  req_cpus
        int  req_ram_mb
        int  priority
        int  duration_sec
        int  remaining_sec
        text status
        timestamptz submitted_at
        timestamptz started_at
        timestamptz completed_at
    }
    ALLOCATIONS {
        int  alloc_id PK
        int  job_id FK
        int  machine_id FK
        int  alloc_cpus
        int  alloc_ram_mb
        timestamptz allocated_at
        timestamptz released_at
    }
    MEMORY_BLOCKS {
        int  block_id PK
        int  machine_id FK
        int  start_mb
        int  size_mb
        text state
        int  alloc_id FK,UK
        int  job_id FK
    }
    DEFRAG_EVENTS {
        int  event_id PK
        int  machine_id FK
        text triggered_by
        text status
        numeric frag_before
        numeric frag_after
        int  blocks_moved
        int  mb_moved
        int  duration_ms
    }
    DEFRAG_MOVES {
        int  move_id PK
        int  event_id FK
        int  block_id FK
        int  from_start_mb
        int  to_start_mb
        int  size_mb
        int  seq
    }
    EXECUTION_LOGS {
        int  log_id PK
        int  job_id FK
        timestamptz ts
        text log_level
        text message
    }
    METRICS_SAMPLES {
        int  sample_id PK
        timestamptz ts
        int  machine_id FK
        int  used_cores
        int  used_ram_mb
        int  frag_index
    }
```

Standalone audit tables with no foreign keys (by design — they must survive the
deletion of the rows they describe):

```mermaid
erDiagram
    TXN_LOG {
        int  txn_id PK
        int  job_id "nullable, SET NULL on delete"
        int  machine_id "loose reference"
        text op
        text outcome
        text reason
        int  duration_ms
        timestamptz ts
    }
    SYSTEM_EVENTS {
        int  event_id PK
        timestamptz ts
        text kind
        text severity
        text message
    }
    POLICY_CHANGES {
        int  change_id PK
        timestamptz ts
        text from_policy
        text to_policy
        text reason
    }
    ENGINE_SAMPLE {
        int  sample_id PK
        timestamptz ts
        int  queue_depth
        int  running_jobs
        text policy
    }
```

## Plain-text version

```
                     +-----------+
                     |   USERS   |
                     +-----------+
                          | 1
                          | submits
                          | N
                     +-----------+
                     |   JOBS    |------< EXECUTION_LOGS   (1:N, cascade)
                     +-----------+------< ALLOCATIONS       (1:N, cascade)
                          |                    |
                          | 0..1 owns          | 1 carves
                          v                    v
                     +------------------------------+
                     |        MEMORY_BLOCKS         |  partitions a cluster's
                     | (start_mb, size_mb, state)   |  [0, total_ram_mb)
                     +------------------------------+
                          ^ N
                          | 1
   +-----------+     +-----------+
   | CPU_RES.  |1:1  | MACHINES  |1:1  | MEMORY_RES. |
   +-----------+-----+-----------+-----+-------------+
                          | 1
        +-----------------+------------------+
        | N                                  | N
   +--------------+                  +----------------+
   | ALLOCATIONS  |                  | DEFRAG_EVENTS  |
   +--------------+                  +----------------+
                                            | 1
                                            | N
                                     +--------------+
                                     | DEFRAG_MOVES |
                                     +--------------+
```

## Cardinalities that matter

| Relationship | Cardinality | Why |
|---|---|---|
| `Users → Jobs` | 1:N | one submitter, many jobs; quota is enforced over this set |
| `Machines → CPU_Resources` / `Memory_Resources` | 1:1 | a cluster has exactly one CPU pool and one RAM pool (UNIQUE `machine_id`) |
| `Jobs → Allocations` | 1:N | a job can hold resources on more than one cluster over its life (and, in a split allocation, at once) |
| `Allocations → Memory_Blocks` | 1:1 | each allocation carves exactly one contiguous block; a NULL-`alloc_id` block is free RAM |
| `Machines → Memory_Blocks` | 1:N | the block map is a partition, so this is total and disjoint |
| `Memory_Blocks → Defrag_Moves` | 1:N | one block can appear in many past defragment runs |
| `Defrag_Events → Defrag_Moves` | 1:N | a run is an ordered list of moves (`seq`) |

## The one modelling decision worth defending

RAM is **not** a counter. It is a set of `Memory_Blocks` that always partitions
`[0, total_ram_mb)`. That is what makes external fragmentation a *query*
(`v_cluster_status.frag_index`) instead of an estimate, and what makes
"allocate the best-fit hole" or "move block X to offset Y" expressible at all.
CPU stays a counter because cores are fungible and never fragment.
