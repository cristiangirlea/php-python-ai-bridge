"""Private HTTP transport with bounded requests and explicit service authentication."""

import argparse
import hmac
import json
import os
import re
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .jobs import CapacityError, JobStore, Settings

MAX_BODY = 262144


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, token, settings=None):
        if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("BRIDGE_TOKEN must contain at least 32 non-whitespace ASCII characters")
        self.token = token
        self.slots = threading.BoundedSemaphore(32)
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

    def _reply(self, status, value):
        body = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _error(self, status, code, message):
        self._reply(status, {"error": {"code": code, "message": message}})

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
                if not {"task", "input"} <= body.keys() or body.keys() - {"task", "input", "timeout_ms"}:
                    raise ValueError("Expected task, input and optional timeout_ms")
                job = self.server.jobs.submit(body["task"], body["input"], body.get("timeout_ms", 30000))
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
        except CapacityError:
            self._error(429, "capacity_exceeded", "Worker capacity reached")
        except OverflowError:
            self._error(413, "body_too_large", "Request exceeds 262144 bytes")
        except (ValueError, UnicodeError, RecursionError):
            self._error(400, "invalid_request", "Request does not match the task contract")
        except (TimeoutError, socket.timeout):
            self._error(408, "request_timeout", "Request body was not received in time")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    settings = Settings(
        concurrency=int(os.environ.get("BRIDGE_CONCURRENCY", "2")),
        capacity=int(os.environ.get("BRIDGE_CAPACITY", "128")),
        retention_seconds=float(os.environ.get("BRIDGE_RETENTION_SECONDS", "300")),
        backend=os.environ.get("BRIDGE_BACKEND", "lexical"),
        model_dir=os.environ.get("BRIDGE_MODEL_DIR", "/models"),
        test_tasks=os.environ.get("BRIDGE_TEST_TASKS") == "1",
    )
    with BridgeServer((args.host, args.port), os.environ.get("BRIDGE_TOKEN", ""), settings) as server:
        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
