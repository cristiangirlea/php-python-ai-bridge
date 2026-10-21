"""Private HTTP transport with bounded requests and explicit service authentication."""

import argparse
import hmac
import json
import os
import re
import signal
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .jobs import CapacityError, DrainingError, JobStore, Settings
from .metrics import CONTENT_TYPE, render
from .tasks import missing_models

MAX_BODY = 262144
MAX_REQUEST_LINE = 8192
MAX_HEADER_BLOCK = 16384
_BUSY_BODY = b'{"error":{"code":"service_busy","message":"Every connection slot is taken"}}'
BUSY = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
        b"Cache-Control: no-store\r\nRetry-After: 1\r\nConnection: close\r\n\r\n%s" % (len(_BUSY_BODY), _BUSY_BODY))


class HeadTooLarge(Exception):
    pass


class _Head:
    """The connection's reader, which refuses a request line or header block over its limit before reading it all;
    the body is read through it unchanged."""

    def __init__(self, rfile):
        self._rfile = rfile
        self._left = None  # bytes the header block may still take; None until the request line is read
        self._done = False

    def readline(self, limit=-1):
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

    def __getattr__(self, name):
        return getattr(self._rfile, name)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, token, settings=None, drain_seconds=30):
        if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("BRIDGE_TOKEN must contain at least 32 non-whitespace ASCII characters")
        self.token = token
        self.drain_seconds = drain_seconds
        self._signalled_at = None
        self._interrupted = threading.Event()
        self.slots = threading.BoundedSemaphore(32)
        # The children the store starts inherit this process's environment block as it is at that moment, and
        # removing a variable later leaves it in /proc/<pid>/environ; so the token leaves before any child exists.
        os.environ.pop("BRIDGE_TOKEN", None)
        self.jobs = JobStore(settings)
        try:
            super().__init__(address, Handler)
        except Exception:
            self.jobs.close()
            raise

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            # One non-blocking send that fits any socket buffer, nothing read: a client cannot hold the accept loop.
            try:
                request.setblocking(False)
                request.send(BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def server_close(self):
        super().server_close()
        self.jobs.close()

    def stop(self):
        """A signal's handler: the first starts the drain, a second ends it at once."""
        if self._signalled_at is not None:
            self._interrupted.set()
            # Stopped from here, not by the drain thread, which may still be inside drain().
            threading.Thread(target=self.shutdown, daemon=True).start()
            return
        self._signalled_at = time.monotonic()
        threading.Thread(target=self._drain_then_stop, daemon=True).start()

    def _drain_then_stop(self):
        """Until running jobs end or the deadline passes, then stop serving; server_close ends what still runs."""
        self.jobs.drain()
        deadline = self._signalled_at + self.drain_seconds
        while not self._interrupted.is_set() and self.jobs.busy():
            left = deadline - time.monotonic()
            if left <= 0 or self._interrupted.wait(min(left, 0.05)):
                break
        self.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "PhpPythonBridge/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)
        self.rfile = _Head(self.rfile)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except HeadTooLarge:
            # A long request line is refused before the request is parsed, so these may not be set yet.
            self.requestline, self.request_version = "", "HTTP/1.0"
            self._error(431, "headers_too_large", "Request line or header block exceeds its limit")

    def log_message(self, *_):
        # Do not log URLs, credentials, inputs or model results.
        pass

    def _reply(self, status, value, headers=()):
        self._send(status, json.dumps(value, allow_nan=False, separators=(",", ":")).encode(), "application/json",
                   headers)

    def _text(self, status, text):
        self._send(status, text.encode("utf-8"), CONTENT_TYPE)

    def _send(self, status, body, content_type, headers=()):
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

    def _error(self, status, code, message, headers=()):
        self._reply(status, {"error": {"code": code, "message": message}}, headers)

    def _authorized(self):
        values = self.headers.get_all("Authorization", [])
        expected = ("Bearer " + self.server.token).encode("ascii")
        if len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), expected):
            self._error(401, "unauthorized", "Service authentication required")
            return False
        return True

    def _body(self):
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

        def reject_constant(_):
            raise ValueError("Non-finite JSON numbers are unsupported")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON keys are unsupported")
                result[key] = value
            return result

        body = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_keys)
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def do_GET(self):
        if not self._authorized():
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
        job = self.server.jobs.get(match[1]) if match else None
        if job is None:
            self._error(404, "not_found", "Job or route not found")
        else:
            self._reply(200, job)

    def do_POST(self):
        if not self._authorized():
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
                job = self.server.jobs.submit(body["task"], body["input"], body.get("timeout_ms", 30000),
                                              body.get("idempotency_key"))
                self._reply(202, job)
                return
            match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})/cancel", self.path)
            if body:
                raise ValueError("Cancel body must be an empty object")
            job = self.server.jobs.cancel(match[1]) if match else None
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


def _number(name, default, kind):
    """A numeric setting from the environment; a malformed one stops the service with the variable's name."""
    try:
        return kind(os.environ.get(name, default))
    except ValueError:
        raise SystemExit(f"{name} must be {'an integer' if kind is int else 'a number'}") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    settings = Settings(
        concurrency=_number("BRIDGE_CONCURRENCY", "2", int),
        capacity=_number("BRIDGE_CAPACITY", "128", int),
        retention_seconds=_number("BRIDGE_RETENTION_SECONDS", "300", float),
        backend=os.environ.get("BRIDGE_BACKEND", "lexical"),
        model_dir=os.environ.get("BRIDGE_MODEL_DIR", "/models"),
        test_tasks=os.environ.get("BRIDGE_TEST_TASKS") == "1",
        max_jobs=_number("BRIDGE_WORKER_MAX_JOBS", "1000", int),
        state_dir=os.environ.get("BRIDGE_STATE_DIR") or None,
    )
    drain_seconds = _number("BRIDGE_DRAIN_SECONDS", "30", int)
    if not 0 <= drain_seconds <= 300:
        raise SystemExit("BRIDGE_DRAIN_SECONDS must be an integer from 0 to 300")
    # Without its files every job needing a model would fail as task_failed, which cannot say why: a cache fetched
    # before a model was added has no directory for it.
    missing = missing_models(settings.model_dir) if settings.backend == "onnx" else []
    if missing:
        raise SystemExit(f"{settings.model_dir} lacks the model files for {', '.join(missing)}; acquire them with "
                         "the fetcher: docker compose -f docker/compose.yaml run --rm --no-deps fetcher")
    try:
        bridge = BridgeServer((args.host, args.port), os.environ.pop("BRIDGE_TOKEN", ""), settings,
                              drain_seconds=drain_seconds)
    except sqlite3.Error:
        raise SystemExit(f"BRIDGE_STATE_DIR {settings.state_dir} must be a directory the service can write "
                         "jobs.sqlite in") from None
    with bridge as server:
        def stop(*_):
            server.stop()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
