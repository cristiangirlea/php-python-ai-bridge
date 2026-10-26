"""Bounded job lifecycle on a pool of warm worker processes. Not a durable queue: the job table (never an input)
can live in SQLite so results and idempotency keys outlive a restart, but unfinished jobs are not replayed.

The store keeps `concurrency` child processes alive and sends each job to an idle one over a pipe. A child loads its
backend's models as it starts and keeps them, so only a job that arrives during that load waits for it. A child that
is interrupted (cancelled, past its deadline) or that crashes never serves again: it is terminated, then killed, and a
fresh child takes its place. A child is also retired after `max_jobs` jobs to bound memory growth.
"""

import copy
import json
import math
import multiprocessing
import os
import re
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

from . import log, metrics, tasks
from .tasks import MODEL_NAMES, TEST_TASKS, execute, validate

STATUSES = ("succeeded", "failed", "timed_out", "cancelled")
TERMINAL = frozenset(STATUSES)
# How long a retired child gets to exit on its own, after terminate() or after being told to stop, before kill().
GRACE_SECONDS = 0.2
# How long a started child gets to say it is ready before it is given up on; and how long the store waits before
# starting another child once one could not be started or died before it was ready, so a fault that kills every
# child at start cannot become a spawn loop.
READY_SECONDS = 30.0
BACKOFF_SECONDS = 1.0
MAX_JOBS_PER_WORKER = 1000
IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,128}")
# Each backend's warm-up, as the (directory, max_length, stride) keys tasks._onnx caches by; a test keeps both in step.
WARM_UP = {"onnx": (("rerank", 512, 0), ("embed", 256, 0), ("redact", 512, 64), ("redact-uncased", 512, 64))}
# The owner is a token's name, never its value; _open_db gives a table from before owners the column, rows to "default".
_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, progress TEXT,
    timeout_ms INTEGER NOT NULL, idempotency_key TEXT, submitted_at REAL NOT NULL, finished_at REAL, owner TEXT);"""
_INDEX = """DROP INDEX IF EXISTS jobs_idempotency_key;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_owner_idempotency_key ON jobs (owner, idempotency_key);"""
DEFAULT_OWNER = "default"
# A job's public fields and the store's own, which start with an underscore.
Job = dict[str, Any]


class CapacityError(Exception):
    def __init__(self, message: str, retry_after: int = 1) -> None:
        super().__init__(message)
        self.retry_after = retry_after  # whole seconds until a submission may be accepted, at least 1


class DrainingError(Exception):
    """The store is draining before the service stops and takes no new job."""


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
    # The names of the service's tokens: a job belongs to one, and each has its own metrics series.
    owners: tuple[str, ...] = (DEFAULT_OWNER,)
    # The most queued, running and retained jobs one owner may hold; BRIDGE_OWNER_CAPACITY. None is the capacity.
    owner_capacity: int | None = None


def _serve(connection: Connection, backend: str, model_dir: str) -> None:
    """A child's whole life: run the jobs the parent sends until it hangs up."""
    # Ctrl+C in a terminal reaches the whole process group; stopping children is the parent's job.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # The service removes its tokens from its environment before it starts any child (server.py), so they are not
    # in the child's environment block; this second pop covers a store built by something that did not.
    os.environ.pop("BRIDGE_TOKEN", None)
    os.environ.pop("BRIDGE_TOKENS", None)
    # Ready only after the warm-up, so no job waits in a pipe nobody reads; a failed load still reports ready.
    try:
        for name, max_length, stride in WARM_UP.get(backend, ()):
            tasks._onnx(Path(model_dir) / name, max_length, stride)
    except Exception:
        loaded = False
    else:
        loaded = True
    try:
        connection.send((None, "ready", None))
        if loaded:
            connection.send((None, "loaded", None))
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

        def progress(completed: int, total: int) -> None:
            connection.send((job_id, "progress", {"completed": completed, "total": total}))  # noqa: B023  (called only within this iteration)

        try:
            reply: tuple[Any, str, object] = (job_id, "result", execute(task, payload, backend, model_dir, progress))
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

    def __init__(self, process: BaseProcess, connection: Connection) -> None:
        self.process = process
        self.connection = connection
        self.started_at = time.monotonic()
        self.ready = False  # it has said so, and so reads a job the moment it is sent
        self.loaded = False  # it has loaded every model its backend needs
        self.job_id: str | None = None  # the job sent to it, acknowledged or not
        self.sending = False  # a send outside the lock owns the pipe until it returns
        self.served = 0  # jobs sent to it, towards max_jobs
        self.retired_at: float | None = None  # set once it must never serve again; it is then reaped outside the lock
        self.killed = False


class JobStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        if (type(self.settings.concurrency) is not int or type(self.settings.capacity) is not int
                or type(self.settings.max_jobs) is not int
                or not 1 <= self.settings.concurrency <= 8 or not 1 <= self.settings.capacity <= 1024
                or not 1 <= self.settings.max_jobs <= 1000000):
            raise ValueError("invalid worker limits")
        cap = self.settings.owner_capacity
        if cap is not None and (type(cap) is not int or not 1 <= cap <= self.settings.capacity):
            raise ValueError("invalid worker limits")
        if (self.settings.backend not in MODEL_NAMES
                or not math.isfinite(self.settings.retention_seconds)
                or self.settings.retention_seconds <= 0):
            raise ValueError("invalid worker settings")
        owners = self.settings.owners
        if (type(owners) is not tuple or not owners or len(set(owners)) != len(owners)
                or any(type(owner) is not str for owner in owners)):
            raise ValueError("invalid worker owners")
        self._context = multiprocessing.get_context("spawn")
        # Aggregates for GET /metrics, since this process started; every series exists from the start, at zero.
        tasks = tuple(MODEL_NAMES[self.settings.backend]) + (TEST_TASKS if self.settings.test_tasks else ())
        self._tasks = tasks
        self._ended = {(owner, task, status): 0 for owner in owners for task in tasks for status in STATUSES}
        self._submitted = {(owner, task): 0 for owner in owners for task in tasks}
        self._rejected = {(owner, "capacity"): 0 for owner in owners}
        self._durations = {task: metrics.new_histogram() for task in tasks}
        self._waits = {task: metrics.new_histogram() for task in tasks}
        self._jobs: dict[str, Job] = {}
        self._keys: dict[tuple[str, str | None], str] = {}  # (owner, idempotency key) -> job id, for retained jobs only
        self._active: dict[str, _Worker] = {}  # job id -> the worker it was sent to
        self._pool: list[_Worker] = []  # children that may take a job, idle or running one
        self._retiring: list[_Worker] = []  # children that never serve again, until they are gone
        self._backoff_until = 0.0  # no child is started before this time
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._draining = False
        self._db: sqlite3.Connection | None = None
        self._open_db()
        # A child that cannot start now is tried again once the backoff ends; a job that finds no child up
        # meanwhile fails as worker_start_failed, so the service still answers.
        self._replenish()
        self._thread = threading.Thread(target=self._coordinate, daemon=True)
        self._thread.start()

    def _open_db(self) -> None:
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
                if "owner" not in [column[1] for column in self._db.execute("PRAGMA table_info(jobs)")]:
                    self._db.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
                with self._db:
                    self._db.execute("UPDATE jobs SET owner = ? WHERE owner IS NULL", (DEFAULT_OWNER,))
                self._db.executescript(_INDEX)
                with self._db:
                    self._db.execute(
                        "UPDATE jobs SET status = 'failed', result = 'null', error = ?, finished_at = ? "
                        "WHERE status NOT IN ('succeeded', 'failed', 'timed_out', 'cancelled')",
                        (json.dumps({"code": "service_restarted", "message": "Task did not complete"}), time.time()))
                    self._db.execute("DELETE FROM jobs WHERE finished_at IS NULL OR finished_at <= ?",
                                     (time.time() - self.settings.retention_seconds,))
                rows = self._db.execute("SELECT id, task, status, result, error, progress, timeout_ms, "
                                        "idempotency_key, finished_at, owner FROM jobs").fetchall()
        except Exception:
            self._db.close()
            raise
        wall, now = time.time(), time.monotonic()
        for job_id, task, status, result, error, progress, timeout_ms, key, finished_at, owner in rows:
            self._jobs[job_id] = {
                "id": job_id, "task": task, "status": status, "result": json.loads(result),
                "error": json.loads(error), "progress": json.loads(progress), "timeout_ms": timeout_ms,
                # Retention counts from the wall-clock finish, carried over to this process's monotonic clock.
                "_input": None, "_deadline": now, "_finished": now - max(0.0, wall - finished_at),
                "_retried": True, "_key": key, "_owner": owner,
            }
            if key is not None:
                self._keys[owner, key] = job_id

    def _write(self, job: Job, submitted: bool = False) -> None:
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
                        "INSERT OR REPLACE INTO jobs (owner, task, timeout_ms, idempotency_key, submitted_at, status, "
                        "result, error, progress, finished_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (job["_owner"], job["task"], job["timeout_ms"], job["_key"], time.time()) + values)
                else:
                    self._db.execute("UPDATE jobs SET status = ?, result = ?, error = ?, progress = ?, "
                                     "finished_at = ? WHERE id = ?", values)
        except sqlite3.Error:
            if submitted:
                raise CapacityError("job table cannot be written") from None

    def submit(self, task: str, payload: Any, timeout_ms: int = 30000, idempotency_key: str | None = None,
               owner: str = DEFAULT_OWNER) -> Job:
        if owner not in self.settings.owners:
            raise ValueError("owner is not one of the service's token names")
        if not isinstance(task, str):
            raise ValueError("task must be a string")
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 300000:
            raise ValueError("timeout_ms must be an integer between 100 and 300000")
        if idempotency_key is not None and (type(idempotency_key) is not str
                                            or not IDEMPOTENCY_KEY.fullmatch(idempotency_key)):
            raise ValueError("idempotency_key must be 1-128 printable ASCII characters")
        payload = validate(task, payload, self.settings.test_tasks)
        with self._lock:
            try:
                return self._accept(task, payload, timeout_ms, idempotency_key, owner)
            except CapacityError:
                # Every 429 the service sends comes from here, so each is counted exactly once.
                self._rejected[owner, "capacity"] += 1
                raise

    def _accept(self, task: str, payload: Any, timeout_ms: int, idempotency_key: str | None, owner: str) -> Job:
        if self._draining:
            raise DrainingError("worker is draining")
        if self._stopping.is_set():
            raise CapacityError("worker is shutting down")
        self._expire()
        # A repeat adds no job, so it is answered even at capacity.
        if (owner, idempotency_key) in self._keys:
            return self._public(self._jobs[self._keys[owner, idempotency_key]])
        # Checked before the total: one of this owner's jobs expiring frees a slot under both limits.
        cap = self.settings.owner_capacity
        if cap is not None and sum(job["_owner"] == owner for job in self._jobs.values()) >= cap:
            raise CapacityError("owner job capacity reached; wait for its retained jobs to expire",
                                self._retry_after(owner))
        if len(self._jobs) >= self.settings.capacity:
            raise CapacityError("job capacity reached; wait for retained jobs to expire", self._retry_after())
        now = time.monotonic()
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id, "task": task, "status": "queued", "result": None,
            "error": None, "progress": None, "timeout_ms": timeout_ms,
            "_input": copy.deepcopy(payload), "_deadline": now + timeout_ms / 1000,
            "_finished": None, "_retried": False, "_key": idempotency_key, "_submitted_at": now, "_owner": owner,
        }
        self._write(job, submitted=True)
        self._jobs[job_id] = job
        if idempotency_key is not None:
            self._keys[owner, idempotency_key] = job_id
        self._submitted[owner, task] += 1
        return self._public(job)

    def _owned(self, job_id: str, owner: str) -> Job | None:
        """The job if this owner submitted it; another owner's job is as unknown to it as one that never was."""
        job = self._jobs.get(job_id)
        return job if job is not None and job["_owner"] == owner else None

    def get(self, job_id: str, owner: str = DEFAULT_OWNER) -> Job | None:
        with self._lock:
            self._expire()
            job = self._owned(job_id, owner)
            return None if job is None else self._public(job)

    def cancel(self, job_id: str, owner: str = DEFAULT_OWNER) -> Job | None:
        with self._lock:
            self._expire()
            job = self._owned(job_id, owner)
            if job is None:
                return None
            if job["status"] not in TERMINAL:
                self._abort(job, "cancelled")
            return self._public(job)

    @property
    def draining(self) -> bool:
        return self._draining

    def drain(self) -> None:
        """Take no new job and fail every job no child has started, dropping its input; running jobs go on."""
        with self._lock:
            self._draining = True
            # A start the parent has not read yet counts as a start.
            for worker in list(self._pool):
                self._drain(worker)
            for job in list(self._jobs.values()):
                if job["status"] == "queued":
                    self._abort(job, "failed", code="service_restarted")

    def busy(self) -> bool:
        """Whether any job has yet to end."""
        with self._lock:
            return any(job["status"] not in TERMINAL for job in self._jobs.values())

    def readiness(self) -> str | None:
        """None when a child is idle with its models loaded; otherwise why not, from a fixed set."""
        with self._lock:
            if self._draining:
                return "draining"
            idle = [worker for worker in self._pool if worker.ready and worker.job_id is None]
            if any(worker.loaded for worker in idle):
                return None
            return "models_loading" if idle else "no_idle_child"

    def snapshot(self) -> dict[str, Any]:
        """The aggregates for GET /metrics as plain values, copied under the lock so the caller formats them without
        holding it."""
        with self._lock:
            statuses = [job["status"] for job in self._jobs.values()]
            return {
                "backend": self.settings.backend, "concurrency": self.settings.concurrency,
                "capacity": self.settings.capacity, "tasks": self._tasks, "owners": self.settings.owners,
                "jobs": dict(self._ended), "submitted": dict(self._submitted), "rejected": dict(self._rejected),
                "queued": statuses.count("queued"), "running": statuses.count("running"),
                "pool_size": len(self._pool), "pool_ready": sum(worker.ready for worker in self._pool),
                "duration": copy.deepcopy(self._durations), "queue": copy.deepcopy(self._waits),
            }

    def _retry_after(self, owner: str | None = None) -> int:
        """Seconds until the first retained job (of this owner, when given) expires; holding only unfinished jobs gives
        a short default, since none of them has a known end."""
        finished: list[float] = [job["_finished"] for job in self._jobs.values()
                    if job["_finished"] is not None and owner in (None, job["_owner"])]
        if not finished:
            return 1
        return max(1, math.ceil(min(finished) + self.settings.retention_seconds - time.monotonic()))

    def _public(self, job: Job) -> Job:
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def _expire(self) -> None:
        now = time.monotonic()
        expired = []
        for job_id in list(self._jobs):
            finished = self._jobs[job_id]["_finished"]
            if finished is not None and now - finished >= self.settings.retention_seconds:
                job = self._jobs.pop(job_id)
                self._keys.pop((job["_owner"], job["_key"]), None)
                expired.append((job_id,))
        if expired and self._db is not None:
            try:
                with self._db:
                    self._db.executemany("DELETE FROM jobs WHERE id = ?", expired)
            except sqlite3.Error:
                pass  # the next open deletes them by their finish time

    def _finish(self, job: Job, status: str, result: Any = None, code: str | None = None) -> None:
        job.update(status=status, result=result, error={"code": code, "message": "Task did not complete"} if code else None)
        job["_input"] = None
        job["_finished"] = time.monotonic()
        self._write(job)
        # A job read back from the table finished in an earlier process and never ends here.
        self._ended[job["_owner"], job["task"], status] += 1
        metrics.observe(self._durations[job["task"]], job["_finished"] - job["_submitted_at"])
        if not log.enabled():
            return
        started = job.get("_started_at")
        log.event("job", warning=status in ("failed", "timed_out"), job_id=job["id"], task=job["task"],
                  owner=job["_owner"], job_status=status,
                  queue_ms=None if started is None else round((started - job["_submitted_at"]) * 1000, 3),
                  run_ms=None if started is None else round((job["_finished"] - started) * 1000, 3))

    def _abort(self, job: Job, status: str, code: str | None = None) -> None:
        """End a job before its child has: the child is retired, since it may be halfway through the input."""
        worker = self._active.pop(job["id"], None)
        if worker is not None:
            worker.job_id = None
            self._retire(worker)
        self._finish(job, status, code=code)

    def _start_worker(self) -> _Worker | None:
        """A fresh child, or None when the system refuses one; nothing allocated is leaked either way. Runs
        without the lock, since creating a process takes milliseconds on Linux and tens of them on Windows."""
        parent = child = process = None
        try:
            parent, child = self._context.Pipe()
            # numpy's OpenBLAS starts a thread per visible CPU, not per CPU of quota, and all count towards pids_limit.
            os.environ.update(RAYON_NUM_THREADS="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
            # ONNX Runtime 1.31 posts telemetry to Microsoft's collector from import on unless this is set.
            os.environ["ORT_DISABLE_TELEMETRY"] = "1"
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

    def _start_failed(self) -> None:
        """A child could not be started, or died before it was ready: wait before trying again, and when no child
        is up to take them, tell the queued jobs now rather than at their deadline."""
        self._backoff_until = time.monotonic() + BACKOFF_SECONDS
        # While draining, queued jobs end as service_restarted instead.
        if not self._pool and not self._draining:
            for job in self._jobs.values():
                if job["status"] == "queued" and job["id"] not in self._active:
                    self._finish(job, "failed", code="worker_start_failed")

    def _replenish(self) -> None:
        """Fill the pool up to the concurrency limit, deciding and adding under the lock and starting without it.
        Only the coordinator calls it once the store is built, so no two starts race for the same place."""
        while True:
            with self._lock:
                if (self._stopping.is_set() or self._draining or len(self._pool) >= self.settings.concurrency
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
    def _discard(worker: _Worker) -> None:
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

    def _retire(self, worker: _Worker, stop: bool = False, died: bool = False) -> None:
        """Take a child out of service for good. With stop, it is asked to exit by itself, because it is idle and
        has done its jobs; otherwise it is terminated, since it may be running one. Either way the reaper kills it
        if it is still there after the grace period, outside the lock. A child that died (or went silent) before
        it was ready counts as a failed start."""
        if worker in self._pool:
            self._pool.remove(worker)
        if worker.retired_at is not None:
            return
        job = self._jobs.get(worker.job_id) if worker.job_id is not None else None
        if worker.job_id is not None:
            self._active.pop(worker.job_id, None)
        worker.job_id = None
        # A send in flight owns the pipe: the child is terminated instead, and the sender closes the pipe.
        stop = stop and not worker.sending
        if stop:
            try:
                worker.connection.send(None)
            except (OSError, ValueError):
                pass
        if not worker.sending:
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

    def _assign(self, job: Job) -> None:
        """Send a queued job to an idle child that is ready for one; otherwise it waits for one, which the
        coordinator starts outside the lock when the pool has room."""
        attached = self._attach(job)
        if attached is not None:
            self._send(*attached)

    def _attach(self, job: Job) -> tuple[_Worker, tuple[str, str, Any]] | None:
        """Under the lock: give a queued job to an idle ready child; returns (worker, message) to send, or None."""
        worker = next((candidate for candidate in self._pool if candidate.ready and candidate.job_id is None), None)
        if worker is None:
            return None
        worker.job_id = job["id"]
        worker.served += 1
        worker.sending = True
        self._active[job["id"]] = worker
        return worker, (job["id"], job["task"], job["_input"])

    def _send(self, worker: _Worker, message: tuple[str, str, Any]) -> None:
        """Send what _attach returned; the coordinator calls it without the lock, so a full pipe never holds a GET."""
        try:
            worker.connection.send(message)
            failed = False
        except (OSError, ValueError):
            failed = True
        with self._lock:
            worker.sending = False
            if worker.retired_at is not None:
                try:
                    worker.connection.close()
                except (OSError, ValueError):
                    pass
            elif failed:
                # Its pipe is gone, so the child is: retired like one that died, and replaced.
                self._retire(worker)

    def _coordinate(self) -> None:
        while not self._stopping.wait(0.01):
            sends: list[tuple[_Worker, tuple[str, str, Any]]] = []
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
                    elif job["status"] == "queued" and self._draining:
                        # Back in the queue after its child died before starting it, so no child will.
                        self._abort(job, "failed", code="service_restarted")
                    elif job["status"] == "queued" and job["id"] not in self._active:
                        attached = self._attach(job)
                        if attached is not None:
                            sends.append(attached)
                retiring = list(self._retiring)
            # Nothing that can wait on a process happens under the lock: a GET never waits for a child to start
            # or to die.
            for attached in sends:
                self._send(*attached)
            self._replenish()
            if self._stopping.is_set():
                return  # close() reaps from here on; reaping too would touch processes it has closed
            self._reap(retiring)

    def _drain(self, worker: _Worker) -> None:
        """Deliver what the child has sent about the job it runs, up to that job's end."""
        try:
            while worker.connection.poll():
                if self._drain_message(worker, worker.connection.recv()):
                    break
        except Exception:
            # The child is gone (end of file), or sent something that is not its protocol: either way it is done.
            self._retire(worker, died=True)

    def _drain_message(self, worker: _Worker, message: tuple[Any, str, Any]) -> bool:
        """Apply one message from a child; True once it ended the job the child was running."""
        job_id, kind, value = message
        if kind == "ready":
            worker.ready = True
            return False
        if kind == "loaded":
            worker.loaded = True
            return False
        job = self._jobs.get(job_id)
        # A message from a job the child no longer runs for us has nowhere to go.
        if job_id != worker.job_id or job is None or job["status"] in TERMINAL:
            return False
        if kind == "started":
            # Not written through: a restart fails a running job exactly as it fails a queued one.
            job["status"] = "running"
            job["_started_at"] = time.monotonic()
            metrics.observe(self._waits[job["task"]], job["_started_at"] - job["_submitted_at"])
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

    def _reap(self, retiring: list[_Worker]) -> None:
        """Outside the lock: let go of retired children that have exited, and kill those that have not in time."""
        gone: list[_Worker] = []
        now = time.monotonic()
        for worker in retiring:
            assert worker.retired_at is not None  # set by _retire before the worker joins the list
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

    def close(self) -> int:
        """Stop every child; returns how many unfinished jobs this ended as cancelled."""
        self._stopping.set()
        self._thread.join(timeout=2)
        with self._lock:
            # The file keeps unfinished jobs as they were, so the next start reports them as service_restarted;
            # this process answers for them as cancelled until it exits.
            if self._db is not None:
                self._db.close()
                self._db = None
            unfinished = [job for job in self._jobs.values() if job["status"] not in TERMINAL]
            for job in unfinished:
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
        return len(unfinished)
