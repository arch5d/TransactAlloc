"""Report section 4.3 -- deadlock AVOIDANCE (Banker's algorithm).

Cluster resources are one flat vector. For machines 1..M the layout is

    [cores_m1, ram_m1, cores_m2, ram_m2, ...]

so "4 cores on node 1 AND 32 GB on node 2" is a single expressible request.
That matters: the deadlock the report demonstrates in section 5 is a job
holding memory on one node while waiting for cores on another, which a
per-node model cannot represent at all.

Run this file directly to check the algorithm without a database:
    py -3 banker.py
"""


def _le(a, b):
    return all(x <= y for x, y in zip(a, b))


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def safe_sequence(available, max_claim, allocation):
    """Return an order in which every job can finish, or None if none exists.

    available  : vector of free resources right now
    max_claim  : {job_id: vector of that job's maximum total demand}
    allocation : {job_id: vector it currently holds}

    A state is SAFE when such an order exists. Being unsafe is not the same as
    being deadlocked -- it means the cluster could be driven into deadlock if
    every job went on to claim its stated maximum, so the Banker refuses to
    enter it.
    """
    width = len(available)
    work = list(available)
    pending = dict(max_claim)
    order = []

    while pending:
        for job_id, claim in pending.items():
            held = allocation.get(job_id, [0] * width)
            still_needs = _sub(claim, held)
            if _le(still_needs, work):
                # This job can finish; assume it does and hand back what it holds.
                work = _add(work, held)
                order.append(job_id)
                del pending[job_id]
                break
        else:
            return None      # nobody can finish from here -> unsafe
    return order


def is_safe(available, max_claim, allocation):
    return safe_sequence(available, max_claim, allocation) is not None


def grant_is_safe(job_id, request, available, max_claim, allocation):
    """Would granting `request` to `job_id` right now leave the cluster safe?"""
    if not _le(request, available):
        return False                        # cannot be granted at all yet
    width = len(available)
    trial_available = _sub(available, request)
    trial_allocation = dict(allocation)
    trial_allocation[job_id] = _add(trial_allocation.get(job_id, [0] * width), request)
    return is_safe(trial_available, max_claim, trial_allocation)


def _self_check():
    # ---- the cluster from seed.sql: 2 nodes, 20 cores and 65536 MB each ----
    # vector layout: [cores_n1, ram_n1, cores_n2, ram_n2]
    total = [20, 65536, 20, 65536]

    # The exact scenario from report section 5.
    #   Job 121 wants 12 cores on node 1, and holds/needs 32 GB on node 2
    #   Job 122 wants 10 cores on node 1, and holds/needs 40 GB on node 2
    max_claim = {121: [12, 0, 0, 32768],
                 122: [10, 0, 0, 40960]}
    allocation = {121: [0, 0, 0, 0], 122: [0, 0, 0, 0]}

    # Step 1: job 121 takes its 32 GB on node 2. Still safe -- 121 can then get
    # its cores, finish, and release everything for 122.
    assert grant_is_safe(121, [0, 0, 0, 32768], total, max_claim, allocation)
    allocation[121] = [0, 0, 0, 32768]
    available = _sub(total, [0, 0, 0, 32768])
    assert available == [20, 65536, 20, 32768]

    # Step 2: job 122 asks for 10 cores on node 1. There is room for it, so a
    # naive allocator says yes -- and that is the step that creates the
    # deadlock the report demonstrates. The Banker refuses:
    #   after granting, node 1 has 10 cores left but 121 needs 12 -> stuck
    #   node 2 has 32 GB left but 122 needs 40 GB -> stuck
    assert _le([10, 0, 0, 0], available), "there IS free capacity, that is the trap"
    assert not grant_is_safe(122, [10, 0, 0, 0], available, max_claim, allocation), \
        "Banker must refuse the request that leads to circular wait"

    # Step 3: with 122 held back, 121 completes and then 122 runs. Safe order.
    order = safe_sequence(available, max_claim, allocation)
    assert order is not None and order[0] == 121, order

    # ---- a plain unsafe state, for contrast ----
    # One resource type, 1 free unit; two jobs each holding 1 and needing 2 more.
    assert not is_safe([1], {1: [3], 2: [3]}, {1: [1], 2: [1]})
    # Give it 2 free units and job 1 can finish, release, and unblock job 2.
    assert is_safe([2], {1: [3], 2: [3]}, {1: [1], 2: [1]})

    print("banker self-check OK "
          "(report section 5 request refused, safe order found, unsafe state rejected)")


if __name__ == "__main__":
    _self_check()
