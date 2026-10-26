"""Optional structured logging: JSON lines on stderr with BRIDGE_LOG=json, nothing at all with off (the default).
A line holds only its event's schema keys, each value from a fixed set, a number, a job ID or a token's name, so no
input, result, header, query string, token, error message or exception text can reach it, whatever a caller passes.
Writing happens on a thread of its own behind a bounded queue: a stderr that blocks or fails never holds or fails a
request; its lines are dropped instead."""

import json
import logging
import math
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO, cast

from .tasks import MODEL_NAMES, TEST_TASKS

MODES = ("off", "json")
# Every key a line of each event carries, in this order, null when not given or not valid.
EVENTS = {
    "request": ("method", "route", "status", "duration_ms", "owner"),
    "job": ("job_id", "task", "owner", "job_status", "queue_ms", "run_ms"),
    "start": ("backend", "concurrency", "capacity", "owner_capacity", "retention_seconds", "max_jobs", "drain_seconds",
              "tokens"),
    "stop": ("cancelled",),
    "drain": ("duration_ms", "running", "interrupted"),
}
METHODS = ("GET", "POST", "other")
ROUTES = ("/healthz", "/readyz", "/metrics", "/v1/jobs", "/v1/jobs/{id}", "/v1/jobs/{id}/cancel", "other")
STATUSES = ("succeeded", "failed", "timed_out", "cancelled")
TASKS = frozenset(task for names in MODEL_NAMES.values() for task in names) | frozenset(TEST_TASKS)
# server.TOKEN_NAME, which this module cannot import; a test keeps the two equal.
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_JOB_ID = re.compile(r"[a-f0-9]{32}")
QUEUE_LINES = 1024


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _duration(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(cast(float, value)) and cast(float, value) >= 0


def _matches(pattern: re.Pattern[str]) -> Callable[[object], bool]:
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _among(choices: Any) -> Callable[[object], bool]:
    return lambda value: type(value) is str and value in choices


# What each key's value must be; anything else is written as null.
_VALID: dict[str, Callable[[object], bool]] = {
    "method": _among(METHODS), "route": _among(ROUTES),
    "status": lambda value: type(value) is int and 100 <= value <= 599,
    "duration_ms": _duration, "queue_ms": _duration, "run_ms": _duration,
    "owner": _matches(_NAME), "job_id": _matches(_JOB_ID), "task": _among(TASKS), "job_status": _among(STATUSES),
    "backend": _among(MODEL_NAMES), "retention_seconds": _duration,
    "concurrency": _count, "capacity": _count, "owner_capacity": _count, "max_jobs": _count, "drain_seconds": _count,
    "tokens": _count,
    "cancelled": _count, "running": _count, "interrupted": lambda value: type(value) is bool,
}


def _event(record: logging.LogRecord) -> tuple[str, dict[str, Any]] | None:
    """The (event, fields) a record carries, or None for a record event() did not make."""
    carried = getattr(record, "bridge", None)
    if type(carried) is tuple and len(carried) == 2 and carried[0] in EVENTS and type(carried[1]) is dict:
        return cast(tuple[str, dict[str, Any]], carried)
    return None


class JsonFormatter(logging.Formatter):
    """One JSON object from the record's event and fields alone: the message, its arguments, an exception and a stack
    are never read."""

    def format(self, record: logging.LogRecord) -> str:
        carried = _event(record)
        # The handler's filter passes only the records event() made.
        assert carried is not None
        name, fields = carried
        moment = time.gmtime(record.created)
        line: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", moment) + ".%03dZ" % int(record.msecs),
            "level": "warning" if record.levelno >= logging.WARNING else "info",
            "event": name,
        }
        for key in EVENTS[name]:
            value = fields.get(key)
            line[key] = value if value is not None and _VALID[key](value) else None
        return json.dumps(line, separators=(",", ":"))


class _Enqueue(logging.Handler):
    """Formats in the caller and hands the line over without waiting; a full queue drops it."""

    def __init__(self, lines: queue.Queue[str | None]) -> None:
        super().__init__()
        self._lines = lines
        self.setFormatter(JsonFormatter())
        self.addFilter(lambda record: _event(record) is not None)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.put_nowait(self.format(record))
        except Exception:
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        # The standard handler prints the exception to stderr, which is what must never happen here.
        pass


def _write(lines: queue.Queue[str | None], stream: TextIO, stopped: threading.Event) -> None:
    while True:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            # close() could not queue the end when the queue was full: it ends here once the queue is empty.
            if stopped.is_set():
                return
            continue
        if line is None:
            return
        try:
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            pass


_logger = logging.getLogger("ai_bridge")
_logger.propagate = False
_logger.setLevel(logging.INFO)
# Without a handler of its own the standard library's last resort would print warnings to stderr.
_logger.addHandler(logging.NullHandler())
_lock = threading.Lock()
_active: tuple[_Enqueue, queue.Queue[str | None], threading.Thread, threading.Event] | None = None  # while json is on


def configure(mode: str, stream: TextIO | None = None) -> None:
    """Start writing JSON lines to stream (stderr when None) for json; off writes nothing. Replaces an earlier one."""
    global _active
    if mode not in MODES:
        raise ValueError("log mode must be off or json")
    close()
    if mode == "off":
        return
    lines: queue.Queue[str | None] = queue.Queue(QUEUE_LINES)
    stopped = threading.Event()
    writer = threading.Thread(target=_write, args=(lines, sys.stderr if stream is None else stream, stopped),
                              daemon=True)
    writer.start()
    handler = _Enqueue(lines)
    with _lock:
        _active = (handler, lines, writer, stopped)
    _logger.addHandler(handler)


def close() -> None:
    """Stop logging and let the writer finish what is queued, waiting a moment at most for a stream that blocks."""
    global _active
    with _lock:
        active, _active = _active, None
    if active is None:
        return
    handler, lines, writer, stopped = active
    _logger.removeHandler(handler)
    stopped.set()
    try:
        lines.put(None, timeout=1)
    except queue.Full:
        return
    writer.join(1)


def enabled() -> bool:
    """Whether a line would be written, so a caller can skip measuring and building one when not."""
    return _active is not None


def event(name: str, warning: bool = False, **fields: object) -> None:
    """Write one line of this event; a key outside its schema is dropped and a value of the wrong kind is null."""
    if _active is None:
        return
    try:
        _logger.log(logging.WARNING if warning else logging.INFO, name, extra={"bridge": (name, fields)})
    except Exception:
        pass
