"""Bounded job lifecycle on a pool of warm worker processes. Not a durable queue: the job table (never an input)
can live in SQLite so results and idempotency keys outlive a restart, but unfinished jobs are not replayed.

The store keeps `concurrency` child processes alive and sends each job to an idle one over a pipe. A child keeps
the models it has loaded, so only its first job pays for imports and model loading. A child that is interrupted
(cancelled, past its deadline) or that crashes never serves again: it is terminated, then killed, and a fresh
child takes its place. A child is also retired after `max_jobs` jobs to bound memory growth.
"""

import copy
import math
import multiprocessing
import json
import os
import re
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from .tasks import MODEL_NAMES, execute, validate

TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
# How long a retired child gets to exit on its own, after terminate() or after being told to stop, before kill().
GRACE_SECONDS = 0.2
# How long a started child gets to say it is ready before it is given up on; and how long the store waits before
# starting another child once one could not be started or died before it was ready, so a fault that kills every
# child at start cannot become a spawn loop.
READY_SECONDS = 30.0
BACKOFF_SECONDS = 1.0
MAX_JOBS_PER_WORKER = 1000
IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,128}")
_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, progress TEXT,
    timeout_ms INTEGER NOT NULL, idempotency_key TEXT, submitted_at REAL NOT NULL, finished_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key ON jobs (idempotency_key);"""


class CapacityError(Exception):
    pass


@dataclass
class Settings:
    concurrency: int = 2
    capacity: int = 128
    retention_seconds: float = 300
    backend: str = "lexical"
    model_dir: str = "/models"
    test_tasks: bool = False
    # Jobs a child serves before it is replaced by a fresh one; BRIDGE_WORKER_MAX_JOBS in the service.
    max_jobs: int = MAX_JOBS_PER_WORKER
    # Where jobs.sqlite lives; BRIDGE_STATE_DIR in the service. None keeps the table in memory only.
    state_dir: str | None = None


def _serve(connection, backend, model_dir):
    """A child's whole life: run the jobs the parent sends until it hangs up."""
    # Ctrl+C in a terminal reaches the whole process group; stopping children is the parent's job.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # The service removes the token from its environment before it starts any child (server.py), so it is not
    # in the child's environment block; this second pop covers a store built by something that did not.
    os.environ.pop("BRIDGE_TOKEN", None)
    try:
        # Jobs come only once the child is here to read them: the parent never waits on a child still importing.
        connection.send((None, "ready", None))
    except (OSError, ValueError):
        return
    while True:
        try:
            message = connection.recv()
        except (EOFError, OSError):
            break
        if message is None:
            break
        job_id, task, payload = message
        del message
        try:
            # Until this arrives the job has not started, so the parent may give it to another child.
            connection.send((job_id, "started", None))
        except (OSError, ValueError):
            break

        def progress(completed, total):
            connection.send((job_id, "progress", {"completed": completed, "total": total}))

        try:
            reply = (job_id, "result", execute(task, payload, backend, model_dir, progress))
        except Exception:
            # Exceptions can contain credentials, document contents and local paths.
            reply = (job_id, "error", "task_failed")
        # No reference to a job outlives its reply, so the next job on this child cannot reach this one's input
        # or result; the memory they used is freed, not wiped.
        del payload
        try:
            connection.send(reply)
        except (OSError, ValueError):
            break
        except Exception:
            # A result that cannot be pickled is the task's failure, not the child's.
            try:
                connection.send((job_id, "error", "task_failed"))
            except Exception:
                break
        del reply
    connection.close()


class _Worker:
    """One child of the pool and what the coordinator knows about it."""

    def __init__(self, process, connection):
        self.process = process
        self.connection = connection
        self.started_at = time.monotonic()
        self.ready = False  # it has said so, and so reads a job the moment it is sent
        self.job_id = None  # the job sent to it, acknowledged or not
        self.served = 0  # jobs sent to it, towards max_jobs
        self.retired_at = None  # set once it must never serve again; it is then reaped outside the lock
        self.killed = False


class JobStore:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        if (type(self.settings.concurrency) is not int or type(self.settings.capacity) is not int
                or type(self.settings.max_jobs) is not int
                or not 1 <= self.settings.concurrency <= 8 or not 1 <= self.settings.capacity <= 1024
                or not 1 <= self.settings.max_jobs <= 1000000):
            raise ValueError("invalid worker limits")
        if (self.settings.backend not in MODEL_NAMES
                or not math.isfinite(self.settings.retention_seconds)
                or self.settings.retention_seconds <= 0):
            raise ValueError("invalid worker settings")
        self._context = multiprocessing.get_context("spawn")
        self._jobs = {}
        self._keys = {}  # idempotency key -> job id, for retained jobs only
        self._active = {}  # job id -> the worker it was sent to
        self._pool = []  # children that may take a job, idle or running one
        self._retiring = []  # children that never serve again, until they are gone
        self._backoff_until = 0.0  # no child is started before this time
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._db = None
        self._open_db()
        # A child that cannot start now is tried again once the backoff ends; a job that finds no child up
        # meanwhile fails as worker_start_failed, so the service still answers.
        self._replenish()
        self._thread = threading.Thread(target=self._coordinate, daemon=True)
        self._thread.start()

    def _open_db(self):
        """One connection for the store's life, used only under the lock. A file left by an earlier run keeps its
        retained results; what it still holds as unfinished never completed and is failed, not replayed."""
        if self.settings.state_dir is None:
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path = os.path.join(self.settings.state_dir, "jobs.sqlite")
            self._db = sqlite3.connect(path, check_same_thread=False)
        try:
            with self._lock:
                if self.settings.state_dir is not None:
                    self._db.execute("PRAGMA journal_mode=WAL")
                # An expired result is overwritten in the file, not left in a free page.
                self._db.execute("PRAGMA secure_delete=ON")
                self._db.executescript(_SCHEMA)
                with self._db:
                    self._db.execute(
                        "UPDATE jobs SET status = 'failed', result = 'null', error = ?, finished_at = ? "
                        "WHERE status NOT IN ('succeeded', 'failed', 'timed_out', 'cancelled')",
                        (json.dumps({"code": "service_restarted", "message": "Task did not complete"}), time.time()))
                    self._db.execute("DELETE FROM jobs WHERE finished_at IS NULL OR finished_at <= ?",
                                     (time.time() - self.settings.retention_seconds,))
                rows = self._db.execute("SELECT id, task, status, result, error, progress, timeout_ms, "
                                        "idempotency_key, finished_at FROM jobs").fetchall()
        except Exception:
            self._db.close()
            raise
        wall, now = time.time(), time.monotonic()
        for job_id, task, status, result, error, progress, timeout_ms, key, finished_at in rows:
            self._jobs[job_id] = {
                "id": job_id, "task": task, "status": status, "result": json.loads(result),
                "error": json.loads(error), "progress": json.loads(progress), "timeout_ms": timeout_ms,
                # Retention counts from the wall-clock finish, carried over to this process's monotonic clock.
                "_input": None, "_deadline": now, "_finished": now - max(0.0, wall - finished_at),
                "_retried": True, "_key": key,
            }
            if key is not None:
                self._keys[key] = job_id

    def _write(self, job, submitted=False):
        """Write a job's row through; a no-op once close() has let go of the connection. A submission that cannot
        be recorded is refused; a later write that fails leaves this process's copy answering, since the
        coordinator must not stop over a full disk."""
        if self._db is None:
            return
        finished = None if job["_finished"] is None else time.time()
        values = (job["status"], json.dumps(job["result"]), json.dumps(job["error"]), json.dumps(job["progress"]),
                  finished, job["id"])
        try:
            with self._db:
                if submitted:
                    # REPLACE drops a row an earlier failed delete left holding this key, which memory no longer has.
                    self._db.execute(
                        "INSERT OR REPLACE INTO jobs (task, timeout_ms, idempotency_key, submitted_at, status, result, "
                        "error, progress, finished_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (job["task"], job["timeout_ms"], job["_key"], time.time()) + values)
                else:
                    self._db.execute("UPDATE jobs SET status = ?, result = ?, error = ?, progress = ?, "
                                     "finished_at = ? WHERE id = ?", values)
        except sqlite3.Error:
            if submitted:
                raise CapacityError("job table cannot be written") from None

    def submit(self, task, payload, timeout_ms=30000, idempotency_key=None):
        if not isinstance(task, str):
            raise ValueError("task must be a string")
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 300000:
            raise ValueError("timeout_ms must be an integer between 100 and 300000")
        if idempotency_key is not None and (type(idempotency_key) is not str
                                            or not IDEMPOTENCY_KEY.fullmatch(idempotency_key)):
            raise ValueError("idempotency_key must be 1-128 printable ASCII characters")
        payload = validate(task, payload, self.settings.test_tasks)
        with self._lock:
            if self._stopping.is_set():
                raise CapacityError("worker is shutting down")
            self._expire()
            # A repeat adds no job, so it is answered even at capacity.
            if idempotency_key in self._keys:
                return self._public(self._jobs[self._keys[idempotency_key]])
            if len(self._jobs) >= self.settings.capacity:
                raise CapacityError("job capacity reached; wait for retained jobs to expire")
            now = time.monotonic()
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id, "task": task, "status": "queued", "result": None,
                "error": None, "progress": None, "timeout_ms": timeout_ms,
                "_input": copy.deepcopy(payload), "_deadline": now + timeout_ms / 1000,
                "_finished": None, "_retried": False, "_key": idempotency_key,
            }
            self._write(job, submitted=True)
            self._jobs[job_id] = job
            if idempotency_key is not None:
                self._keys[idempotency_key] = job_id
            return self._public(job)

    def get(self, job_id):
        with self._lock:
            self._expire()
            return self._public(self._jobs[job_id]) if job_id in self._jobs else None

    def cancel(self, job_id):
        with self._lock:
            self._expire()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] not in TERMINAL:
                self._abort(job, "cancelled")
            return self._public(job)

    def _public(self, job):
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def _expire(self):
        now = time.monotonic()
        expired = []
        for job_id in list(self._jobs):
            finished = self._jobs[job_id]["_finished"]
            if finished is not None and now - finished >= self.settings.retention_seconds:
                self._keys.pop(self._jobs.pop(job_id)["_key"], None)
                expired.append((job_id,))
        if expired and self._db is not None:
            try:
                with self._db:
                    self._db.executemany("DELETE FROM jobs WHERE id = ?", expired)
            except sqlite3.Error:
                pass  # the next open deletes them by their finish time

    def _finish(self, job, status, result=None, code=None):
        job.update(status=status, result=result, error={"code": code, "message": "Task did not complete"} if code else None)
        job["_input"] = None
        job["_finished"] = time.monotonic()
        self._write(job)

    def _abort(self, job, status, code=None):
        """End a job before its child has: the child is retired, since it may be halfway through the input."""
        worker = self._active.pop(job["id"], None)
        if worker is not None:
            worker.job_id = None
            self._retire(worker)
        self._finish(job, status, code=code)

    def _start_worker(self):
        """A fresh child, or None when the system refuses one; nothing allocated is leaked either way. Runs
        without the lock, since creating a process takes milliseconds on Linux and tens of them on Windows."""
        parent = child = process = None
        try:
            parent, child = self._context.Pipe()
            # numpy's OpenBLAS starts a thread per visible CPU, not per CPU of quota, and all count towards pids_limit.
            os.environ.update(RAYON_NUM_THREADS="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
            process = self._context.Process(
                target=_serve, args=(child, self.settings.backend, self.settings.model_dir), daemon=True)
            process.start()
        except Exception:
            for resource in (child, parent, process):
                if resource is not None:
                    try:
                        resource.close()
                    except (OSError, ValueError):
                        pass
            return None
        # The parent keeps no copy of the child's end, so a dead child reads as end-of-file, not a hang.
        child.close()
        return _Worker(process, parent)

    def _start_failed(self):
        """A child could not be started, or died before it was ready: wait before trying again, and when no child
        is up to take them, tell the queued jobs now rather than at their deadline."""
        self._backoff_until = time.monotonic() + BACKOFF_SECONDS
        if not self._pool:
            for job in self._jobs.values():
                if job["status"] == "queued" and job["id"] not in self._active:
                    self._finish(job, "failed", code="worker_start_failed")

    def _replenish(self):
        """Fill the pool up to the concurrency limit, deciding and adding under the lock and starting without it.
        Only the coordinator calls it once the store is built, so no two starts race for the same place."""
        while True:
            with self._lock:
                if (self._stopping.is_set() or len(self._pool) >= self.settings.concurrency
                        or time.monotonic() < self._backoff_until):
                    return
            worker = self._start_worker()
            with self._lock:
                if worker is None:
                    self._start_failed()
                    return
                if not self._stopping.is_set():
                    self._pool.append(worker)
                    continue
            # close() has drained the pool meanwhile, so this child is stopped here.
            self._discard(worker)
            return

    @staticmethod
    def _discard(worker):
        """Stop a child that never joined the pool."""
        try:
            worker.connection.close()
        except (OSError, ValueError):
            pass
        worker.process.terminate()
        worker.process.join(GRACE_SECONDS)
        if worker.process.is_alive():
            worker.process.kill()
            worker.process.join(GRACE_SECONDS)
        try:
            worker.process.close()
        except ValueError:
            pass

    def _retire(self, worker, stop=False, died=False):
        """Take a child out of service for good. With stop, it is asked to exit by itself, because it is idle and
        has done its jobs; otherwise it is terminated, since it may be running one. Either way the reaper kills it
        if it is still there after the grace period, outside the lock. A child that died (or went silent) before
        it was ready counts as a failed start."""
        if worker in self._pool:
            self._pool.remove(worker)
        if worker.retired_at is not None:
            return
        job = self._jobs.get(worker.job_id) if worker.job_id is not None else None
        self._active.pop(worker.job_id, None)
        worker.job_id = None
        if stop:
            try:
                worker.connection.send(None)
            except (OSError, ValueError):
                pass
        try:
            worker.connection.close()
        except (OSError, ValueError):
            pass
        if not stop and worker.process.is_alive():
            worker.process.terminate()
        worker.retired_at = time.monotonic()
        self._retiring.append(worker)
        if not worker.ready:
            if died:
                self._start_failed()
            else:
                self._backoff_until = worker.retired_at + BACKOFF_SECONDS
        if job is not None and job["status"] not in TERMINAL:
            if job["status"] == "queued" and not job["_retried"]:
                job["_retried"] = True  # the child never had it, so it can run elsewhere, once
            else:
                self._finish(job, "failed", code="worker_crashed")

    def _assign(self, job):
        """Send a queued job to an idle child that is ready for one; otherwise it waits for one, which the
        coordinator starts outside the lock when the pool has room."""
        worker = next((candidate for candidate in self._pool if candidate.ready and candidate.job_id is None), None)
        if worker is None:
            return
        worker.job_id = job["id"]
        worker.served += 1
        self._active[job["id"]] = worker
        try:
            worker.connection.send((job["id"], job["task"], job["_input"]))
        except (OSError, ValueError):
            # Its pipe is gone, so the child is: retired like one that died, and replaced.
            self._retire(worker)

    def _coordinate(self):
        while not self._stopping.wait(0.01):
            with self._lock:
                self._expire()
                now = time.monotonic()
                for worker in list(self._pool):
                    self._drain(worker)
                    if worker.retired_at is None and not worker.process.is_alive():
                        # A result can arrive after the first poll, just before exit.
                        self._drain(worker)
                        self._retire(worker, died=True)
                    elif not worker.ready and now - worker.started_at >= READY_SECONDS:
                        self._retire(worker)
                for job in list(self._jobs.values()):
                    if job["status"] in TERMINAL:
                        continue
                    if now >= job["_deadline"]:
                        self._abort(job, "timed_out", code="deadline_exceeded")
                    elif job["status"] == "queued" and job["id"] not in self._active:
                        self._assign(job)
                retiring = list(self._retiring)
            # Nothing that can wait on a process happens under the lock: a GET never waits for a child to start
            # or to die.
            self._replenish()
            if self._stopping.is_set():
                return  # close() reaps from here on; reaping too would touch processes it has closed
            self._reap(retiring)

    def _drain(self, worker):
        """Deliver what the child has sent about the job it runs, up to that job's end."""
        try:
            while worker.connection.poll():
                if self._drain_message(worker, worker.connection.recv()):
                    break
        except Exception:
            # The child is gone (end of file), or sent something that is not its protocol: either way it is done.
            self._retire(worker, died=True)

    def _drain_message(self, worker, message):
        """Apply one message from a child; True once it ended the job the child was running."""
        job_id, kind, value = message
        if kind == "ready":
            worker.ready = True
            return False
        job = self._jobs.get(job_id)
        # A message from a job the child no longer runs for us has nowhere to go.
        if job_id != worker.job_id or job is None or job["status"] in TERMINAL:
            return False
        if kind == "started":
            # Not written through: a restart fails a running job exactly as it fails a queued one.
            job["status"] = "running"
            return False
        if kind == "progress":
            job["progress"] = value
            return False
        self._active.pop(job_id, None)
        worker.job_id = None
        if kind == "result":
            self._finish(job, "succeeded", result=value)
        else:
            self._finish(job, "failed", code="task_failed")
        if worker.served >= self.settings.max_jobs:
            self._retire(worker, stop=True)
        return True

    def _reap(self, retiring):
        """Outside the lock: let go of retired children that have exited, and kill those that have not in time."""
        gone = []
        now = time.monotonic()
        for worker in retiring:
            if worker.process.is_alive():
                if not worker.killed and now - worker.retired_at >= GRACE_SECONDS:
                    worker.process.kill()
                    worker.killed = True
                continue
            worker.process.join(0)
            worker.process.close()
            gone.append(worker)
        if gone:
            with self._lock:
                for worker in gone:
                    if worker in self._retiring:  # close() may have cleared the list meanwhile
                        self._retiring.remove(worker)

    def close(self):
        self._stopping.set()
        self._thread.join(timeout=2)
        with self._lock:
            # The file keeps unfinished jobs as they were, so the next start reports them as service_restarted;
            # this process answers for them as cancelled until it exits.
            if self._db is not None:
                self._db.close()
                self._db = None
            for job in self._jobs.values():
                if job["status"] not in TERMINAL:
                    self._abort(job, "cancelled")
            for worker in list(self._pool):
                self._retire(worker, stop=True)
            retiring = list(self._retiring)
        # Drain the pool: no child outlives the store.
        for worker in retiring:
            worker.process.join(GRACE_SECONDS)
            if worker.process.is_alive():
                worker.process.kill()
                worker.process.join(GRACE_SECONDS)
            try:
                worker.process.close()
            except ValueError:
                pass  # still exiting; a daemon, so the interpreter takes it down with itself
        # A coordinator still inside a start only has that child left to stop.
        self._thread.join()
        with self._lock:
            self._retiring.clear()
