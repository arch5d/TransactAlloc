"""Fragmentation: block map queries, defragmentation plan and apply.

CPU never fragments in this model. RAM is a list of Memory_Blocks forming a
contiguous partition [0, total_ram_mb). The external fragmentation is the
standard: 1 - largest_free / total_free.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Hole:
    block_id: int
    start_mb: int
    size_mb: int


def cluster_status(conn, machine_id: int):
    row = conn.execute(
        """
        SELECT machine_id, hostname, total_ram_mb, free_ram_mb,
               holes, largest_free_mb, frag_index
          FROM v_cluster_status
         WHERE machine_id = %s
        """,
        (machine_id,),
    ).fetchone()
    if not row:
        return None
    cols = ("machine_id", "hostname", "total_ram_mb", "free_ram_mb",
            "holes", "largest_free_mb", "frag_index")
    if isinstance(row, dict):
        return row
    return dict(zip(cols, row))


def free_holes(conn, machine_id: int):
    rows = conn.execute(
        """
        SELECT block_id, start_mb, size_mb
          FROM Memory_Blocks
         WHERE machine_id = %s AND state = 'FREE'
         ORDER BY start_mb
        """,
        (machine_id,),
    ).fetchall()
    return [Hole(b, s, sz) for b, s, sz in rows]


def allocated_blocks(conn, machine_id: int):
    rows = conn.execute(
        """
        SELECT block_id, start_mb, size_mb, alloc_id, job_id
          FROM Memory_Blocks
         WHERE machine_id = %s AND state = 'ALLOCATED'
         ORDER BY start_mb
        """,
        (machine_id,),
    ).fetchall()
    return rows


def total_free(conn, machine_id: int):
    row = conn.execute(
        "SELECT total_ram_mb - allocated_ram_mb FROM Memory_Resources WHERE machine_id=%s",
        (machine_id,),
    ).fetchone()
    return row[0] if row else 0


def defrag_plan(conn, machine_id: int) -> list[dict]:
    """Plan: compact allocated blocks toward start 0 (first-fit by address).
    Returns moves: {block_id, from_start_mb, to_start_mb, size_mb, seq}.
    No writes -- pure plan.
    """
    allocs = allocated_blocks(conn, machine_id)
    moves = []
    target = 0
    for blk in allocs:
        b, s, sz, _, _ = blk
        if s != target:
            moves.append(
                dict(block_id=b, from_start_mb=s, to_start_mb=target, size_mb=sz, seq=len(moves)+1)
            )
        target += sz
    return moves


def apply_defrag(conn, machine_id: int, moves: list[dict], triggered_by: str = "MANUAL", reason: str | None = None) -> int:
    """Apply a compacting defragmentation in the caller's transaction.
    Creates Defrag_Events + Defrag_Moves, rewrites start_mb of moved blocks,
    rebuilds the FREE tail, coalesces, and validates the map. Returns event_id.
    """
    st = cluster_status(conn, machine_id)
    holes_before = st["holes"]
    largest_before = st["largest_free_mb"]
    frag_before = float(st["frag_index"]) if st["frag_index"] is not None else 0.0

    event_id = conn.execute(
        """
        INSERT INTO Defrag_Events (machine_id, triggered_by, status,
                                   holes_before, largest_before_mb, frag_before,
                                   holes_after, largest_after_mb, frag_after,
                                   blocks_moved, mb_moved, started_at)
        VALUES (%s, %s, 'RUNNING', %s, %s, %s, 0, 0, 0, %s, %s, NOW())
        RETURNING event_id
        """,
        (machine_id, triggered_by, holes_before, largest_before, frag_before, len(moves), sum(m["size_mb"] for m in moves)),
    ).fetchone()[0]

    if reason:
        conn.execute("UPDATE Defrag_Events SET reason=%s WHERE event_id=%s", (reason, event_id))

    for mv in moves:
        conn.execute(
            """
            UPDATE Memory_Blocks SET start_mb=%s
             WHERE block_id=%s AND machine_id=%s
            """,
            (mv["to_start_mb"], mv["block_id"], machine_id),
        )
        conn.execute(
            """
            INSERT INTO Defrag_Moves (event_id, block_id, from_start_mb, to_start_mb, size_mb, seq)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (event_id, mv["block_id"], mv["from_start_mb"], mv["to_start_mb"], mv["size_mb"], mv["seq"]),
        )

    # rebuild FREE block: everything from last allocated end to total
    total = st["total_ram_mb"]
    last = 0
    for blk in allocated_blocks(conn, machine_id):
        _, s, sz, _, _ = blk
        last = max(last, s + sz)
    # remove old FREE blocks and create tail if any
    conn.execute(
        "DELETE FROM Memory_Blocks WHERE machine_id=%s AND state='FREE'",
        (machine_id,),
    )
    if last < total:
        conn.execute(
            "INSERT INTO Memory_Blocks (machine_id, start_mb, size_mb, state) VALUES (%s, %s, %s, 'FREE')",
            (machine_id, last, total - last),
        )
    # coalesce (should be trivial)
    conn.execute("SELECT CoalesceFreeBlocks(%s)", (machine_id,))
    # validate
    conn.execute("SELECT ValidateBlockMap(%s)", (machine_id,))

    st2 = cluster_status(conn, machine_id)
    holes_after = st2["holes"]
    largest_after = st2["largest_free_mb"]
    frag_after = float(st2["frag_index"]) if st2["frag_index"] is not None else 0.0
    conn.execute(
        """
        UPDATE Defrag_Events
           SET status='COMMITTED', holes_after=%s, largest_after_mb=%s,
               frag_after=%s, finished_at=NOW(),
               duration_ms=EXTRACT(MILLISECOND FROM (NOW()-started_at))::int
         WHERE event_id=%s
        """,
        (holes_after, largest_after, frag_after, event_id),
    )
    return event_id
