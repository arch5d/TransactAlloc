"""Report section 4.3 -- deadlock DETECTION (Wait-For Graph).

An edge  A -> B  reads "job A is waiting for a resource currently held by
job B". A cycle in that graph is a circular wait, which is the fourth Coffman
condition and therefore a deadlock.

Where the edges come from: an app-level graph, not pg_locks. These jobs are
not blocked on a database row -- they asked for capacity, were refused, and
are queued to retry. Postgres cannot see that wait; only the scheduler can.

Run this file directly to check the detector without a database:
    py -3 wfg.py
"""

import threading


class WaitForGraph:
    def __init__(self):
        self.edges = {}      # waiter -> set of holders it is blocked behind
        self._lock = threading.RLock()

    def add_wait(self, waiter, holder):
        if waiter != holder:
            with self._lock:
                self.edges.setdefault(waiter, set()).add(holder)

    def clear_job(self, job_id):
        """Drop a job from the graph -- it finished, or it was rolled back."""
        with self._lock:
            self.edges.pop(job_id, None)
            for holders in self.edges.values():
                holders.discard(job_id)

    def snapshot(self):
        with self._lock:
            return {w: set(h) for w, h in self.edges.items()}

    def edges_as_list(self):
        """JSON-friendly view for the dashboard."""
        with self._lock:
            return [[w, h] for w, hs in sorted(self.edges.items()) for h in sorted(hs)]

    def find_cycle(self):
        """Return one cycle as [a, b, ..., a], or None.

        Three-colour DFS: GREY means "on the current path", so meeting a GREY
        node is a back edge, i.e. a cycle. Nodes are visited in sorted order so
        the same input always reports the same cycle -- a demo that names a
        different victim each run is not a demo.

        ponytail: recursive DFS, so the depth is capped by the interpreter's
        recursion limit. Fine for a cluster of jobs; swap in an explicit stack
        if the graph ever gets thousands of nodes deep.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {}
        path = []

        with self._lock:
            graph = {w: set(h) for w, h in self.edges.items()}

        def visit(node):
            colour[node] = GREY
            path.append(node)
            for nxt in sorted(graph.get(node, ())):
                state = colour.get(nxt, WHITE)
                if state == GREY:
                    return path[path.index(nxt):] + [nxt]
                if state == WHITE:
                    found = visit(nxt)
                    if found:
                        return found
            path.pop()
            colour[node] = BLACK
            return None

        for node in sorted(graph):
            if colour.get(node, WHITE) == WHITE:
                found = visit(node)
                if found:
                    return found
        return None


def youngest(cycle, submitted_at):
    """Victim selection: the newest job in the cycle.

    Report section 5 rolls back the youngest transaction. It has done the least
    work, so aborting it wastes the least -- and because "youngest" is a total
    order on submission time, the choice cannot itself cycle, which is what
    stops the classic livelock where two jobs keep aborting each other.
    """
    members = list(dict.fromkeys(cycle))          # de-duplicate, keep order
    return max(members, key=lambda job_id: submitted_at[job_id])


def _self_check():
    # ---- report section 5: Job#121 <-> Job#122 circular wait ----
    g = WaitForGraph()
    g.add_wait(121, 122)      # 121 wants cores on node 1, held by 122
    g.add_wait(122, 121)      # 122 wants ram on node 2, held by 121

    cycle = g.find_cycle()
    assert cycle is not None, "the 121 <-> 122 circular wait must be detected"
    assert set(cycle) == {121, 122}, cycle
    assert cycle[0] == cycle[-1], f"a cycle must close on itself: {cycle}"

    submitted = {121: 100.0, 122: 101.0}          # 122 arrived later
    assert youngest(cycle, submitted) == 122

    # Rolling the victim back breaks the cycle.
    g.clear_job(122)
    assert g.find_cycle() is None, "removing the victim must clear the deadlock"

    # ---- a wait chain that is NOT a deadlock ----
    chain = WaitForGraph()
    chain.add_wait(1, 2)
    chain.add_wait(2, 3)
    assert chain.find_cycle() is None, "a chain with no back edge is not a deadlock"

    # ---- a longer cycle, to prove it is not hardcoded to pairs ----
    tri = WaitForGraph()
    tri.add_wait(1, 2)
    tri.add_wait(2, 3)
    tri.add_wait(3, 1)
    found = tri.find_cycle()
    assert found is not None and set(found) == {1, 2, 3}, found
    assert youngest(found, {1: 5.0, 2: 9.0, 3: 7.0}) == 2

    # ---- a job waiting on itself is not an edge ----
    self_wait = WaitForGraph()
    self_wait.add_wait(1, 1)
    assert self_wait.find_cycle() is None

    print("wfg self-check OK "
          "(pair cycle, 3-cycle, chain rejected, victim = youngest, cycle cleared)")


if __name__ == "__main__":
    _self_check()
