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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .jobs import CapacityError, JobStore, Settings
from .tasks import missing_models

MAX_BODY = 262144


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, token, settings=None):
        if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("BRIDGE_TOKEN must contain at least 32 non-whitespace ASCII characters")
        self.token = token
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
            request.close()
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


class Handler(BaseHTTPRequestHandler):
    server_version = "PhpPythonBridge/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        # Do not log URLs, credentials, inputs or model results.
        pass

    def _reply(self, status, value, headers=()):
        body = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        for name, header in headers:
            self.send_header(name, header)
        self.send_header("Content-Type", "application/json")
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
    # Without its files every job needing a model would fail as task_failed, which cannot say why: a cache fetched
    # before a model was added has no directory for it.
    missing = missing_models(settings.model_dir) if settings.backend == "onnx" else []
    if missing:
        raise SystemExit(f"{settings.model_dir} lacks the model files for {', '.join(missing)}; acquire them with "
                         "the fetcher: docker compose -f docker/compose.yaml run --rm --no-deps fetcher")
    try:
        bridge = BridgeServer((args.host, args.port), os.environ.pop("BRIDGE_TOKEN", ""), settings)
    except sqlite3.Error:
        raise SystemExit(f"BRIDGE_STATE_DIR {settings.state_dir} must be a directory the service can write "
                         "jobs.sqlite in") from None
    with bridge as server:
        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
