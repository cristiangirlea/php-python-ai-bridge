"""The MCP server's own client of the HTTP protocol, exercised against a real in-process worker."""

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from bridge_mcp.protocol import Bridge, BridgeError

TOKEN = "test-only-bridge-token-never-use-in-production"


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=16, test_tasks=True))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}"
        cls.bridge = Bridge(cls.url, TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def test_construction_mirrors_the_php_client_rules(self):
        for url in ["ftp://x", "http://user:pw@host", "http://host/path", "http://host?q=1", "http://host#f", ""]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Bridge(url, TOKEN)
        with self.assertRaises(ValueError):
            Bridge(self.url, "short")

    def test_health_reports_backend_and_protocol(self):
        health = self.bridge.health()
        self.assertEqual((health["protocol"], health["backend"]), (1, "lexical"))

    def test_submit_get_and_cancel_round_trip(self):
        job = self.bridge.submit("test.delay", {"seconds": 2}, 5000)
        self.assertEqual((job["task"], job["status"]), ("test.delay", "queued"))
        self.assertEqual(self.bridge.get(job["id"])["id"], job["id"])
        self.assertEqual(self.bridge.cancel(job["id"])["status"], "cancelled")

    def test_run_returns_the_result_and_forwards_progress(self):
        seen = []
        result = self.bridge.run("test.delay", {"seconds": 0.3, "value": 7}, deadline_s=5,
                                 on_progress=lambda completed, total: seen.append((completed, total)), poll_s=0.05)
        self.assertEqual(result["value"], 7)
        # The final report is always delivered once, even when polling missed the intermediate ones.
        self.assertEqual(seen[-1], (1, 1))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))

    def test_local_deadline_cancels_the_remote_job(self):
        started = time.monotonic()
        with self.assertRaises(BridgeError) as caught:
            self.bridge.run("test.delay", {"seconds": 5}, deadline_s=0.3, poll_s=0.05)
        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.bridge.get(caught.exception.job_id)["status"], "cancelled")

    def test_an_interrupted_wait_cancels_the_job(self):
        def explode(completed, total):
            raise RuntimeError("the host went away")

        with patch.object(self.bridge, "cancel", wraps=self.bridge.cancel) as cancel:
            with self.assertRaises(RuntimeError):
                self.bridge.run("test.delay", {"seconds": 3}, deadline_s=5, on_progress=explode, poll_s=0.05)
        cancel.assert_called_once()
        self.assertEqual(self.bridge.get(cancel.call_args.args[0])["status"], "cancelled")

    def test_non_json_error_pages_keep_their_status(self):
        class Unavailable(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>maintenance</html>"
                self.send_response(503)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        proxy = HTTPServer(("127.0.0.1", 0), Unavailable)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(BridgeError) as caught:
                Bridge("http://127.0.0.1:%d" % proxy.server_address[1], TOKEN).health()
        finally:
            proxy.shutdown()
            thread.join()
            proxy.server_close()
        self.assertEqual((caught.exception.code, caught.exception.status), ("http_error", 503))

    def test_task_failures_become_errors_with_the_worker_code(self):
        for task, code in [("test.crash", "worker_crashed"), ("test.error", "task_failed")]:
            with self.subTest(task=task), self.assertRaises(BridgeError) as caught:
                self.bridge.run(task, {}, deadline_s=5, poll_s=0.05)
            self.assertEqual(caught.exception.code, code)

    def test_contract_and_authentication_failures_carry_status_codes(self):
        with self.assertRaises(BridgeError) as caught:
            self.bridge.submit("rerank", {"query": "x"}, 5000)
        self.assertEqual((caught.exception.status, caught.exception.code), (400, "invalid_request"))
        with self.assertRaises(BridgeError) as caught:
            Bridge(self.url, "x" * 32).health()
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(BridgeError) as caught:
            self.bridge.get("a" * 32)
        self.assertEqual(caught.exception.status, 404)

    def test_transport_failures_are_errors_not_exceptions_from_the_socket(self):
        with self.assertRaises(BridgeError) as caught:
            Bridge("http://127.0.0.1:1", TOKEN, request_timeout_s=0.5).health()
        self.assertEqual(caught.exception.code, "transport_error")

    def test_errors_never_carry_the_token(self):
        for action in [lambda: self.bridge.submit("rerank", {}, 5000), lambda: Bridge("http://127.0.0.1:1", TOKEN, 0.5).health()]:
            with self.assertRaises(BridgeError) as caught:
                action()
            self.assertNotIn(TOKEN, str(caught.exception))
