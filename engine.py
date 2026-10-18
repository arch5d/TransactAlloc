"""TransactAlloc engine: worker pool + adaptive selector + WFG + sampler.

Bridges the original console scheduler/worker (mutex+semaphore) with the
new block-map database and real-time state.
"""

import threading
import time
from queue import PriorityQueue
from typing import Optional

import db
import policies
import scheduler as scheduler_mod
import worker as worker_mod
import wfg as wfg_mod
from adaptive import Context, choose_policy


class Engine:
    def __init__(self, num_workers: int = 4, policy_mode: str = "auto"):
        self.num_workers = num_workers
        self.policy_mode = policy_mode  # auto|fcfs|priority|round-robin
        self.queue = PriorityQueue(maxsize=200)
        self.mutex = threading.Lock()
        self.sem = threading.Semaphore(0)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seq = 0
        self._policy_lock = threading.Lock()
        self.current_policy = "fcfs"
        self.current_reason = "initial"
        self.wfg = wfg_mod.WaitForGraph()
        worker_mod.set_graph(self.wfg)
        worker_mod.set_release_hook(self._discard_job)
        worker_mod.set_stop_event(self._stop)
        # job_ids currently owned by the engine (queued, retrying, or running).
        # The reconciler uses this to avoid double-admitting a job.
        self._inflight: set[int] = set()
        self._sampler_stop = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._recon_thread: Optional[threading.Thread] = None
        self._resolve_lock = threading.Lock()

    def start(self):
        self._stop.clear()
        for i in range(self.num_workers):
            t = threading.Thread(
                target=self._worker,
                args=(i + 1,),
                name=f"ta-worker-{i+1}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        self._start_sampler()
        self._start_deadlock_monitor()
        self._start_reconciler()
        self._load_queued()

    def _start_reconciler(self):
        """Re-admit jobs that were written QUEUED but are not in the queue.

        A job that cannot be placed right now is parked as QUEUED in the
        database. When its blockers finish, this loop notices and re-submits it
        -- with values re-read from the database, so a round-robin job resumes
        with its *reduced* remaining time rather than its original duration.
        """
        def _run():
            while not self._stop.is_set():
                try:
                    with db.connect() as c:
                        rows = c.execute(
                            """
                            SELECT job_id, task_name, req_cpus, req_ram_mb, priority,
                                   duration_sec, remaining_sec,
                                   EXTRACT(EPOCH FROM submitted_at) AS submitted_epoch
                              FROM Jobs WHERE status='QUEUED' ORDER BY job_id
                            """
                        ).fetchall()
                    with self.mutex:
                        pending = set(self._inflight)
                    if self._stop.is_set():
                        break
                    for r in rows:
                        jid = int(r[0])
                        if self._stop.is_set():
                            break
                        if jid in pending:
                            continue
                        self.submit(dict(
                            job_id=jid, task_name=r[1], req_cpus=int(r[2]),
                            req_ram_mb=int(r[3]), priority=int(r[4]),
                            duration_sec=int(r[5] or 1),
                            remaining_sec=int(r[6] or r[5] or 1),
                            submitted_at=float(r[7] or time.time()),
                        ))
                except Exception:
                    pass
                time.sleep(1)

        self._recon_thread = threading.Thread(target=_run, name="ta-reconciler", daemon=True)
        self._recon_thread.start()

    def _discard_job(self, job_id: int):
        """Called when a worker leaves the queue; WFG cleanup is explicit."""
        jid = int(job_id)
        with self.mutex:
            self._inflight.discard(jid)

    def _load_queued(self):
        """Re-admit jobs left QUEUED in the database (e.g. across a restart).

        The in-memory PriorityQueue dies with the process; the Jobs table does
        not. Anything still QUEUED when the engine starts is put back in line.
        """
        with db.connect() as c:
            rows = c.execute(
                """
                SELECT job_id, task_name, req_cpus, req_ram_mb, priority,
                       duration_sec, remaining_sec,
                       EXTRACT(EPOCH FROM submitted_at) AS submitted_epoch
                  FROM Jobs WHERE status='QUEUED' ORDER BY job_id
                """
            ).fetchall()
        for r in rows:
            job = dict(
                job_id=int(r[0]), task_name=r[1], req_cpus=int(r[2]),
                req_ram_mb=int(r[3]), priority=int(r[4]),
                duration_sec=int(r[5] or 1),
                remaining_sec=int(r[6] or r[5] or 1),
                submitted_at=float(r[7] or time.time()),
            )
            self.submit(job)

    def stop(self):
        self._stop.set()
        self._sampler_stop.set()
        # Workers abort their current job within HEARTBEAT_SEC, so joining with
        # a short grace period is enough -- and guarantees no stale worker keeps
        # touching rows after the caller resets the database.
        for t in self._threads:
            t.join(timeout=6)
        if self._sampler_thread:
            self._sampler_thread.join(timeout=2)
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        if self._recon_thread:
            self._recon_thread.join(timeout=2)

    def running(self) -> bool:
        # Report intent, not liveness: once stop() is called the dashboard
        # should show "stopped" even while in-flight jobs drain.
        return not self._stop.is_set() and any(t.is_alive() for t in self._threads)

    def submit(self, job: dict, prio: int | None = None):
        jid = int(job["job_id"])
        with self.mutex:
            if self._stop.is_set():
                # The engine is stopping: never accept new work. This closes the
                # race where the reconciler re-admits a job that a worker has
                # just parked for the stop, only for another worker to start it
                # again and outlive stop()'s join.
                return
            if jid in self._inflight:
                return
            self._inflight.add(jid)
            self._seq += 1
            p = prio if prio is not None else policies.effective_priority(job, time.time())
            self.queue.put((p, self._seq, job))
        self.sem.release()

    def _set_policy(self, pol: str, reason: str):
        """Swap the active policy and audit the change if it actually moved."""
        with self._policy_lock:
            prev = self.current_policy
            self.current_policy = pol
            self.current_reason = reason
            if prev == pol:
                return
            try:
                with db.connect(autocommit=True) as c:
                    c.execute(
                        "INSERT INTO Policy_Changes (from_policy,to_policy,reason) VALUES (%s,%s,%s)",
                        (prev, pol, reason),
                    )
                    c.execute(
                        "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'POLICY','INFO',%s)",
                        (f"policy {prev} -> {pol}: {reason}",),
                    )
            except Exception:
                # an audit failure must never stop a worker
                pass

    def _decide_policy(self) -> str:
        """Pick this round's policy; audit it when it changes. Returns it."""
        if self.policy_mode == "auto":
            with db.connect() as c:
                queued = scheduler_mod.load_queued(c)
            ctx = Context(
                queue_depth=len(queued),
                running=worker_mod.active_count(),
                queued_jobs=queued,
                now=time.time(),
            )
            pol, reason = choose_policy(ctx)
            self._set_policy(pol, reason)
        else:
            pol = self.policy_mode
            self._set_policy(pol, f"manual override -> {pol}")
        return pol

    def _worker(self, wid: int):
        while not self._stop.is_set():
            if not self.sem.acquire(timeout=0.5):
                continue
            with self.mutex:
                if self.queue.empty():
                    continue
                _pval, _, job = self.queue.get()
            if self._stop.is_set():
                # Engine is stopping: leave the job QUEUED in the database for
                # the next start; do not begin new work.
                continue
            pol = self._decide_policy()
            worker_mod.execute_job(job, wid, pol)

    def _start_sampler(self):
        def _run():
            while not self._sampler_stop.is_set():
                try:
                    self._sample_once()
                except Exception:
                    pass
                time.sleep(5)
        self._sampler_thread = threading.Thread(target=_run, name="ta-sampler", daemon=True)
        self._sampler_thread.start()

    def _start_deadlock_monitor(self):
        def _run():
            while not self._stop.is_set():
                try:
                    cycle = self.wfg.find_cycle()
                    if cycle:
                        self._resolve_deadlock(cycle)
                except Exception:
                    pass
                time.sleep(1)
        self._monitor_thread = threading.Thread(target=_run, name="ta-deadlock", daemon=True)
        self._monitor_thread.start()

    def _resolve_deadlock(self, cycle):
        """Roll back the youngest job in a detected cycle. One transaction."""
        with self._resolve_lock:
            members = list(dict.fromkeys(cycle))
            with db.connect() as c:
                rows = c.execute(
                    "SELECT job_id, EXTRACT(EPOCH FROM submitted_at) FROM Jobs WHERE job_id = ANY(%s)",
                    (members,),
                ).fetchall()
            if not rows:
                for m in members:
                    self.wfg.clear_job(m)
                return
            submitted = {int(j): float(t) for j, t in rows}
            victim = wfg_mod.youngest(cycle, submitted)
            survivor = ", ".join(f"#{j}" for j in members if j != victim) or "none"
            with db.connect(autocommit=True) as c:
                start = time.perf_counter()
                try:
                    with c.transaction():
                        c.execute("SELECT ReleaseJobResources(%s)", (victim,))
                        c.execute("UPDATE Jobs SET status='QUEUED' WHERE job_id=%s", (victim,))
                        c.execute(
                            "INSERT INTO Execution_Logs (job_id,log_level,message) VALUES (%s,'ERROR','rolled back: deadlock victim')",
                            (victim,),
                        )
                    dur = int((time.perf_counter() - start) * 1000)
                    c.execute(
                        "INSERT INTO Txn_Log (job_id,op,outcome,reason,duration_ms) VALUES (%s,'DEADLOCK_ROLLBACK','COMMIT',%s,%s)",
                        (victim, f"cycle {' -> '.join('#'+str(j) for j in cycle)}", dur),
                    )
                    c.execute(
                        "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'DEADLOCK','ERROR',%s)",
                        (f"circular wait {' -> '.join('#'+str(j) for j in cycle)}; rolled back #{victim}, {survivor} unblocked",),
                    )
                except Exception as exc:
                    c.execute(
                        "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'DEADLOCK','WARN',%s)",
                        (f"rollback of #{victim} failed: {str(exc)[:120]}",),
                    )
            self.wfg.clear_job(victim)
            print(f"[DEADLOCK] cycle {' -> '.join('#'+str(j) for j in cycle)}; rolled back #{victim}")

    def _sample_once(self):
        now = time.time()
        with db.connect(autocommit=True) as c:
            # per cluster metrics
            for mid, host, tc, ac, tr, ar in c.execute(
                """
                SELECT m.machine_id, m.hostname, c.total_cores, c.allocated_cores,
                       r.total_ram_mb, r.allocated_ram_mb
                  FROM Machines m
                  JOIN CPU_Resources c ON c.machine_id=m.machine_id
                  JOIN Memory_Resources r ON r.machine_id=m.machine_id
                 ORDER BY m.machine_id
                """
            ).fetchall():
                fs = c.execute(
                    "SELECT COALESCE(SUM(size_mb),0), COUNT(*), COALESCE(MAX(size_mb),0) FROM Memory_Blocks WHERE machine_id=%s AND state='FREE'",
                    (mid,),
                ).fetchone()
                free_mb, holes, largest = fs[0], fs[1], fs[2]
                frag_idx = 0.0 if free_mb <= 0 else round(1.0 - largest / free_mb if largest >= 0 else 0.0, 4)
                c.execute(
                    """
                    INSERT INTO Metrics_Samples (ts,machine_id,used_cores,used_ram_mb,free_ram_mb,holes,largest_free_mb,frag_index)
                    VALUES (NOW(),%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (mid, ac, ar, free_mb, holes, largest, frag_idx),
                )
            # engine sample
            qd = c.execute("SELECT COUNT(*) FROM Jobs WHERE status='QUEUED'").fetchone()[0]
            runn = c.execute("SELECT COUNT(*) FROM Jobs WHERE status='RUNNING'").fetchone()[0]
            pol = self.current_policy
            c.execute(
                "INSERT INTO Engine_Sample (ts,queue_depth,running_jobs,policy) VALUES (NOW(),%s,%s,%s)",
                (qd, runn, pol),
            )
            # system event snapshot occasionally
            if int(now) % 10 == 0:
                c.execute(
                    "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'ENGINE','INFO',%s)",
                    (f"sample qd={qd} run={runn} pol={pol}",),
                )
