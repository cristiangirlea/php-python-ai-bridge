"""Private HTTP transport with bounded requests and explicit service authentication."""

import argparse
import hmac
import io
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import log
from .jobs import DEFAULT_OWNER, CapacityError, DrainingError, JobStore, Settings
from .metrics import CONTENT_TYPE, render
from .tasks import missing_models

MAX_BODY = 262144
MAX_REQUEST_LINE = 8192
MAX_HEADER_BLOCK = 16384
# How long a stop waits for a held drain thread's line: past it, the stop goes ahead without one.
DRAIN_LINE_WAIT = 5
# What a request line names as the route: the template a path matches, never the path itself.
_ROUTES = {"/healthz": "/healthz", "/readyz": "/readyz", "/metrics": "/metrics", "/v1/jobs": "/v1/jobs"}
_JOB_ROUTES = ((re.compile(r"/v1/jobs/[a-f0-9]{32}"), "/v1/jobs/{id}"),
               (re.compile(r"/v1/jobs/[a-f0-9]{32}/cancel"), "/v1/jobs/{id}/cancel"))
_BUSY_BODY = b'{"error":{"code":"service_busy","message":"Every connection slot is taken"}}'
BUSY = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
        b"Cache-Control: no-store\r\nRetry-After: 1\r\nConnection: close\r\n\r\n%s" % (len(_BUSY_BODY), _BUSY_BODY))


class HeadTooLarge(Exception):
    pass


class _Head:
    """The connection's reader, which refuses a request line or header block over its limit before reading it all;
    the body is read through it unchanged."""

    def __init__(self, rfile: io.BufferedIOBase) -> None:
        self._rfile = rfile
        self._left: int | None = None  # bytes the header block may still take; None until the request line is read
        self._done = False

    def readline(self, limit: int = -1) -> bytes:
        if self._done:
            return self._rfile.readline(limit)
        if self._left is None:
            line = self._rfile.readline(MAX_REQUEST_LINE + 3)
            # One line end is not counted, so a run of carriage returns or a truncated read is over the limit.
            content = line[:-2] if line.endswith(b"\r\n") else line[:-1] if line.endswith(b"\n") else line
            if len(content) > MAX_REQUEST_LINE:
                raise HeadTooLarge
            self._left = MAX_HEADER_BLOCK
            return line
        # Read past the budget by a little, so a line that ends exactly on it can be told from a longer one.
        line = self._rfile.readline(self._left + 3)
        if line in (b"\r\n", b"\n", b""):
            self._done = True
            return line
        self._left -= len(line)
        if self._left < 0:
            raise HeadTooLarge
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self._rfile, name)
MAX_TOKENS = 64
MAX_TOKENS_FILE = 65536
TOKEN_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def check_tokens(tokens: dict[str, str], source: str = "the token list") -> dict[str, str]:
    """Refuse a token list the service must not start with; the message names the source, never a name or token."""
    if not tokens:
        raise ValueError(f"{source} holds no token")
    if len(tokens) > MAX_TOKENS:
        raise ValueError(f"{source} holds more than {MAX_TOKENS} tokens")
    for name, token in tokens.items():
        if type(name) is not str or not TOKEN_NAME.fullmatch(name):
            raise ValueError(f"{source}: every name must be 1-64 characters of A-Z, a-z, 0-9, '_', '.' and '-'")
        if type(token) is not str or len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError(f"{source}: every token must contain at least 32 non-whitespace ASCII characters")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError(f"{source} gives two names the same token")
    return tokens


def _entries(lines: list[str], source: str) -> dict[str, str]:
    """name=token entries as a mapping; the token is everything after the first "=", since a token may hold one."""
    if len(lines) > MAX_TOKENS:
        raise ValueError(f"{source} holds more than {MAX_TOKENS} tokens")
    tokens: dict[str, str] = {}
    for line in lines:
        name, separator, token = line.partition("=")
        if not separator or not TOKEN_NAME.fullmatch(name):
            raise ValueError(f"{source}: every entry must be name=token, the name 1-64 characters of A-Z, a-z, 0-9, "
                             "'_', '.' and '-'")
        if name in tokens:
            raise ValueError(f"{source} names a token twice")
        tokens[name] = token
    return check_tokens(tokens, source)


def _read_tokens_file(path: str) -> dict[str, str]:
    """The file's entries, read once; a file others could read or replace is refused, since it holds every token."""
    try:
        # Non-blocking, so a FIFO at the path cannot hold the start up; a regular file reads as usual.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as file:
            mode = os.fstat(file.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("BRIDGE_TOKENS_FILE must be a regular file")
            if mode & 0o077:
                raise ValueError("BRIDGE_TOKENS_FILE must not be open to group or others: give it mode 0600 or stricter")
            raw = file.read(MAX_TOKENS_FILE + 1)
    except OSError:
        raise ValueError("BRIDGE_TOKENS_FILE cannot be read by the service") from None
    if len(raw) > MAX_TOKENS_FILE:
        raise ValueError(f"BRIDGE_TOKENS_FILE is larger than {MAX_TOKENS_FILE} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("BRIDGE_TOKENS_FILE is not UTF-8 text") from None
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return _entries(lines, "BRIDGE_TOKENS_FILE")


def resolve_tokens() -> dict[str, str]:
    """The service's tokens by name from BRIDGE_TOKEN (named default), BRIDGE_TOKENS or BRIDGE_TOKENS_FILE, exactly one
    of them. Both token variables leave the environment here, before any child exists: the store's children inherit
    the environment block, and a variable removed later stays in /proc/<pid>/environ. The file's path may stay."""
    single = os.environ.pop("BRIDGE_TOKEN", "")
    listed = os.environ.pop("BRIDGE_TOKENS", "")
    path = os.environ.get("BRIDGE_TOKENS_FILE", "")
    try:
        if single and (listed or path):
            raise ValueError("BRIDGE_TOKEN cannot be set together with BRIDGE_TOKENS or BRIDGE_TOKENS_FILE")
        if listed and path:
            raise ValueError("BRIDGE_TOKENS and BRIDGE_TOKENS_FILE cannot both be set")
        if path:
            return _read_tokens_file(path)
        if listed:
            return _entries(listed.split(","), "BRIDGE_TOKENS")
        if single:
            return check_tokens({DEFAULT_OWNER: single}, "BRIDGE_TOKEN")
        raise ValueError("Set BRIDGE_TOKEN, BRIDGE_TOKENS or BRIDGE_TOKENS_FILE to the service's tokens")
    except ValueError as error:
        raise SystemExit(str(error)) from None


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], tokens: str | dict[str, str], settings: Settings | None = None,
                 drain_seconds: int = 30) -> None:
        """tokens maps each name to its token; a single token is the name default."""
        if isinstance(tokens, str):
            tokens = {DEFAULT_OWNER: tokens}
        check_tokens(tokens)
        # The header each name is presented with; every request is compared with all of them.
        self.credentials = tuple((name, ("Bearer " + token).encode("ascii")) for name, token in tokens.items())
        self.drain_seconds = drain_seconds
        self.cancelled = 0  # unfinished jobs the stop ended, for the stop line
        self._signalled_at: float | None = None
        self._interrupted = threading.Event()
        self._drainer: threading.Thread | None = None
        self._drained = threading.Event()  # the drain thread is done with its line: fields published, or none to write
        self._drain_lock = threading.Lock()
        self._drain_line: dict[str, Any] | None = None  # those fields until the line is written
        self._drain_closed = False  # set by server_close: no drain line after it, since stop's follows
        self.slots = threading.BoundedSemaphore(32)
        # The children the store starts inherit this process's environment block as it is at that moment, and
        # removing a variable later leaves it in /proc/<pid>/environ; so the tokens leave before any child exists.
        os.environ.pop("BRIDGE_TOKEN", None)
        os.environ.pop("BRIDGE_TOKENS", None)
        self.jobs = JobStore(replace(settings or Settings(), owners=tuple(tokens)))
        try:
            super().__init__(address, Handler)
        except Exception:
            self.jobs.close()
            raise

    def process_request(self, request: Any, address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            # One non-blocking send that fits any socket buffer, nothing read: a client cannot hold the accept loop.
            try:
                request.setblocking(False)
                sent = request.send(BUSY) == len(BUSY)
            except OSError:
                sent = False
            self.shutdown_request(request)
            # A reply that did not go out is no reply, as for a connection that ends before one.
            if sent:
                log.event("request", warning=True, method=None, route="other", status=503, duration_ms=0, owner=None)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, address: Any) -> None:
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints the peer's address and a traceback to stderr, which takes only JSON lines or nothing.
        pass

    def server_close(self) -> None:
        super().server_close()
        if self._drainer is not None:
            # A second signal can stop serving while the drain thread is still held; its line still goes before stop's.
            self._drained.wait(DRAIN_LINE_WAIT)
            self._write_drain(last=True)
        self.cancelled = self.jobs.close()

    def stop(self) -> None:
        """A signal's handler: the first starts the drain, a second ends it at once."""
        if self._signalled_at is not None:
            self._interrupted.set()
            # Stopped from here, not by the drain thread, which may still be inside drain().
            threading.Thread(target=self.shutdown, daemon=True).start()
            return
        self._signalled_at = time.monotonic()
        self._drainer = threading.Thread(target=self._drain_then_stop, daemon=True)
        self._drainer.start()

    def _drain_then_stop(self) -> None:
        """Until running jobs end or the deadline passes, then stop serving; server_close ends what still runs."""
        assert self._signalled_at is not None  # stop() sets it before it starts this thread
        self.jobs.drain()
        deadline = self._signalled_at + self.drain_seconds
        while not self._interrupted.is_set() and self.jobs.busy():
            left = deadline - time.monotonic()
            if left <= 0 or self._interrupted.wait(min(left, 0.05)):
                break
        if log.enabled():
            running = self.jobs.snapshot()["running"]
            with self._drain_lock:
                self._drain_line = {"duration_ms": round((time.monotonic() - self._signalled_at) * 1000, 3),
                                    "running": running, "interrupted": self._interrupted.is_set()}
        self._drained.set()
        self._write_drain()
        self.shutdown()

    def _write_drain(self, last: bool = False) -> None:
        """The drain line from the published fields, once, by the drain thread or server_close, whichever is first;
        last is server_close's call, after which a drain thread published too late writes nothing."""
        with self._drain_lock:
            fields, self._drain_line = self._drain_line, None
            # Queued under the lock, so server_close cannot pass this and write stop's line first.
            if fields is not None and not self._drain_closed:
                log.event("drain", warning=fields["running"] > 0, **fields)
            self._drain_closed = self._drain_closed or last


class Handler(BaseHTTPRequestHandler):
    server_version = "PhpPythonBridge/0.1"
    sys_version = ""
    server: BridgeServer
    _owner: str | None
    _status: int | None

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)
        # _Head is not a BufferedIOBase: it forwards every other attribute to the stream it wraps.
        self.rfile = _Head(self.rfile)  # type: ignore[assignment]

    def handle_one_request(self) -> None:
        logged = log.enabled()
        started = time.monotonic() if logged else 0.0
        # The standard library types command and path as str; they are None until a request line parses.
        self.command = self.path = self._owner = self._status = None  # type: ignore[assignment]
        try:
            super().handle_one_request()
        except HeadTooLarge:
            # A long request line is refused before the request is parsed, so these may not be set yet.
            self.requestline, self.request_version = "", "HTTP/1.0"
            self._error(431, "headers_too_large", "Request line or header block exceeds its limit")
        finally:
            # After the reply is written; a connection that ended before any reply was written has no line.
            if logged and self._status is not None:
                log.event("request", warning=self._status >= 500, method=self._method(), route=self._route(),
                          status=self._status, duration_ms=round((time.monotonic() - started) * 1000, 3),
                          owner=self._owner)

    def _method(self) -> str | None:
        if self.command is None:
            return None
        return self.command if self.command in ("GET", "POST") else "other"

    def _route(self) -> str:
        if self.path in _ROUTES:
            return _ROUTES[self.path]
        if self.path is not None:
            for pattern, route in _JOB_ROUTES:
                if pattern.fullmatch(self.path):
                    return route
        return "other"

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        super().send_error(code, message, explain)
        # The standard library's own refusals write their whole reply here, and pass an HTTPStatus.
        self._status = int(code)

    def log_message(self, *_: Any) -> None:
        # Do not log URLs, credentials, inputs or model results.
        pass

    def _reply(self, status: int, value: object, headers: Iterable[tuple[str, str]] = ()) -> None:
        self._send(status, json.dumps(value, allow_nan=False, separators=(",", ":")).encode(), "application/json",
                   headers)

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), CONTENT_TYPE)

    def _send(self, status: int, body: bytes, content_type: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        for name, header in headers:
            self.send_header(name, header)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
        # Only now sent: a peer gone mid-reply raises above and gets no request line.
        self._status = status

    def _error(self, status: int, code: str, message: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        self._reply(status, {"error": {"code": code, "message": message}}, headers)

    def _authorized(self) -> str | None:
        """The name of the token presented, or None once a 401 is sent. Every token is compared, whichever matches."""
        values = self.headers.get_all("Authorization", [])
        presented = values[0].encode("utf-8") if len(values) == 1 else b""
        owner = None
        for name, expected in self.server.credentials:
            if hmac.compare_digest(presented, expected) and owner is None:
                owner = name
        if len(values) != 1 or owner is None:
            self._error(401, "unauthorized", "Service authentication required")
            return None
        self._owner = owner
        return owner

    def _body(self) -> dict[str, Any]:
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]):
            raise ValueError("A single Content-Length is required; chunked bodies are unsupported")
        size = int(lengths[0])
        if size > MAX_BODY:
            raise OverflowError("Request exceeds 262144 bytes")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Incomplete request body")

        def reject_constant(_: str) -> None:
            raise ValueError("Non-finite JSON numbers are unsupported")

        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON keys are unsupported")
                result[key] = value
            return result

        body = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_keys)
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def do_GET(self) -> None:
        owner = self._authorized()
        if owner is None:
            return
        if self.path == "/healthz":
            self._reply(200, {"status": "ok", "protocol": 1, "backend": self.server.jobs.settings.backend})
            return
        if self.path == "/readyz":
            reason = self.server.jobs.readiness()
            if reason is None:
                self._reply(200, {"status": "ready"})
            else:
                self._reply(503, {"status": "not_ready", "reason": reason})
            return
        if self.path == "/metrics":
            # The snapshot is taken under the store's lock; the formatting is not.
            self._text(200, render(self.server.jobs.snapshot()))
            return
        match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})", self.path)
        job = self.server.jobs.get(match[1], owner) if match else None
        if job is None:
            self._error(404, "not_found", "Job or route not found")
        else:
            self._reply(200, job)

    def do_POST(self) -> None:
        owner = self._authorized()
        if owner is None:
            return
        try:
            body = self._body()
            if self.path == "/v1/jobs":
                if self.server.jobs.draining:
                    raise DrainingError
                if (not {"task", "input"} <= body.keys()
                        or body.keys() - {"task", "input", "timeout_ms", "idempotency_key"}):
                    raise ValueError("Expected task, input, optional timeout_ms and optional idempotency_key")
                # The store reads None as no key, so an explicit null is refused here rather than ignored.
                if "idempotency_key" in body and body["idempotency_key"] is None:
                    raise ValueError("idempotency_key must be a string")
                job: dict[str, Any] | None = self.server.jobs.submit(body["task"], body["input"], body.get("timeout_ms", 30000),
                                              body.get("idempotency_key"), owner)
                self._reply(202, job)
                return
            match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})/cancel", self.path)
            if body:
                raise ValueError("Cancel body must be an empty object")
            job = self.server.jobs.cancel(match[1], owner) if match else None
            if job is None:
                self._error(404, "not_found", "Job or route not found")
            else:
                self._reply(200, job)
        except DrainingError:
            self._error(503, "draining", "Worker is draining before it stops", [("Retry-After", "1")])
        except CapacityError as error:
            # Delay seconds, never an HTTP date: a client's clock need not agree with this one.
            self._error(429, "capacity_exceeded", "Worker capacity reached", [("Retry-After", str(int(error.retry_after)))])
        except OverflowError:
            self._error(413, "body_too_large", "Request exceeds 262144 bytes")
        except (ValueError, UnicodeError, RecursionError):
            self._error(400, "invalid_request", "Request does not match the task contract")
        except (TimeoutError, socket.timeout):
            self._error(408, "request_timeout", "Request body was not received in time")


def _number[T](name: str, default: str, kind: Callable[[str], T]) -> T:
    """A numeric setting from the environment; a malformed one stops the service with the variable's name."""
    try:
        return kind(os.environ.get(name, default))
    except ValueError:
        raise SystemExit(f"{name} must be {'an integer' if kind is int else 'a number'}") from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    tokens = resolve_tokens()
    settings = Settings(
        concurrency=_number("BRIDGE_CONCURRENCY", "2", int),
        capacity=_number("BRIDGE_CAPACITY", "128", int),
        result_capacity=_number("BRIDGE_RESULT_CAPACITY", "384", int),
        retention_seconds=_number("BRIDGE_RETENTION_SECONDS", "300", float),
        backend=os.environ.get("BRIDGE_BACKEND", "lexical"),
        model_dir=os.environ.get("BRIDGE_MODEL_DIR", "/models"),
        test_tasks=os.environ.get("BRIDGE_TEST_TASKS") == "1",
        max_jobs=_number("BRIDGE_WORKER_MAX_JOBS", "1000", int),
        state_dir=os.environ.get("BRIDGE_STATE_DIR") or None,
        owner_capacity=_number("BRIDGE_OWNER_CAPACITY", "", int) if "BRIDGE_OWNER_CAPACITY" in os.environ else None,
    )
    if not 1 <= settings.result_capacity <= 65536:
        raise SystemExit("BRIDGE_RESULT_CAPACITY must be an integer from 1 to 65536")
    if settings.owner_capacity is not None and not 1 <= settings.owner_capacity <= settings.capacity + settings.result_capacity:
        raise SystemExit("BRIDGE_OWNER_CAPACITY must be an integer from 1 to the sum of BRIDGE_CAPACITY and BRIDGE_RESULT_CAPACITY")
    drain_seconds = _number("BRIDGE_DRAIN_SECONDS", "30", int)
    if not 0 <= drain_seconds <= 300:
        raise SystemExit("BRIDGE_DRAIN_SECONDS must be an integer from 0 to 300")
    log_mode = os.environ.get("BRIDGE_LOG") or "off"
    if log_mode not in log.MODES:
        raise SystemExit("BRIDGE_LOG must be off or json")
    # Without its files every job needing a model would fail as task_failed, which cannot say why: a cache fetched
    # before a model was added has no directory for it.
    missing = missing_models(settings.model_dir) if settings.backend == "onnx" else []
    if missing:
        raise SystemExit(f"{settings.model_dir} lacks the model files for {', '.join(missing)}; acquire them with "
                         "the fetcher: docker compose -f docker/compose.yaml run --rm --no-deps fetcher")
    try:
        bridge = BridgeServer((args.host, args.port), tokens, settings, drain_seconds=drain_seconds)
    except sqlite3.Error:
        raise SystemExit(f"BRIDGE_STATE_DIR {settings.state_dir} must be a directory the service can write "
                         "jobs.sqlite in") from None
    log.configure(log_mode)
    try:
        log.event("start", backend=settings.backend, concurrency=settings.concurrency, capacity=settings.capacity,
                  result_capacity=settings.result_capacity,
                  owner_capacity=settings.capacity + settings.result_capacity if settings.owner_capacity is None else settings.owner_capacity,
                  retention_seconds=settings.retention_seconds, max_jobs=settings.max_jobs,
                  drain_seconds=drain_seconds, tokens=len(tokens))
        with bridge as server:
            def stop(*_: Any) -> None:
                server.stop()

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            server.serve_forever(poll_interval=0.1)
        log.event("stop", warning=bool(bridge.cancelled), cancelled=bridge.cancelled)
    finally:
        log.close()


if __name__ == "__main__":
    main()
