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
    def test_both_job_id_forms_and_invalid_ids(self):
        valid = ['a' * 32, 'a-' + 'f' * 32, '9-az--' + '0' * 32, 'z' * 16 + '-' + 'a' * 32]
        invalid = ['A-' + 'a' * 32, '-a-' + 'a' * 32, 'a_b-' + 'a' * 32,
                   'z' * 17 + '-' + 'a' * 32, 'a-' + 'a' * 31, 'a-' + 'A' * 32,
                   'a-' + 'a' * 33, 'a-' + 'a' * 32 + '\n', '../healthz']
        for job_id in valid + invalid:
            body = {'id': job_id, 'task': 'rerank', 'status': 'queued', 'result': None,
                    'error': None, 'progress': None, 'timeout_ms': 30000}
            with self.subTest(job_id=job_id):
                if job_id in valid:
                    self.assertEqual(Bridge._id(job_id), job_id)
                    self.assertEqual(Bridge._job(body)['id'], job_id)
                else:
                    with self.assertRaises(BridgeError):
                        Bridge._job(body)
                    for method in (self.bridge.get, self.bridge.cancel):
                        with self.assertRaises(ValueError):
                            method(job_id)

    def test_prefixed_worker_submit_poll_result_and_cancel(self):
        service = BridgeServer(('127.0.0.1', 0), TOKEN, Settings(instance_id='mcp-1', test_tasks=True))
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = Bridge('http://127.0.0.1:%d' % service.server_address[1], TOKEN)
            job = bridge.submit('test.delay', {'seconds': 2}, 5000)
            self.assertRegex(job['id'], r'^mcp-1-[a-f0-9]{32}$')
            self.assertEqual(bridge.get(job['id'])['id'], job['id'])
            self.assertEqual(bridge.cancel(job['id'])['status'], 'cancelled')
            self.assertEqual(bridge.run('test.delay', {'seconds': 0.01, 'value': 7}, deadline_s=5)['value'], 7)
        finally:
            service.shutdown()
            thread.join()
            service.server_close()

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

    def refusal(self, *headers):
        """What the client makes of a 429 that carries these headers."""
        class Full(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"error":{"code":"capacity_exceeded","message":"Worker capacity reached"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        stub = HTTPServer(("127.0.0.1", 0), Full)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(BridgeError) as caught:
                Bridge("http://127.0.0.1:%d" % stub.server_address[1], TOKEN).health()
        finally:
            stub.shutdown()
            thread.join()
            stub.server_close()
        return caught.exception

    def test_a_capacity_refusal_carries_the_retry_after_seconds(self):
        error = self.refusal(("Retry-After", "7"))
        self.assertEqual((error.status, error.code, error.retry_after), (429, "capacity_exceeded", 7))
        self.assertIsNone(self.refusal().retry_after)
        self.assertEqual(self.refusal(("Retry-After", "0")).retry_after, 0)
        for value in ["Wed, 21 Oct 2026 07:28:00 GMT", "1234567", "-1", "1.5", ""]:
            with self.subTest(value=value):
                self.assertIsNone(self.refusal(("Retry-After", value)).retry_after)
        self.assertIsNone(self.refusal(("Retry-After", "7"), ("Retry-After", "3")).retry_after)

    def test_the_real_worker_sends_retry_after_on_a_full_store(self):
        server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=1, test_tasks=True))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = Bridge("http://127.0.0.1:%d" % server.server_address[1], TOKEN)
            bridge.submit("test.delay", {"seconds": 5}, 10000)
            with self.assertRaises(BridgeError) as caught:
                bridge.submit("test.delay", {"seconds": 0}, 10000)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        # The one slot holds a job still running, so capacity returns no sooner than the worker's short default.
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 1))

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
