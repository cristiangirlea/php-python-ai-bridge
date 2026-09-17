"""In-memory, bounded job lifecycle. Not a durable queue."""

import copy
import math
import multiprocessing
import threading
import time
import uuid
from dataclasses import dataclass

from .tasks import execute, validate

TERMINAL = frozenset({"succeeded", "failed", "timed_out", "cancelled"})


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


def _run_child(connection, task, payload, backend, model_dir):
    def progress(completed, total):
        connection.send(("progress", {"completed": completed, "total": total}))

    try:
        result = execute(task, payload, backend, model_dir, progress)
        connection.send(("result", result))
    except Exception:
        # Exceptions can contain credentials, document contents and local paths.
        connection.send(("error", "task_failed"))
    finally:
        connection.close()


class JobStore:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        if (type(self.settings.concurrency) is not int or type(self.settings.capacity) is not int
                or not 1 <= self.settings.concurrency <= 8 or not 1 <= self.settings.capacity <= 1024):
            raise ValueError("invalid worker limits")
        if (self.settings.backend not in {"lexical", "onnx"}
                or not math.isfinite(self.settings.retention_seconds)
                or self.settings.retention_seconds <= 0):
            raise ValueError("invalid worker settings")
        self._context = multiprocessing.get_context("spawn")
        self._jobs = {}
        self._active = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._coordinate, daemon=True)
        self._thread.start()

    def submit(self, task, payload, timeout_ms=30000):
        if not isinstance(task, str):
            raise ValueError("task must be a string")
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 300000:
            raise ValueError("timeout_ms must be an integer between 100 and 300000")
        payload = validate(task, payload, self.settings.test_tasks)
        with self._lock:
            if self._stopping.is_set():
                raise CapacityError("worker is shutting down")
            self._expire()
            if len(self._jobs) >= self.settings.capacity:
                raise CapacityError("job capacity reached; wait for retained jobs to expire")
            now = time.monotonic()
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {
                "id": job_id, "task": task, "status": "queued", "result": None,
                "error": None, "progress": None, "timeout_ms": timeout_ms,
                "_input": copy.deepcopy(payload), "_deadline": now + timeout_ms / 1000,
                "_finished": None,
            }
            return self._public(self._jobs[job_id])

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
                self._finish(job, "cancelled")
            return self._public(job)

    def _public(self, job):
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def _expire(self):
        now = time.monotonic()
        for job_id in list(self._jobs):
            finished = self._jobs[job_id]["_finished"]
            if finished is not None and now - finished >= self.settings.retention_seconds:
                del self._jobs[job_id]

    def _finish(self, job, status, result=None, code=None):
        active = self._active.pop(job["id"], None)
        if active:
            process, connection = active
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.2)
            connection.close()
            process.close()
        job.update(status=status, result=result, error={"code": code, "message": "Task did not complete"} if code else None)
        job["_input"] = None
        job["_finished"] = time.monotonic()

    def _coordinate(self):
        while not self._stopping.wait(0.01):
            with self._lock:
                self._expire()
                for job in list(self._jobs.values()):
                    if job["status"] in TERMINAL:
                        continue
                    if time.monotonic() >= job["_deadline"]:
                        self._finish(job, "timed_out", code="deadline_exceeded")
                        continue
                    if job["status"] == "queued" and len(self._active) < self.settings.concurrency:
                        receive = send = process = None
                        try:
                            receive, send = self._context.Pipe(duplex=False)
                            process = self._context.Process(
                                target=_run_child,
                                args=(send, job["task"], job["_input"], self.settings.backend, self.settings.model_dir),
                                daemon=True,
                            )
                            process.start()
                        except Exception:
                            for resource in (send, receive, process):
                                if resource is not None:
                                    try:
                                        resource.close()
                                    except (OSError, ValueError):
                                        pass
                            self._finish(job, "failed", code="worker_start_failed")
                            continue
                        send.close()
                        self._active[job["id"]] = process, receive
                        job["status"] = "running"
                    active = self._active.get(job["id"])
                    if not active:
                        continue
                    process, connection = active
                    self._drain(job, connection)
                    if job["status"] not in TERMINAL and not process.is_alive():
                        # A result can arrive after the first poll, just before exit.
                        self._drain(job, connection)
                        if job["status"] not in TERMINAL:
                            self._finish(job, "failed", code="worker_crashed")

    def _drain(self, job, connection):
        try:
            # Each registered task has a bounded number of progress events.
            while connection.poll():
                kind, value = connection.recv()
                if kind == "progress":
                    job["progress"] = value
                elif kind == "result":
                    self._finish(job, "succeeded", result=value)
                    break
                else:
                    self._finish(job, "failed", code="task_failed")
                    break
        except (EOFError, OSError):
            if job["status"] not in TERMINAL:
                self._finish(job, "failed", code="worker_crashed")

    def close(self):
        self._stopping.set()
        self._thread.join(timeout=2)
        with self._lock:
            for job in self._jobs.values():
                if job["status"] not in TERMINAL:
                    self._finish(job, "cancelled")
