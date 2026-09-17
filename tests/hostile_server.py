"""Test-only peers returning malformed responses. Never used by the task service."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.respond("wrong_id")

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.respond(data.get("task", "invalid"))

    def respond(self, scenario):
        status, body, content_type = 200, b"{}", "application/json"
        if scenario == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://worker:8090/healthz")
            self.end_headers()
            return
        if scenario == "malformed":
            body = b"not-json-secret-value"
        elif scenario == "oversized":
            body = b"x" * 262145
        elif scenario == "content_type":
            content_type = "text/html"
        elif scenario == "upstream_error":
            status, body = 500, b"secret-token-do-not-reflect"
        elif scenario in {"wrong_id", "wrong_task", "wrong_timeout", "wrong_status"}:
            body = json.dumps({"id": "b" * 32,
                               "task": "rerank" if scenario in {"wrong_id", "wrong_task"} else scenario,
                               "status": "running" if scenario == "wrong_status" else "queued",
                               "result": None, "error": None, "progress": None,
                               "timeout_ms": 100 if scenario == "wrong_timeout" else 30000}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
