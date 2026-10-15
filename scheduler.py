"""Report section 4.2 -- Producer / Consumer over a bounded thread-safe buffer.

    producer   submit_job()       -> put into a bounded PriorityQueue, sem.release()
    consumer   worker_dispatch()  -> sem.acquire(), take from the queue, execute

Mutex plus counting semaphore, which is the pattern the OS syllabus asks for.
"""
import threading
import time
from queue import PriorityQueue

import worker


class JobScheduler:
    def __init__(self, max_queue_size=100, num_workers=4, policy="fcfs"):
        self.queue = PriorityQueue(maxsize=max_queue_size)
        self.mutex = threading.Lock()
        self.sem_available_jobs = threading.Semaphore(0)
        self.policy = policy
        self.num_workers = num_workers
        self._stop = threading.Event()
        self._threads = []
        # Tie-breaker for the priority tuple. The report uses time.time() here,
        # but two jobs submitted inside the same clock tick would then make
        # PriorityQueue fall through to comparing the job dicts, which raises
        # TypeError. A counter cannot collide.
        self._seq = 0

    def submit_job(self, priority_value, job):
        """PRODUCER. Blocks when the bounded buffer is full -- the point of maxsize."""
        with self.mutex:
            self._seq += 1
            self.queue.put((priority_value, self._seq, job))
            print(f"[PRODUCER] Job #{job['job_id']} queued (priority {priority_value})")
        self.sem_available_jobs.release()

    def worker_dispatch(self, worker_id):
        """CONSUMER. One of these runs per worker thread."""
        while not self._stop.is_set():
            # Timed acquire so a parked worker still notices _stop and exits.
            if not self.sem_available_jobs.acquire(timeout=0.5):
                continue

            with self.mutex:
                if self.queue.empty():
                    continue
                priority_value, _seq, job = self.queue.get()

            # The mutex is released BEFORE the database work starts.
            #
            # Report section 4.2 puts execute_allocation_transaction() inside
            # the `with self.mutex:` block. That would hold a process-wide lock
            # across the whole transaction, so no two workers could ever be in
            # the database at once -- the row-level locking in phase 2 and the
            # deadlock demo in phase 3 would both become unreachable, and the
            # thread pool would be decorative. The lock guards the queue and
            # the sequence counter, nothing more.
            print(f"[CONSUMER-{worker_id}] Dispatching Job #{job['job_id']} "
                  f"(priority {priority_value})")
            worker.execute_job(job, worker_id, self.policy)

    def start(self):
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self.worker_dispatch, args=(i + 1,),
                             name=f"worker-{i + 1}", daemon=True)
            for i in range(self.num_workers)
        ]
        for t in self._threads:
            t.start()
        print(f"[SCHEDULER] {self.num_workers} worker threads started (policy={self.policy})")

    def drain(self, timeout=300):
        """Block until the buffer is empty and no worker is mid-job."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.queue.empty() and worker.active_count() == 0:
                return True
            time.sleep(0.2)
        print("[SCHEDULER] drain timed out")
        return False

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)


def load_queued(conn):
    """Every QUEUED job, as plain dicts for the policy functions."""
    rows = conn.execute("""
        SELECT job_id, task_name, req_cpus, req_ram_mb, priority,
               duration_sec, remaining_sec, EXTRACT(EPOCH FROM submitted_at)
        FROM Jobs WHERE status = 'QUEUED'
        ORDER BY submitted_at, job_id
    """).fetchall()
    return [dict(job_id=r[0], task_name=r[1], req_cpus=r[2], req_ram_mb=r[3],
                 priority=r[4], duration_sec=r[5], remaining_sec=r[6],
                 submitted_at=float(r[7]))
            for r in rows]
