import contextlib
import http.client
import io
import json
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from ai_bridge import jobs, log, metrics, server, tasks
from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from test_log import BlockedStream, BrokenStream, parse as parse_log

TOKEN = "test-only-bridge-token-never-use-in-production"


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=8))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def auth(self):
        return {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}

    def test_authentication_required_for_health_and_jobs(self):
        for path in ["/healthz", "/v1/jobs/" + "a" * 32]:
            self.assertEqual(self.request("GET", path)[0], 401)

    def test_wrong_credentials(self):
        self.assertEqual(self.request("GET", "/healthz", headers={"Authorization": "Bearer wrong"})[0], 401)

    def test_health(self):
        status, data = self.request("GET", "/healthz", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(data, {"status": "ok", "protocol": 1, "backend": "lexical"})

    def test_submission_contract(self):
        body = json.dumps({"task": "rerank", "input": {"query": "x", "documents": ["x"]}})
        status, data = self.request("POST", "/v1/jobs", body, self.auth())
        self.assertEqual(status, 202)
        self.assertEqual(data["status"], "queued")
        self.assertNotIn("_input", data)
        body = json.dumps({"task": "rerank", "input": {"query": "x", "documents": ["x", "y"], "top_k": 1}})
        self.assertEqual(self.request("POST", "/v1/jobs", body, self.auth())[0], 202)
        body = json.dumps({"task": "embed", "input": {"texts": ["x", "y"]}})
        self.assertEqual(self.request("POST", "/v1/jobs", body, self.auth())[0], 202)
        body = json.dumps({"task": "redact", "input": {"text": "x", "entities": ["PER"], "min_score": 0.9}})
        self.assertEqual(self.request("POST", "/v1/jobs", body, self.auth())[0], 202)

    def test_invalid_bodies(self):
        for body in ["[]", "null", "{", '{"task":"rerank","task":"test.crash","input":{}}',
                     '{"task":"rerank","input":{},"extra":1}',
                     '{"task":"test.crash","input":{}}',
                     '{"task":"rerank","input":{"query":NaN,"documents":["x"]}}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"],"top_k":2}}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"],"top_k":null}}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":""}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":"' + "a" * 129 + '"}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":"has space"}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":"café"}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":"\\u007f"}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":123}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"]},"idempotency_key":null}',
                     '{"task":"embed","input":{"texts":[]}}',
                     '{"task":"embed","input":{"texts":["x"],"query":"x"}}',
                     '{"task":"redact","input":{"text":""}}',
                     '{"task":"redact","input":{"text":"x","entities":["MISC"]}}']:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/v1/jobs", body, self.auth())[0], 400)

    def test_repeated_idempotency_key_returns_202_with_existing_job(self):
        body = json.dumps({"task": "rerank", "input": {"query": "x", "documents": ["x"]}, "idempotency_key": "http-k1"})
        status, first = self.request("POST", "/v1/jobs", body, self.auth())
        self.assertEqual(status, 202)
        status, repeated = self.request("POST", "/v1/jobs", body, self.auth())
        self.assertEqual(status, 202)
        self.assertEqual(repeated["id"], first["id"])
        other = json.dumps({"task": "rerank", "input": {"query": "x", "documents": ["x"]}, "idempotency_key": "http-k2"})
        status, second = self.request("POST", "/v1/jobs", other, self.auth())
        self.assertEqual(status, 202)
        self.assertNotEqual(second["id"], first["id"])

    def test_a_submission_the_job_table_refuses_is_429(self):
        database = MagicMock()
        database.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        body = json.dumps({"task": "rerank", "input": {"query": "x", "documents": ["x"]}, "idempotency_key": "http-k3"})
        with patch.object(self.server.jobs, "_db", database):
            status, data = self.request("POST", "/v1/jobs", body, self.auth())
        self.assertEqual(status, 429)
        self.assertEqual(data["error"]["code"], "capacity_exceeded")
        self.assertNotIn(("default", "http-k3"), self.server.jobs._keys)

    def test_body_limit(self):
        self.assertEqual(self.request("POST", "/v1/jobs", " " * 262145, self.auth())[0], 413)

    def test_content_type(self):
        headers = self.auth()
        headers["Content-Type"] = "text/plain"
        self.assertEqual(self.request("POST", "/v1/jobs", "{}", headers)[0], 400)

    def test_unknown_job(self):
        self.assertEqual(self.request("GET", "/v1/jobs/" + "a" * 32, headers=self.auth())[0], 404)

    def test_query_parameters_do_not_change_route(self):
        self.assertEqual(self.request("GET", "/healthz?token=" + TOKEN, headers=self.auth())[0], 404)

    def test_weak_token_is_rejected(self):
        for token in ["short", TOKEN + "\n", TOKEN + "\x00", TOKEN + "\x7f", TOKEN + "é"]:
            with self.subTest(token=repr(token)), self.assertRaises(ValueError):
                BridgeServer(("127.0.0.1", 0), token)


if __name__ == "__main__":
    unittest.main()


class CapacityHttpTests(unittest.TestCase):
    """A store of its own, so the full one never refuses the other HTTP tests."""

    def setUp(self):
        self.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=1, retention_seconds=300))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def submit(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request("POST", "/v1/jobs", json.dumps({"task": "rerank", "input": {"query": "x", "documents": ["x"]}}),
                               {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.getheader("Retry-After"), json.loads(response.read())
        finally:
            connection.close()

    def test_a_refusal_for_capacity_says_when_to_retry_in_whole_seconds(self):
        status, retry_after, job = self.submit()
        self.assertEqual((status, retry_after), (202, None))
        deadline = time.monotonic() + 5
        while self.server.jobs.get(job["id"])["status"] != "succeeded":
            self.assertLess(time.monotonic(), deadline, "the job never finished")
            time.sleep(0.01)
        status, retry_after, body = self.submit()
        self.assertEqual((status, body["error"]["code"]), (429, "capacity_exceeded"))
        # Whole seconds, never an HTTP date: the finished job holds its slot for the 300 s retention.
        self.assertRegex(retry_after, r"\A[1-9][0-9]*\Z")
        self.assertIn(int(retry_after), (299, 300))

    def test_a_refusal_for_capacity_is_counted_once_per_429(self):
        def rejected():
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
            try:
                connection.request("GET", "/metrics", headers={"Authorization": "Bearer " + TOKEN})
                return sample(parse_metrics(connection.getresponse().read().decode()),
                              "bridge_submissions_rejected_total", owner="default", reason="capacity")
            finally:
                connection.close()

        self.assertEqual(self.submit()[0], 202)
        self.assertEqual(rejected(), 0)
        for count in (1, 2):
            self.assertEqual(self.submit()[0], 429)
            self.assertEqual(rejected(), count)

    def test_a_shutting_down_store_says_one_second(self):
        self.server.jobs.close()
        status, retry_after, _ = self.submit()
        self.assertEqual((status, retry_after), (429, "1"))


def exchange(address, data=b"", wait=2):
    """Send raw bytes, read until the server closes; (status, headers, body). Raw, so nothing a client library adds
    or refuses gets in the way of the limits under test."""
    with socket.create_connection(address, timeout=wait) as connection:
        try:
            connection.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # refused part way through; the answer is already on its way
        received = b""
        try:
            while chunk := connection.recv(65536):
                received += chunk
        except ConnectionResetError:
            # A refusal that left request bytes unread ends in a reset after the answer.
            pass
    head, _, body = received.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return int(lines[0].split(" ")[1]), headers, body


def _stalled_serve(connection, backend, model_dir):
    """A child whose warm-up takes about three seconds: the ONNX warm-up with every model load stalled."""
    with patch.object(tasks, "_onnx", side_effect=lambda *_: time.sleep(0.75)):
        jobs._serve(connection, "onnx", model_dir)


class DrainHttpTests(unittest.TestCase):
    """The first signal drains: submissions are refused, GETs answer, running jobs may finish until the deadline."""

    def start(self, drain_seconds):
        self.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(concurrency=1, test_tasks=True),
                                   drain_seconds=drain_seconds)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def raw(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body, {"Authorization": "Bearer " + TOKEN,
                                                    "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def submit(self, seconds):
        status, _, job = self.raw("POST", "/v1/jobs", json.dumps({"task": "test.delay", "input": {"seconds": seconds}}))
        self.assertEqual(status, 202)
        return job

    def wait_running(self, job_id):
        deadline = time.monotonic() + 5
        while self.server.jobs.get(job_id)["status"] != "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_a_running_job_finishes_inside_the_deadline_while_gets_answer_and_submissions_are_refused(self):
        self.start(drain_seconds=10)
        job = self.submit(1)
        self.wait_running(job["id"])
        self.server.stop()
        status, headers, body = self.raw("POST", "/v1/jobs", json.dumps({"task": "test.delay", "input": {}}))
        self.assertEqual((status, body["error"]["code"]), (503, "draining"))
        self.assertEqual((headers["Retry-After"], headers["Connection"]), ("1", "close"))
        self.assertEqual(self.raw("GET", "/healthz")[0], 200)
        status, _, body = self.raw("GET", "/readyz")
        self.assertEqual((status, body), (503, {"status": "not_ready", "reason": "draining"}))
        self.assertEqual(self.raw("GET", "/v1/jobs/" + job["id"])[2]["status"], "running")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "the drain did not end when the job did")
        self.assertEqual(self.server.jobs.get(job["id"])["status"], "succeeded")

    def test_a_queued_job_is_failed_as_service_restarted_at_once(self):
        self.start(drain_seconds=10)
        running = self.submit(1)
        self.wait_running(running["id"])
        queued = self.submit(0)
        self.server.stop()
        _, _, job = self.raw("GET", "/v1/jobs/" + queued["id"])
        self.assertEqual((job["status"], job["error"]["code"]), ("failed", "service_restarted"))

    def test_the_deadline_ends_the_drain_and_the_running_job_as_today(self):
        self.start(drain_seconds=1)
        job = self.submit(10)
        self.wait_running(job["id"])
        started = time.monotonic()
        self.server.stop()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 1.5)
        self.server.server_close()
        self.assertEqual(self.server.jobs.get(job["id"])["status"], "cancelled")

    def test_a_second_signal_ends_the_drain_at_once(self):
        self.start(drain_seconds=300)
        job = self.submit(10)
        self.wait_running(job["id"])
        started = time.monotonic()
        self.server.stop()
        self.server.stop()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)

    def test_the_deadline_counts_from_the_signal_not_from_the_end_of_the_drain_call(self):
        self.start(drain_seconds=1)
        job = self.submit(10)
        self.wait_running(job["id"])
        drain = self.server.jobs.drain
        with patch.object(self.server.jobs, "drain", side_effect=lambda: (time.sleep(0.8), drain())):
            started = time.monotonic()
            self.server.stop()
            self.thread.join(5)
        elapsed = time.monotonic() - started
        self.assertFalse(self.thread.is_alive())
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 1.5)

    def test_a_second_signal_stops_serving_while_the_drain_call_is_still_held(self):
        self.start(drain_seconds=300)
        job = self.submit(10)
        self.wait_running(job["id"])
        held = threading.Event()
        self.addCleanup(held.set)
        drain = self.server.jobs.drain
        with patch.object(self.server.jobs, "drain", side_effect=lambda: (held.wait(5), drain())):
            started = time.monotonic()
            self.server.stop()
            self.server.stop()
            self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)

    def test_a_malformed_body_during_the_drain_is_still_400(self):
        self.start(drain_seconds=10)
        job = self.submit(1)
        self.wait_running(job["id"])
        self.server.stop()
        status, _, body = self.raw("POST", "/v1/jobs", "{")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    def test_a_child_still_loading_holds_neither_gets_nor_the_drain(self):
        with patch.object(jobs, "_serve", _stalled_serve):
            self.start(drain_seconds=1)
            time.sleep(1)  # time for the child to start; it is still loading its models
            # Larger than a pipe holds, so a send to a child that does not read would block.
            status, _, job = self.raw("POST", "/v1/jobs", json.dumps(
                {"task": "test.delay", "input": {"seconds": 0, "value": "a" * 250000}}))
            self.assertEqual(status, 202)
            time.sleep(0.3)
            started = time.monotonic()
            self.assertEqual(self.raw("GET", "/healthz")[0], 200)
            self.assertEqual(self.raw("GET", "/v1/jobs/" + job["id"])[2]["status"], "queued")
            self.assertLess(time.monotonic() - started, 0.5)
            started = time.monotonic()
            self.server.stop()
            self.thread.join(3)
            self.assertFalse(self.thread.is_alive())
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(self.server.jobs.get(job["id"])["error"]["code"], "service_restarted")

    def test_a_drain_of_zero_seconds_stops_at_once(self):
        self.start(drain_seconds=0)
        job = self.submit(10)
        self.wait_running(job["id"])
        self.server.stop()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())


class ConnectionLimitTests(unittest.TestCase):
    """What the server answers before a handler runs: a full connection budget, and a head over its limits."""

    def setUp(self):
        self.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=4))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def get(self, extra=b"", path=b"/healthz"):
        return exchange(self.server.server_address,
                        b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n"
                        + extra + b"\r\n")

    def test_a_full_connection_budget_answers_503_without_reading(self):
        idle = [socket.create_connection(self.server.server_address, timeout=2) for _ in range(32)]
        try:
            deadline = time.monotonic() + 5
            while self.server.slots._value:
                self.assertLess(time.monotonic(), deadline, "the idle connections were not all taken")
                time.sleep(0.01)
            # Nothing sent: the answer comes without the server reading anything.
            status, headers, body = exchange(self.server.server_address)
        finally:
            for connection in idle:
                connection.close()
        self.assertEqual(status, 503)
        self.assertEqual((headers["Retry-After"], headers["Connection"]), ("1", "close"))
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(json.loads(body)["error"]["code"], "service_busy")
        # The slots come back as the idle connections end.
        deadline = time.monotonic() + 5
        while self.get()[0] != 200:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)

    def test_a_header_block_over_16_kib_is_431(self):
        status, headers, body = self.get(b"X-Pad: " + b"a" * 16384 + b"\r\n")
        self.assertEqual(status, 431)
        self.assertEqual(json.loads(body)["error"]["code"], "headers_too_large")
        self.assertEqual(headers["Connection"], "close")
        # Under the standard library's 100 header lines, so the byte budget is what refuses it.
        many = b"".join(b"X-Pad-%d: %s\r\n" % (index, b"a" * 200) for index in range(90))
        self.assertEqual(self.get(many)[0], 431)

    def test_a_header_block_inside_the_limit_is_served(self):
        used = len(b"Host: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n")
        filler = 16384 - used - len(b"X-Pad: \r\n")
        self.assertEqual(self.get(b"X-Pad: " + b"a" * filler + b"\r\n")[0], 200)
        self.assertEqual(self.get(b"X-Pad: " + b"a" * (filler + 1) + b"\r\n")[0], 431)

    def test_carriage_returns_after_the_request_line_count_towards_its_limit(self):
        status, _, body = exchange(self.server.server_address,
                                   b"GET /healthz HTTP/1.1" + b"\r" * 9000 + b"\r\nHost: x\r\nAuthorization: Bearer "
                                   + TOKEN.encode() + b"\r\n\r\n")
        self.assertEqual(status, 431)
        self.assertEqual(json.loads(body)["error"]["code"], "headers_too_large")

    def test_a_request_line_over_8_kib_is_431(self):
        status, _, body = self.get(path=b"/" + b"a" * 8192)
        self.assertEqual(status, 431)
        self.assertEqual(json.loads(body)["error"]["code"], "headers_too_large")
        # One byte shorter it is an ordinary unknown route.
        self.assertEqual(self.get(path=b"/" + b"a" * (8192 - len(b"GET / HTTP/1.1")))[0], 404)
        self.assertEqual(self.get(path=b"/" + b"a" * 70000)[0], 431)


class ReadinessHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=4))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def readyz(self, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request("GET", "/readyz", headers={"Authorization": "Bearer " + TOKEN}
                               if headers is None else headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_the_token_is_required(self):
        self.assertEqual(self.readyz({})[0], 401)
        self.assertEqual(self.readyz({"Authorization": "Bearer wrong"})[0], 401)

    def test_ready_once_a_child_is_idle_and_warm_and_each_reason_otherwise(self):
        deadline = time.monotonic() + 10
        while (reading := self.readyz())[0] != 200:
            status, body = reading
            self.assertEqual((status, body["status"]), (503, "not_ready"))
            self.assertIn(body["reason"], ("no_idle_child", "models_loading"))
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(self.readyz(), (200, {"status": "ready"}))
        for reason in ("no_idle_child", "models_loading"):
            with self.subTest(reason=reason), patch.object(self.server.jobs, "readiness", return_value=reason):
                self.assertEqual(self.readyz(), (503, {"status": "not_ready", "reason": reason}))
        self.server.jobs.drain()
        self.assertEqual(self.readyz(), (503, {"status": "not_ready", "reason": "draining"}))


def _children(pid):
    """The processes whose parent is pid, from /proc."""
    found = []
    for entry in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{entry}/stat") as stat:
                if int(stat.read().rsplit(")", 1)[1].split()[1]) == pid:
                    found.append(int(entry))
        except (OSError, IndexError, ValueError):
            pass
    return found


def _gone(pid):
    """Whether pid has exited; a zombie left to an init that does not reap counts as exited."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


@unittest.skipUnless(os.path.isdir("/proc") and hasattr(signal, "SIGTERM"), "needs POSIX signals and /proc")
class SignalShutdownTests(unittest.TestCase):
    """main() in a process of its own under real signals: it drains, stops its children and closes the job table."""

    def launch(self, drain_seconds):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.database = os.path.join(state.name, "jobs.sqlite")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.address = probe.getsockname()
        environment = {**os.environ, "BRIDGE_TOKEN": TOKEN, "BRIDGE_TEST_TASKS": "1", "BRIDGE_CONCURRENCY": "1",
                       "BRIDGE_STATE_DIR": state.name, "BRIDGE_DRAIN_SECONDS": str(drain_seconds)}
        self.process = subprocess.Popen([sys.executable, "-m", "ai_bridge.server", "--port", str(self.address[1])],
                                        env=environment)
        self.addCleanup(self.process.wait, 5)
        self.addCleanup(lambda: self.process.poll() is None and self.process.kill())
        deadline = time.monotonic() + 10
        while True:
            try:
                if self.request("GET", "/readyz")[0] == 200:
                    break
            except OSError:
                pass
            self.assertLess(time.monotonic(), deadline, "the worker did not become ready")
            time.sleep(0.05)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.address, timeout=2)
        try:
            connection.request(method, path, body, {"Authorization": "Bearer " + TOKEN,
                                                    "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def run_job(self, seconds):
        body = json.dumps({"task": "test.delay", "input": {"seconds": seconds}})
        status, job = self.request("POST", "/v1/jobs", body)
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 5
        while self.request("GET", "/v1/jobs/" + job["id"])[1]["status"] != "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.children = _children(self.process.pid)
        self.assertTrue(self.children)
        return job

    def stopped(self, signals, within):
        """Send the signals, wait for the exit, and check what a clean stop leaves; returns the seconds it took."""
        started = time.monotonic()
        for number in signals:
            self.process.send_signal(number)
            time.sleep(0.1)
        self.assertEqual(self.process.wait(within + 5), 0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, within)
        deadline = time.monotonic() + 5
        while not all(map(_gone, self.children)):
            self.assertLess(time.monotonic(), deadline, "a child outlived the worker")
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.database))
        self.assertFalse(os.path.exists(self.database + "-wal"), "the job table was not closed")
        return elapsed

    def test_a_running_job_completes_and_submissions_meanwhile_are_503_draining(self):
        self.launch(drain_seconds=10)
        job = self.run_job(1)
        self.process.send_signal(signal.SIGTERM)
        status, body = self.request("POST", "/v1/jobs", json.dumps({"task": "test.delay", "input": {}}))
        self.assertEqual((status, body["error"]["code"]), (503, "draining"))
        self.stopped([], within=3)
        with contextlib.closing(sqlite3.connect(self.database)) as database:
            self.assertEqual(database.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone(),
                             ("succeeded",))

    def test_the_deadline_ends_a_running_job(self):
        self.launch(drain_seconds=1)
        self.run_job(10)
        self.assertGreaterEqual(self.stopped([signal.SIGTERM], within=2.5), 1)

    def test_a_drain_of_zero_seconds_stops_at_once(self):
        self.launch(drain_seconds=0)
        self.run_job(10)
        self.stopped([signal.SIGTERM], within=1.5)

    def test_a_second_signal_stops_at_once(self):
        self.launch(drain_seconds=300)
        self.run_job(10)
        self.stopped([signal.SIGTERM, signal.SIGINT], within=2)


class StartupTests(unittest.TestCase):
    """An ONNX worker refuses to start without its model files, naming what is missing, rather than failing every job
    as task_failed: a cache fetched before a model was added holds no directory for it."""

    def models(self, root, complete):
        for name in complete:
            os.makedirs(os.path.join(root, name))
            for file in ("tokenizer.json", "model.onnx"):
                open(os.path.join(root, name, file), "w").close()

    def start(self, backend, root):
        environment = {"BRIDGE_BACKEND": backend, "BRIDGE_MODEL_DIR": root, "BRIDGE_TOKEN": TOKEN}
        with patch.dict(os.environ, environment), patch.object(sys, "argv", ["ai_bridge.server", "--port", "0"]), \
                patch.object(server, "BridgeServer") as bridge, patch.object(server.signal, "signal"):
            server.main()
        return bridge

    def test_missing_models_names_each_directory_without_both_files(self):
        with tempfile.TemporaryDirectory() as root:
            self.models(root, ["rerank", "embed", "redact"])
            os.makedirs(os.path.join(root, "redact-uncased"))
            open(os.path.join(root, "redact-uncased", "tokenizer.json"), "w").close()
            self.assertEqual(tasks.missing_models(root), ["redact-uncased"])
            self.assertEqual(tasks.missing_models(os.path.join(root, "absent")), list(tasks.MODEL_DIRECTORIES))

    def test_an_onnx_worker_refuses_to_start_without_its_models(self):
        with tempfile.TemporaryDirectory() as root:
            self.models(root, ["rerank", "embed", "redact"])
            with self.assertRaises(SystemExit) as raised:
                self.start("onnx", root)
        message = str(raised.exception.code)
        self.assertIn("redact-uncased", message)
        self.assertIn("fetcher", message)
        self.assertNotIn(TOKEN, message)

    def test_an_onnx_worker_with_every_model_starts(self):
        with tempfile.TemporaryDirectory() as root:
            self.models(root, tasks.MODEL_DIRECTORIES)
            bridge = self.start("onnx", root)
        bridge.assert_called_once()

    def test_a_lexical_worker_needs_no_models(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = self.start("lexical", root)
        bridge.assert_called_once()


class EnvironmentTests(unittest.TestCase):
    """server.main builds the store's settings from the BRIDGE_* variables."""

    def main(self, environment, real_server=False):
        """Run main with exactly these BRIDGE_* variables; returns the settings BridgeServer was given."""
        with patch.dict(os.environ, {"BRIDGE_TOKEN": TOKEN, **environment}), \
                patch.object(sys, "argv", ["ai_bridge.server", "--port", "0"]), \
                patch.object(server.signal, "signal"):
            for name in [name for name in os.environ if name.startswith("BRIDGE_")]:
                if name != "BRIDGE_TOKEN" and name not in environment:
                    del os.environ[name]
            if real_server:
                # A store that accepted the settings would serve forever; fail instead, and close it on the way out.
                started = AssertionError("the server started")
                with patch.object(server.BridgeServer, "serve_forever", side_effect=started):
                    server.main()
                return None
            with patch.object(server, "BridgeServer") as bridge:
                server.main()
        self.bridge = bridge
        return bridge.call_args.args[2]

    def test_the_drain_deadline_is_read_and_defaults_to_30_seconds(self):
        self.main({})
        self.assertEqual(self.bridge.call_args.kwargs["drain_seconds"], 30)
        for value in ("0", "300", "45"):
            with self.subTest(value=value):
                self.main({"BRIDGE_DRAIN_SECONDS": value})
                self.assertEqual(self.bridge.call_args.kwargs["drain_seconds"], int(value))

    def test_a_drain_deadline_outside_0_to_300_or_not_an_integer_names_its_variable(self):
        for value in ("-1", "301", "1.5", "soon", ""):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as raised:
                    self.main({"BRIDGE_DRAIN_SECONDS": value})
                self.assertIn("BRIDGE_DRAIN_SECONDS", str(raised.exception.code))

    def test_both_signals_reach_the_server_s_stop(self):
        with patch.dict(os.environ, {"BRIDGE_TOKEN": TOKEN}), \
                patch.object(sys, "argv", ["ai_bridge.server", "--port", "0"]), \
                patch.object(server.signal, "signal") as handlers, patch.object(server, "BridgeServer") as bridge:
            server.main()
        registered = {call.args[0]: call.args[1] for call in handlers.call_args_list}
        self.assertEqual(set(registered), {server.signal.SIGTERM, server.signal.SIGINT})
        running = bridge.return_value.__enter__.return_value
        registered[server.signal.SIGTERM](server.signal.SIGTERM, None)
        registered[server.signal.SIGINT](server.signal.SIGINT, None)
        self.assertEqual(running.stop.call_count, 2)

    def test_defaults_when_nothing_is_set(self):
        self.assertEqual(self.main({}), Settings())

    def test_every_variable_is_read(self):
        with tempfile.TemporaryDirectory() as state_dir:
            settings = self.main({
                "BRIDGE_CONCURRENCY": "3", "BRIDGE_CAPACITY": "9", "BRIDGE_RETENTION_SECONDS": "1.5",
                "BRIDGE_BACKEND": "lexical", "BRIDGE_MODEL_DIR": "/elsewhere", "BRIDGE_TEST_TASKS": "1",
                "BRIDGE_WORKER_MAX_JOBS": "7", "BRIDGE_STATE_DIR": state_dir, "BRIDGE_OWNER_CAPACITY": "4",
            })
            self.assertEqual(settings, Settings(concurrency=3, capacity=9, retention_seconds=1.5, backend="lexical",
                                                model_dir="/elsewhere", test_tasks=True, max_jobs=7,
                                                state_dir=state_dir, owner_capacity=4))

    def test_an_empty_state_dir_is_unset(self):
        self.assertIsNone(self.main({"BRIDGE_STATE_DIR": ""}).state_dir)

    def test_a_state_dir_that_cannot_hold_the_table_names_its_variable(self):
        with tempfile.TemporaryDirectory() as root:
            a_file = os.path.join(root, "file")
            open(a_file, "w").close()
            read_only = os.path.join(root, "read-only")
            os.mkdir(read_only, 0o500)
            paths = [os.path.join(root, "missing"), a_file]
            # Root writes through the mode bits; compose runs these tests as uid 65532.
            if os.geteuid() != 0:
                paths.append(read_only)
            for path in paths:
                with self.subTest(path=path):
                    with self.assertRaises(SystemExit) as raised:
                        self.main({"BRIDGE_STATE_DIR": path}, real_server=True)
                    self.assertIn("BRIDGE_STATE_DIR", str(raised.exception.code))
                    self.assertIn(path, str(raised.exception.code))

    def test_test_tasks_need_exactly_one(self):
        for value in ("true", "yes", "0", ""):
            with self.subTest(value=value):
                self.assertFalse(self.main({"BRIDGE_TEST_TASKS": value}).test_tasks)

    def test_a_malformed_number_names_its_variable(self):
        for name, value, expected in (
                ("BRIDGE_WORKER_MAX_JOBS", "many", "BRIDGE_WORKER_MAX_JOBS must be an integer"),
                ("BRIDGE_WORKER_MAX_JOBS", "1.5", "BRIDGE_WORKER_MAX_JOBS must be an integer"),
                ("BRIDGE_CONCURRENCY", "", "BRIDGE_CONCURRENCY must be an integer"),
                ("BRIDGE_CAPACITY", "1e3", "BRIDGE_CAPACITY must be an integer"),
                ("BRIDGE_RETENTION_SECONDS", "soon", "BRIDGE_RETENTION_SECONDS must be a number")):
            with self.subTest(name=name, value=value):
                with self.assertRaises(SystemExit) as raised:
                    self.main({name: value})
                self.assertEqual(raised.exception.code, expected)

    def test_an_owner_cap_outside_1_to_the_capacity_or_not_an_integer_names_its_variable(self):
        for capacity, value in (("9", "0"), ("9", "10"), (None, "129"), ("9", "-1"), ("9", "many"), ("9", "1.5"),
                                ("9", "")):
            environment = {"BRIDGE_OWNER_CAPACITY": value, **({"BRIDGE_CAPACITY": capacity} if capacity else {})}
            with self.subTest(environment=environment):
                with self.assertRaises(SystemExit) as raised:
                    self.main(environment)
                self.assertIn("BRIDGE_OWNER_CAPACITY", str(raised.exception.code))
        for value in ("1", "9"):
            with self.subTest(value=value):
                self.assertEqual(self.main({"BRIDGE_CAPACITY": "9", "BRIDGE_OWNER_CAPACITY": value}).owner_capacity,
                                 int(value))

    def test_an_out_of_range_job_limit_is_refused_by_the_store(self):
        # The real server: the store checks its limits before it starts any child or binds the port.
        for value in ("0", "1000001", "-1"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid worker limits"):
                self.main({"BRIDGE_WORKER_MAX_JOBS": value}, real_server=True)


SAMPLE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)")
LABEL = r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"'
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|\+Inf")


def parse_metrics(text):
    """A strict reading of the Prometheus text format, version 0.0.4: {family: (type, {(name, labels): value})}.
    Raises ValueError for anything a scraper could misread."""
    if not text.endswith("\n"):
        raise ValueError("the body must end with a newline")
    families, seen, current = {}, set(), None
    for line in text[:-1].split("\n"):
        if line.startswith("# HELP "):
            if len(line.split(" ", 3)) != 4:
                raise ValueError(f"HELP without text: {line!r}")
            continue
        if line.startswith("# TYPE "):
            parts = line.split(" ")
            if len(parts) != 4 or parts[3] not in {"counter", "gauge", "histogram"}:
                raise ValueError(f"malformed TYPE: {line!r}")
            if parts[2] in families:
                raise ValueError(f"a second TYPE for {parts[2]}")
            current = parts[2]
            families[current] = (parts[3], {})
            continue
        match = SAMPLE.fullmatch(line)
        if match is None or current is None:
            raise ValueError(f"not a sample line under a TYPE: {line!r}")
        name, labels_text, value = match.groups()
        kind = families[current][0]
        allowed = {current + suffix for suffix in ("_bucket", "_sum", "_count")} if kind == "histogram" else {current}
        if name not in allowed:
            raise ValueError(f"{name} outside its family {current}")
        labels = {}
        if labels_text is not None:
            if not re.fullmatch(f"{LABEL}(?:,{LABEL})*", labels_text):
                raise ValueError(f"malformed labels: {line!r}")
            for key, label in re.findall(LABEL, labels_text):
                if key in labels:
                    raise ValueError(f"label {key} twice: {line!r}")
                labels[key] = re.sub(r"\\(.)", lambda m: "\n" if m[1] == "n" else m[1], label)
        if not NUMBER.fullmatch(value):
            raise ValueError(f"malformed value: {line!r}")
        key = (name, tuple(sorted(labels.items())))
        if key in seen:
            raise ValueError(f"duplicate series: {line!r}")
        seen.add(key)
        families[current][1][key] = float(value)
    for family, (kind, samples) in families.items():
        if kind == "histogram":
            _check_histogram(family, samples)
    return families


def _check_histogram(family, samples):
    series = {}
    for (name, labels), value in samples.items():
        rest = tuple(item for item in labels if item[0] != "le")
        entry = series.setdefault(rest, {"buckets": [], "sum": None, "count": None})
        if name.endswith("_bucket"):
            bound = dict(labels).get("le")
            if bound is None or not NUMBER.fullmatch(bound):
                raise ValueError(f"a bucket of {family} without a numeric le")
            entry["buckets"].append((float(bound), value))
        else:
            entry[name.rsplit("_", 1)[1]] = value
    for rest, entry in series.items():
        counts = [count for _, count in sorted(entry["buckets"])]
        if not entry["buckets"] or max(entry["buckets"])[0] != float("inf"):
            raise ValueError(f"{family}{rest} has no +Inf bucket")
        if counts != sorted(counts):
            raise ValueError(f"{family}{rest} buckets are not cumulative")
        if entry["sum"] is None or entry["count"] is None or counts[-1] != entry["count"]:
            raise ValueError(f"{family}{rest} +Inf bucket differs from _count, or a total is missing")


def sample(families, name, **labels):
    family = next(family for family in families if name == family or name.startswith(family + "_"))
    return families[family][1][(name, tuple(sorted(labels.items())))]


class MetricsParserTests(unittest.TestCase):
    """The parser the endpoint's tests rely on refuses what a scraper would misread."""

    GOOD = ("# HELP h_seconds Durations.\n# TYPE h_seconds histogram\n"
            'h_seconds_bucket{task="a",le="1"} 1\nh_seconds_bucket{task="a",le="+Inf"} 2\n'
            'h_seconds_sum{task="a"} 3.5\nh_seconds_count{task="a"} 2\n'
            '# TYPE c_total counter\nc_total{x="q\\"\\\\\\n"} 4\n')

    def test_a_well_formed_body_parses(self):
        families = parse_metrics(self.GOOD)
        self.assertEqual(sample(families, "h_seconds_count", task="a"), 2)
        self.assertEqual(sample(families, "c_total", x='q"\\\n'), 4)

    def test_malformed_bodies_are_refused(self):
        for broken in [
                self.GOOD.rstrip("\n"),
                self.GOOD + "# TYPE c_total counter\n",
                self.GOOD + 'c_total{x="q\\"\\\\\\n"} 5\n',
                self.GOOD.replace('le="+Inf"} 2', 'le="9"} 2'),
                self.GOOD.replace('le="1"} 1', 'le="1"} 3'),
                self.GOOD.replace('h_seconds_count{task="a"} 2', 'h_seconds_count{task="a"} 5'),
                self.GOOD.replace('h_seconds_sum{task="a"} 3.5\n', ""),
                self.GOOD.replace("c_total{", "other_total{"),
                "x 1\n", self.GOOD + "c_total 1 2\n", self.GOOD + "c_total NaN\n",
                self.GOOD + 'c_total{x="a",x="b"} 1\n', self.GOOD + "c_total{x=unquoted} 1\n",
                self.GOOD + "# TYPE s summary\n"]:
            with self.subTest(broken=broken[-40:]), self.assertRaises(ValueError):
                parse_metrics(broken)


class MetricsRenderTests(unittest.TestCase):
    def test_label_values_are_escaped(self):
        self.assertEqual(metrics.escape('a\\b"c\nd'), 'a\\\\b\\"c\\nd')
        families = parse_metrics(metrics.gauge("g", "A gauge.", [({"v": 'a\\b"c\nd'}, 1)]))
        self.assertEqual(sample(families, "g", v='a\\b"c\nd'), 1)

    def test_values_are_written_exactly(self):
        self.assertEqual([metrics.number(value) for value in (0, 7, 0.1, 2.5, 1e-07, float("inf"))],
                         ["0", "7", "0.1", "2.5", "1e-07", "+Inf"])

    def test_a_histogram_is_cumulative_with_its_totals(self):
        text = metrics.histogram("h_seconds", "Durations.", [({"task": "a"}, {
            "buckets": [1, 0, 2] + [0] * 8 + [1], "sum": 400.5, "count": 4})])
        families = parse_metrics(text)
        self.assertEqual(sample(families, "h_seconds_bucket", task="a", le="0.05"), 1)
        self.assertEqual(sample(families, "h_seconds_bucket", task="a", le="0.25"), 3)
        self.assertEqual(sample(families, "h_seconds_bucket", task="a", le="300"), 3)
        self.assertEqual(sample(families, "h_seconds_bucket", task="a", le="+Inf"), 4)
        self.assertEqual(sample(families, "h_seconds_sum", task="a"), 400.5)


class MetricsHttpTests(unittest.TestCase):
    """GET /metrics on a store with the test tasks, so failures and timeouts can be produced."""

    @classmethod
    def setUpClass(cls):
        cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(concurrency=1, capacity=16, test_tasks=True))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def raw(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def auth(self):
        return {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}

    def scrape(self):
        status, headers, body = self.raw("GET", "/metrics", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Connection"], "close")
        return body.decode("utf-8"), parse_metrics(body.decode("utf-8"))

    def submit(self, task, value, timeout_ms=30000, key=None):
        body = {"task": task, "input": value, "timeout_ms": timeout_ms}
        if key is not None:
            body["idempotency_key"] = key
        status, _, data = self.raw("POST", "/v1/jobs", json.dumps(body), self.auth())
        self.assertEqual(status, 202, data)
        return json.loads(data)

    def wait(self, job_id, states=("succeeded", "failed", "timed_out", "cancelled"), timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.server.jobs.get(job_id)
            if job["status"] in states:
                return job
            time.sleep(0.01)
        self.fail(f"job did not reach {states}")

    def test_the_token_is_required_exactly_as_on_every_route(self):
        for index, headers in enumerate([{}, {"Authorization": "Bearer wrong"},
                                         {"Authorization": "Bearer " + TOKEN + "x"},
                                         {"Authorization": "Basic " + TOKEN}]):
            with self.subTest(case=index):
                status, _, body = self.raw("GET", "/metrics", headers=headers)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.putrequest("GET", "/metrics")
            connection.putheader("Authorization", "Bearer " + TOKEN)
            connection.putheader("Authorization", "Bearer " + TOKEN)
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 401)
        finally:
            connection.close()

    def test_other_methods_and_spellings_are_unknown_routes(self):
        self.assertEqual(self.raw("POST", "/metrics", "{}", self.auth())[0], 404)
        self.assertEqual(self.raw("POST", "/unknown", "{}", self.auth())[0], 404)
        for path in ["/metrics?x=1", "/metrics/", "/Metrics", "/v1/metrics"]:
            with self.subTest(path=path):
                self.assertEqual(self.raw("GET", path, headers=self.auth())[0], 404)

    def test_the_body_parses_and_names_every_family(self):
        _, families = self.scrape()
        self.assertEqual({family: kind for family, (kind, _) in families.items()}, {
            "bridge_jobs_total": "counter", "bridge_jobs_submitted_total": "counter",
            "bridge_submissions_rejected_total": "counter", "bridge_queue_depth": "gauge",
            "bridge_jobs_running": "gauge", "bridge_pool_size": "gauge", "bridge_pool_ready": "gauge",
            "bridge_concurrency": "gauge", "bridge_capacity": "gauge",
            "bridge_job_duration_seconds": "histogram", "bridge_job_queue_seconds": "histogram",
            "bridge_info": "gauge"})
        self.assertEqual(sample(families, "bridge_info", protocol="1", backend="lexical"), 1)
        self.assertEqual(sample(families, "bridge_concurrency"), 1)
        self.assertEqual(sample(families, "bridge_capacity"), 16)
        self.assertIn(sample(families, "bridge_submissions_rejected_total", owner="default", reason="capacity"), (0, 1, 2))
        bounds = sorted({dict(labels)["le"] for name, labels in families["bridge_job_duration_seconds"][1]
                         if name.endswith("_bucket")}, key=float)
        self.assertEqual(bounds, ["0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "30", "60", "300", "+Inf"])
        statuses = {dict(labels)["status"] for _, labels in families["bridge_jobs_total"][1]}
        self.assertEqual(statuses, {"succeeded", "failed", "timed_out", "cancelled"})
        tasks = {dict(labels)["task"] for _, labels in families["bridge_jobs_total"][1]}
        self.assertEqual(tasks, {"rerank", "embed", "redact", "test.delay", "test.crash", "test.error", "test.inspect"})

    def test_counters_and_histograms_move_with_the_jobs_and_nothing_of_a_job_is_exposed(self):
        _, before = self.scrape()
        marker = "metrics-marker-" + uuid.uuid4().hex
        key = "metrics-key-" + uuid.uuid4().hex
        succeeded = self.submit("test.delay", {"seconds": 0, "value": marker}, key=key)
        failed = self.submit("test.error", {"value": marker})
        ids = [succeeded["id"], failed["id"]]
        for job_id in ids:
            self.wait(job_id)
        cancelled = self.submit("test.delay", {"seconds": 5, "value": marker})
        self.wait(cancelled["id"], {"running"})
        self.raw("POST", "/v1/jobs/" + cancelled["id"] + "/cancel", "{}", self.auth())
        # The cancelled job's child is replaced; the next one starts only on the fresh child.
        deadline = time.monotonic() + 10
        while self.server.jobs.snapshot()["pool_ready"] != 1:
            self.assertLess(time.monotonic(), deadline, "no fresh child")
            time.sleep(0.01)
        timed_out = self.submit("test.delay", {"seconds": 5, "value": marker}, 150)
        ids += [cancelled["id"], timed_out["id"]]
        self.assertEqual(self.wait(timed_out["id"])["status"], "timed_out")
        text, after = self.scrape()

        def moved(name, **labels):
            return sample(after, name, **labels) - sample(before, name, **labels)

        self.assertEqual(moved("bridge_jobs_total", owner="default", task="test.delay", status="succeeded"), 1)
        self.assertEqual(moved("bridge_jobs_total", owner="default", task="test.error", status="failed"), 1)
        self.assertEqual(moved("bridge_jobs_total", owner="default", task="test.delay", status="cancelled"), 1)
        self.assertEqual(moved("bridge_jobs_total", owner="default", task="test.delay", status="timed_out"), 1)
        self.assertEqual(moved("bridge_jobs_submitted_total", owner="default", task="test.delay"), 3)
        self.assertEqual(moved("bridge_jobs_submitted_total", owner="default", task="test.error"), 1)
        self.assertEqual(moved("bridge_job_duration_seconds_count", task="test.delay"), 3)
        self.assertEqual(moved("bridge_job_duration_seconds_count", task="test.error"), 1)
        self.assertGreaterEqual(moved("bridge_job_duration_seconds_sum", task="test.delay"), 0.15)
        self.assertEqual(moved("bridge_job_duration_seconds_bucket", task="test.delay", le="+Inf"), 3)
        self.assertEqual(moved("bridge_job_queue_seconds_count", task="test.delay"), 3)
        for secret in ids + [marker, key, TOKEN, "This internal exception", "task_failed", "deadline_exceeded",
                             self.server.jobs.settings.model_dir]:
            with self.subTest(secret=secret[:16]):
                self.assertNotIn(secret, text)

    def test_a_scrape_while_a_job_runs_answers_at_once(self):
        job = self.submit("test.delay", {"seconds": 2})
        self.wait(job["id"], {"running"})
        started = time.monotonic()
        _, families = self.scrape()
        # Well inside the 2 s request timeout, while the job still runs.
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(sample(families, "bridge_jobs_running"), 1)
        self.assertEqual(sample(families, "bridge_pool_size"), 1)
        self.raw("POST", "/v1/jobs/" + job["id"] + "/cancel", "{}", self.auth())
        self.wait(job["id"])

    def test_the_body_is_formatted_outside_the_store_lock(self):
        held = []
        render = metrics.render

        def checking(snapshot):
            # Another thread can take the lock here, so the scrape held it only while it copied the numbers.
            def probe():
                taken = self.server.jobs._lock.acquire(blocking=False)
                if taken:
                    self.server.jobs._lock.release()
                held.append(not taken)

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            return render(snapshot)

        with patch.object(server, "render", checking):
            self.scrape()
        self.assertEqual(held, [False])



APP = "test-only-app-token-never-use-in-production-1"
OTHER = "test-only-other-token-never-use-in-production-2"


class TokenSourceTests(unittest.TestCase):
    """main() resolves the tokens from BRIDGE_TOKEN, BRIDGE_TOKENS or BRIDGE_TOKENS_FILE, and refuses to start on a bad
    one with a message naming the variable and never a token."""

    def main(self, environment):
        """Run main with exactly these BRIDGE_* variables; returns the tokens BridgeServer was given and the token
        variables still in the environment when it was built."""
        seen = {}

        def built(address, tokens, settings, drain_seconds=30):
            seen["tokens"] = tokens
            seen["left"] = {name for name in ("BRIDGE_TOKEN", "BRIDGE_TOKENS") if name in os.environ}
            return MagicMock()

        with patch.dict(os.environ, environment), patch.object(sys, "argv", ["ai_bridge.server", "--port", "0"]), \
                patch.object(server.signal, "signal"), patch.object(server, "BridgeServer", side_effect=built):
            for name in [name for name in os.environ if name.startswith("BRIDGE_") and name not in environment]:
                del os.environ[name]
            server.main()
        return seen

    def refused(self, environment, variable):
        with self.assertRaises(SystemExit) as raised:
            self.main(environment)
        message = str(raised.exception.code)
        self.assertIn(variable, message)
        for value in (APP, OTHER, TOKEN, APP[:20], "short-token"):
            self.assertNotIn(value, message)
        return message

    def tokens_file(self, root, content, mode=0o600):
        path = os.path.join(root, "tokens")
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
        os.chmod(path, mode)
        return path

    def test_the_single_token_is_named_default_and_leaves_the_environment(self):
        seen = self.main({"BRIDGE_TOKEN": TOKEN})
        self.assertEqual(seen, {"tokens": {"default": TOKEN}, "left": set()})

    def test_the_listed_tokens_are_read_and_leave_the_environment(self):
        seen = self.main({"BRIDGE_TOKENS": f"app={APP},other={OTHER}"})
        self.assertEqual(seen, {"tokens": {"app": APP, "other": OTHER}, "left": set()})
        # A token may hold "=": only the first one separates the name.
        self.assertEqual(self.main({"BRIDGE_TOKENS": f"a.b-c_9={APP}=x"})["tokens"], {"a.b-c_9": APP + "=x"})

    def test_sixty_four_tokens_are_accepted(self):
        listed = ",".join(f"n{index}={APP}{index}" for index in range(64))
        self.assertEqual(len(self.main({"BRIDGE_TOKENS": listed})["tokens"]), 64)

    def test_the_token_file_is_read_with_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as root:
            for mode in (0o600, 0o400):
                with self.subTest(mode=oct(mode)):
                    path = self.tokens_file(root, f"# the applications\n\napp={APP}\n  # indented\nother={OTHER}\n", mode)
                    seen = self.main({"BRIDGE_TOKENS_FILE": path})
                    self.assertEqual(seen["tokens"], {"app": APP, "other": OTHER})
                    os.remove(path)

    def test_a_bad_token_is_refused_for_each_source(self):
        spaced = APP[:20] + " " + APP[20:]
        for value in ("short-token", spaced, APP + "\x7f", APP + "é", ""):
            with self.subTest(value=repr(value)[:24]):
                self.refused({"BRIDGE_TOKENS": f"app={value}"}, "BRIDGE_TOKENS")
                self.refused({"BRIDGE_TOKEN": value or " "}, "BRIDGE_TOKEN")
                with tempfile.TemporaryDirectory() as root:
                    self.refused({"BRIDGE_TOKENS_FILE": self.tokens_file(root, f"app={value}\n")}, "BRIDGE_TOKENS_FILE")
        with tempfile.TemporaryDirectory() as root:
            # A trailing space in the file is part of the line, so the token is refused, not trimmed.
            self.refused({"BRIDGE_TOKENS_FILE": self.tokens_file(root, f"app={APP} \n")}, "BRIDGE_TOKENS_FILE")

    def test_a_bad_name_or_entry_is_refused(self):
        for listed in (f"={APP}", f"a b={APP}", "x" * 65 + f"={APP}", APP, f"café={APP}", f"app={APP},",
                       f"app/1={APP}", f",app={APP}"):
            with self.subTest(listed=listed[:24]):
                self.refused({"BRIDGE_TOKENS": listed}, "BRIDGE_TOKENS")
        self.assertEqual(self.main({"BRIDGE_TOKENS": "x" * 64 + f"={APP}"})["tokens"], {"x" * 64: APP})

    def test_a_duplicate_name_or_value_is_refused(self):
        for listed in (f"app={APP},app={OTHER}", f"app={APP},other={APP}"):
            with self.subTest(listed=listed[:12]):
                self.refused({"BRIDGE_TOKENS": listed}, "BRIDGE_TOKENS")
                with tempfile.TemporaryDirectory() as root:
                    path = self.tokens_file(root, listed.replace(",", "\n"))
                    self.refused({"BRIDGE_TOKENS_FILE": path}, "BRIDGE_TOKENS_FILE")

    def test_more_than_sixty_four_tokens_are_refused(self):
        listed = ",".join(f"n{index}={APP}{index}" for index in range(65))
        self.assertIn("64", self.refused({"BRIDGE_TOKENS": listed}, "BRIDGE_TOKENS"))

    def test_more_than_sixty_four_tokens_in_the_file_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.tokens_file(root, "".join(f"n{index}={APP}{index}\n" for index in range(65)))
            self.assertIn("64", self.refused({"BRIDGE_TOKENS_FILE": path}, "BRIDGE_TOKENS_FILE"))

    def test_a_token_file_over_the_size_limit_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            content = f"app={APP}\n#" + "x" * (server.MAX_TOKENS_FILE - len(APP) - 6) + "\n"
            self.assertEqual(len(content.encode()), server.MAX_TOKENS_FILE + 1)
            message = self.refused({"BRIDGE_TOKENS_FILE": self.tokens_file(root, content)}, "BRIDGE_TOKENS_FILE")
            self.assertIn("65536", message)

    def test_a_fifo_at_the_token_file_path_is_refused_at_once(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("this platform has no FIFOs")
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "tokens")
            os.mkfifo(path, 0o600)
            started = time.monotonic()
            self.assertIn("regular file", self.refused({"BRIDGE_TOKENS_FILE": path}, "BRIDGE_TOKENS_FILE"))
            self.assertLess(time.monotonic() - started, 5)

    def test_an_empty_variable_counts_as_unset(self):
        self.assertEqual(self.main({"BRIDGE_TOKEN": TOKEN, "BRIDGE_TOKENS": ""})["tokens"], {"default": TOKEN})
        with tempfile.TemporaryDirectory() as root:
            path = self.tokens_file(root, f"app={APP}\n")
            seen = self.main({"BRIDGE_TOKENS": "", "BRIDGE_TOKENS_FILE": path})
            self.assertEqual(seen["tokens"], {"app": APP})

    def test_an_empty_list_or_file_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            self.refused({"BRIDGE_TOKENS_FILE": self.tokens_file(root, "# nothing\n\n")}, "BRIDGE_TOKENS_FILE")
        self.refused({}, "BRIDGE_TOKEN")

    def test_a_token_file_open_to_group_or_others_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            for mode in (0o640, 0o604, 0o660, 0o644, 0o602):
                with self.subTest(mode=oct(mode)):
                    path = self.tokens_file(root, f"app={APP}\n", mode)
                    self.assertIn("0600", self.refused({"BRIDGE_TOKENS_FILE": path}, "BRIDGE_TOKENS_FILE"))
                    os.remove(path)

    def test_a_token_file_that_cannot_be_read_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, "missing"), root]
            # Root reads through the mode bits; compose runs these tests as uid 65532.
            if os.geteuid() != 0:
                paths.append(self.tokens_file(root, f"app={APP}\n", 0o000))
            for path in paths:
                with self.subTest(path=path):
                    self.refused({"BRIDGE_TOKENS_FILE": path}, "BRIDGE_TOKENS_FILE")
            binary = os.path.join(root, "binary")
            with open(binary, "wb") as file:
                file.write(b"app=\xff" + APP.encode())
            os.chmod(binary, 0o600)
            self.refused({"BRIDGE_TOKENS_FILE": binary}, "BRIDGE_TOKENS_FILE")

    def test_the_sources_are_exclusive(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.tokens_file(root, f"app={APP}\n")
            for environment, names in (
                    ({"BRIDGE_TOKENS": f"app={APP}", "BRIDGE_TOKENS_FILE": path}, ("BRIDGE_TOKENS", "BRIDGE_TOKENS_FILE")),
                    ({"BRIDGE_TOKEN": TOKEN, "BRIDGE_TOKENS": f"app={APP}"}, ("BRIDGE_TOKEN", "BRIDGE_TOKENS")),
                    ({"BRIDGE_TOKEN": TOKEN, "BRIDGE_TOKENS_FILE": path}, ("BRIDGE_TOKEN", "BRIDGE_TOKENS_FILE"))):
                with self.subTest(names=names):
                    message = self.refused(environment, names[0])
                    self.assertIn(names[1], message)

    def test_the_server_validates_the_tokens_it_is_given(self):
        for tokens in ({}, {"app": "short"}, {"a b": APP}, {"app": APP, "other": APP},
                       {f"n{index}": APP + str(index) for index in range(65)}):
            with self.subTest(tokens=list(tokens)[:2]), self.assertRaises(ValueError) as raised:
                BridgeServer(("127.0.0.1", 0), tokens)
            self.assertNotIn(APP, str(raised.exception))


class OwnerHttpTests(unittest.TestCase):
    """Two tokens on one worker: a job belongs to the token that submitted it."""

    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.start(Settings(concurrency=1, capacity=8, test_tasks=True, state_dir=self.state.name))

    def start(self, settings):
        self.server = BridgeServer(("127.0.0.1", 0), {"app": APP, "other": OTHER}, settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def tearDown(self):
        self.stop()
        self.state.cleanup()

    def call(self, method, path, token, body=None):
        """Status and the decoded body; no answer to any token may hold a token."""
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, None if body is None else json.dumps(body),
                               {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            response = connection.getresponse()
            status, data = response.status, response.read()
        finally:
            connection.close()
        for value in (APP, OTHER):
            self.assertNotIn(value.encode(), data)
        return status, json.loads(data) if data.startswith(b"{") else data.decode()

    def submit(self, token, key=None, seconds=0):
        body = {"task": "test.delay", "input": {"seconds": seconds}}
        if key is not None:
            body["idempotency_key"] = key
        return self.call("POST", "/v1/jobs", token, body)

    def finished(self, job_id, owner):
        deadline = time.monotonic() + 5
        while self.server.jobs.get(job_id, owner)["status"] != "succeeded":
            self.assertLess(time.monotonic(), deadline, "the job never finished")
            time.sleep(0.01)

    def test_another_token_cannot_read_or_cancel_a_job(self):
        status, job = self.submit(APP, seconds=5)
        self.assertEqual(status, 202)
        unknown = self.call("GET", "/v1/jobs/" + "a" * 32, OTHER)
        self.assertEqual(unknown[0], 404)
        # Exactly the answer for an unknown job, so the other token learns nothing of it.
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], OTHER), unknown)
        self.assertEqual(self.call("POST", "/v1/jobs/" + job["id"] + "/cancel", OTHER, {}), unknown)
        status, seen = self.call("GET", "/v1/jobs/" + job["id"], APP)
        self.assertEqual(status, 200)
        self.assertNotEqual(seen["status"], "cancelled")
        self.assertEqual(set(seen), {"id", "task", "status", "result", "error", "progress", "timeout_ms"})
        status, cancelled = self.call("POST", "/v1/jobs/" + job["id"] + "/cancel", APP, {})
        self.assertEqual((status, cancelled["status"]), (200, "cancelled"))

    def test_an_idempotency_key_belongs_to_its_token(self):
        first_app = self.submit(APP, "shared-key")[1]
        first_other = self.submit(OTHER, "shared-key")[1]
        self.assertNotEqual(first_app["id"], first_other["id"])
        self.assertEqual(self.submit(APP, "shared-key")[1]["id"], first_app["id"])
        self.assertEqual(self.submit(OTHER, "shared-key")[1]["id"], first_other["id"])

    def test_health_readiness_and_metrics_answer_every_token(self):
        deadline = time.monotonic() + 5
        while self.call("GET", "/readyz", APP)[0] != 200:
            self.assertLess(time.monotonic(), deadline, "the worker never became ready")
            time.sleep(0.02)
        for token in (APP, OTHER):
            with self.subTest(token=token[:14]):
                self.assertEqual(self.call("GET", "/healthz", token)[0], 200)
                self.assertEqual(self.call("GET", "/readyz", token), (200, {"status": "ready"}))
                self.assertEqual(self.call("GET", "/metrics", token)[0], 200)
        for token in ("wrong", APP + "x", APP[:-1], APP + "," + OTHER):
            for path in ("/healthz", "/readyz", "/metrics"):
                with self.subTest(token=token[:14], path=path):
                    status, body = self.call("GET", path, token)
                    self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))

    def test_the_owner_series_exist_at_zero_and_move_for_the_right_owner(self):
        self.stop()
        self.start(Settings(concurrency=1, capacity=1, test_tasks=True))
        families = parse_metrics(self.call("GET", "/metrics", APP)[1])
        for owner in ("app", "other"):
            self.assertEqual(sample(families, "bridge_jobs_total", owner=owner, task="test.delay", status="succeeded"), 0)
            self.assertEqual(sample(families, "bridge_jobs_submitted_total", owner=owner, task="test.delay"), 0)
            self.assertEqual(sample(families, "bridge_submissions_rejected_total", owner=owner, reason="capacity"), 0)
        owners = {dict(labels)["owner"] for family in ("bridge_jobs_total", "bridge_jobs_submitted_total",
                                                       "bridge_submissions_rejected_total")
                  for _, labels in families[family][1]}
        self.assertEqual(owners, {"app", "other"})
        status, job = self.submit(APP)
        self.assertEqual(status, 202)
        self.finished(job["id"], "app")
        self.assertEqual(self.submit(OTHER)[0], 429)
        families = parse_metrics(self.call("GET", "/metrics", OTHER)[1])
        self.assertEqual(sample(families, "bridge_jobs_total", owner="app", task="test.delay", status="succeeded"), 1)
        self.assertEqual(sample(families, "bridge_jobs_total", owner="other", task="test.delay", status="succeeded"), 0)
        self.assertEqual(sample(families, "bridge_jobs_submitted_total", owner="app", task="test.delay"), 1)
        self.assertEqual(sample(families, "bridge_jobs_submitted_total", owner="other", task="test.delay"), 0)
        self.assertEqual(sample(families, "bridge_submissions_rejected_total", owner="other", reason="capacity"), 1)
        self.assertEqual(sample(families, "bridge_submissions_rejected_total", owner="app", reason="capacity"), 0)
        # The families without an owner label are unchanged.
        self.assertEqual(sample(families, "bridge_job_duration_seconds_count", task="test.delay"), 1)

    def test_a_token_at_its_cap_gets_the_full_store_s_429_and_the_other_still_submits(self):
        self.stop()
        self.start(Settings(concurrency=1, capacity=3, owner_capacity=2, retention_seconds=300, test_tasks=True))

        def submit(token):
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
            try:
                connection.request("POST", "/v1/jobs", json.dumps({"task": "test.delay", "input": {"seconds": 0}}),
                                   {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, response.getheader("Retry-After"), json.loads(response.read())
            finally:
                connection.close()

        for _ in range(2):
            status, _, job = submit(APP)
            self.assertEqual(status, 202)
            self.finished(job["id"], "app")
        status, retry_after, capped = submit(APP)
        self.assertEqual((status, capped["error"]["code"]), (429, "capacity_exceeded"))
        # The capped token's own jobs hold its slots for the 300 s retention.
        self.assertRegex(retry_after, r"\A[1-9][0-9]*\Z")
        self.assertIn(int(retry_after), (299, 300))
        self.assertEqual(submit(OTHER)[0], 202)
        # The store is now full as a whole, and its 429 is the same answer.
        status, retry_after, full = submit(OTHER)
        self.assertEqual(status, 429)
        self.assertIn(int(retry_after), (299, 300))
        self.assertEqual(capped, full)
        families = parse_metrics(self.call("GET", "/metrics", APP)[1])
        self.assertEqual(sample(families, "bridge_submissions_rejected_total", owner="app", reason="capacity"), 1)
        self.assertEqual(sample(families, "bridge_submissions_rejected_total", owner="other", reason="capacity"), 1)

    def test_no_token_value_reaches_the_job_table_and_a_restored_job_keeps_its_owner(self):
        status, job = self.submit(APP, "table-key")
        self.assertEqual(status, 202)
        self.submit(OTHER, "table-key")
        self.finished(job["id"], "app")
        self.stop()
        self.start(Settings(concurrency=1, capacity=8, test_tasks=True, state_dir=self.state.name))
        written = b""
        for name in os.listdir(self.state.name):
            if name.startswith("jobs.sqlite"):
                with open(os.path.join(self.state.name, name), "rb") as file:
                    written += file.read()
        self.assertIn(b"app", written)
        for value in (APP, OTHER):
            self.assertNotIn(value.encode(), written)
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], OTHER)[0], 404)
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], APP)[1]["status"], "succeeded")
        self.assertEqual(self.submit(APP, "table-key")[1]["id"], job["id"])


SENTINEL = "sentinel-7f3a-must-never-be-logged"


def settle(bridge, timeout=2):
    """Until every connection slot is free again: a slot is released after its request line is queued."""
    deadline = time.monotonic() + timeout
    while bridge.slots._value != 32:
        if time.monotonic() > deadline:
            raise AssertionError("a connection was still being handled")
        time.sleep(0.01)


class LoggingHttpTests(unittest.TestCase):
    """A real server on a loopback port with BRIDGE_LOG=json's writer capturing what it would put on stderr."""

    def setUp(self):
        self.stream = io.StringIO()
        log.configure("json", self.stream)
        self.addCleanup(log.close)
        self.server = BridgeServer(("127.0.0.1", 0), {"app": APP, "other": OTHER},
                                   Settings(concurrency=1, capacity=16, test_tasks=True), drain_seconds=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def lines(self):
        """Every line written so far, each checked against its event's schema; logging stops here."""
        settle(self.server)
        log.close()
        self.text = self.stream.getvalue()
        return parse_log(self.text)

    def call(self, method, path, body=None, token=APP, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            sent = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {}),
                    **(headers or {})}
            connection.request(method, path, body, sent)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def ended(self, job_id):
        deadline = time.monotonic() + 5
        while self.server.jobs.get(job_id, "app")["status"] not in jobs.TERMINAL:
            self.assertLess(time.monotonic(), deadline, "the job never ended")
            time.sleep(0.01)

    def test_a_request_line_per_route_with_its_template_and_never_the_path(self):
        job_id = "b" * 32
        calls = [("GET", "/healthz", None, 200), ("GET", "/readyz", None, None), ("GET", "/metrics", None, 200),
                 ("GET", "/v1/jobs/" + job_id, None, 404), ("POST", "/v1/jobs/" + job_id + "/cancel", "{}", 404),
                 ("POST", "/v1/jobs", "{}", 400), ("GET", "/" + SENTINEL, None, 404),
                 ("GET", "/healthz?" + SENTINEL, None, 404), ("GET", "/v1/jobs/" + SENTINEL, None, 404),
                 ("DELETE", "/v1/jobs", None, 501)]
        for method, path, body, status in calls:
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
            try:
                connection.request(method, path, body, {"Authorization": "Bearer " + APP,
                                                        "Content-Type": "application/json"})
                response = connection.getresponse()
                response.read()
                if status is not None:
                    self.assertEqual(response.status, status)
            finally:
                connection.close()
        requests = [line for line in self.lines() if line["event"] == "request"]
        self.assertEqual([(line["method"], line["route"]) for line in requests],
                         [("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/metrics"), ("GET", "/v1/jobs/{id}"),
                          ("POST", "/v1/jobs/{id}/cancel"), ("POST", "/v1/jobs"), ("GET", "other"), ("GET", "other"),
                          ("GET", "other"), ("other", "/v1/jobs")])
        self.assertEqual([line["status"] for line in requests][3:], [404, 404, 400, 404, 404, 404, 501])
        # The unsupported method is refused before authentication, so it has no owner.
        self.assertEqual([line["owner"] for line in requests], ["app"] * 9 + [None])
        self.assertEqual([line["level"] == "warning" for line in requests], [line["status"] >= 500 for line in requests])
        for line in requests:
            self.assertGreaterEqual(line["duration_ms"], 0)
        self.assertNotIn(SENTINEL, self.text)
        self.assertNotIn(job_id, self.text)

    def test_a_401_is_logged_with_no_owner(self):
        self.assertEqual(self.call("GET", "/healthz", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/healthz", token=SENTINEL + "x" * 32)[0], 401)
        lines = self.lines()
        self.assertEqual([(line["status"], line["owner"], line["route"]) for line in lines],
                         [(401, None, "/healthz")] * 2)
        self.assertNotIn(SENTINEL, self.text)

    def test_each_token_s_requests_carry_its_name(self):
        self.call("GET", "/healthz", token=OTHER)
        self.call("GET", "/healthz", token=APP)
        self.assertEqual([line["owner"] for line in self.lines()], ["other", "app"])

    def test_no_input_result_token_header_query_key_or_exception_text_is_written(self):
        body = json.dumps({"task": "test.inspect", "input": {"value": [SENTINEL]}, "idempotency_key": SENTINEL + "-k"})
        headers = {"X-Sentinel": SENTINEL, "Cookie": SENTINEL}
        # A query string is not the route, so this is read as an unknown route's body, not a submission.
        self.assertEqual(self.call("POST", "/v1/jobs?" + SENTINEL, body, headers=headers)[0], 400)
        status, inspected = self.call("POST", "/v1/jobs", body, headers=headers)
        self.assertEqual(status, 202)
        status, failing = self.call("POST", "/v1/jobs", json.dumps({"task": "test.error", "input": {}}))
        self.assertEqual(status, 202)
        status, ranked = self.call("POST", "/v1/jobs", json.dumps(
            {"task": "rerank", "input": {"query": SENTINEL, "documents": [SENTINEL, "other text"]}}))
        self.assertEqual(status, 202)
        for job in (inspected, failing, ranked):
            self.ended(job["id"])
        self.assertEqual(self.call("GET", "/v1/jobs/" + failing["id"])[1]["status"], "failed")
        result = self.call("GET", "/v1/jobs/" + ranked["id"])[1]["result"]
        self.assertEqual(self.call("POST", "/v1/jobs", "{" + SENTINEL)[0], 400)
        lines = self.lines()
        for value in (SENTINEL, APP, OTHER, "Bearer", "internal exception", "Traceback", "RuntimeError",
                      "Task did not complete", "task_failed", result["model"], "test-only", "?"):
            with self.subTest(value=value):
                self.assertNotIn(value, self.text)
        ended = {line["job_id"]: line for line in lines if line["event"] == "job"}
        self.assertEqual({job_id: (line["task"], line["job_status"], line["owner"]) for job_id, line in ended.items()},
                         {inspected["id"]: ("test.inspect", "succeeded", "app"),
                          failing["id"]: ("test.error", "failed", "app"), ranked["id"]: ("rerank", "succeeded", "app")})
        self.assertEqual(ended[failing["id"]]["level"], "warning")
        self.assertEqual(ended[ranked["id"]]["level"], "info")

    def test_refusals_before_and_around_the_handler_are_logged(self):
        # 413 for a body over the limit, 431 for a header block or a request line over its own.
        self.assertEqual(self.call("POST", "/v1/jobs", " " * 262145)[0], 413)
        head = b"GET /healthz HTTP/1.1\r\nHost: x\r\nX-Pad: " + SENTINEL.encode() * 600 + b"\r\n\r\n"
        self.assertEqual(exchange(self.server.server_address, head)[0], 431)
        long_line = b"GET /" + SENTINEL.encode() * 300 + b" HTTP/1.1\r\n\r\n"
        self.assertEqual(exchange(self.server.server_address, long_line)[0], 431)
        # 408: a body announced and never sent, so the five-second read deadline passes.
        self.assertEqual(exchange(self.server.server_address,
                                  b"POST /v1/jobs HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + APP.encode()
                                  + b"\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n", wait=8)[0], 408)
        lines = self.lines()
        self.assertEqual([(line["method"], line["route"], line["status"], line["owner"]) for line in lines],
                         [("POST", "/v1/jobs", 413, "app"), ("GET", "/healthz", 431, None), (None, "other", 431, None),
                          ("POST", "/v1/jobs", 408, "app")])
        self.assertGreaterEqual(lines[3]["duration_ms"], 4000)
        self.assertNotIn(SENTINEL, self.text)

    def test_a_busy_503_is_logged_without_anything_read(self):
        idle = [socket.create_connection(self.server.server_address, timeout=2) for _ in range(32)]
        try:
            deadline = time.monotonic() + 5
            while self.server.slots._value:
                self.assertLess(time.monotonic(), deadline, "the idle connections were not all taken")
                time.sleep(0.01)
            self.assertEqual(exchange(self.server.server_address, b"GET /" + SENTINEL.encode())[0], 503)
        finally:
            for connection in idle:
                connection.close()
        busy = [line for line in self.lines() if line["status"] == 503]
        self.assertEqual([(line["method"], line["route"], line["owner"], line["level"]) for line in busy],
                         [(None, "other", None, "warning")])
        self.assertNotIn(SENTINEL, self.text)

    def test_the_drain_says_how_many_jobs_the_deadline_cut_short(self):
        job = self.call("POST", "/v1/jobs", json.dumps({"task": "test.delay", "input": {"seconds": 5}}))[1]
        deadline = time.monotonic() + 5
        while self.server.jobs.get(job["id"], "app")["status"] != "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.server.stop()
        self.thread.join(5)
        drained = [line for line in self.lines() if line["event"] == "drain"]
        self.assertEqual([(line["running"], line["interrupted"], line["level"]) for line in drained],
                         [(1, False, "warning")])
        self.assertLess(drained[0]["duration_ms"], 2000)

    def test_a_drain_with_nothing_running_is_info(self):
        self.server.stop()
        self.thread.join(5)
        drained = [line for line in self.lines() if line["event"] == "drain"]
        self.assertEqual([(line["running"], line["interrupted"], line["level"]) for line in drained],
                         [(0, False, "info")])

    def test_a_connection_that_times_out_before_its_head_is_read_writes_no_line(self):
        silent = socket.create_connection(self.server.server_address, timeout=10)
        half = socket.create_connection(self.server.server_address, timeout=10)
        try:
            half.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")
            # The handler's five-second idle timeout ends both without a reply.
            for connection in (silent, half):
                self.assertEqual(connection.recv(1), b"")
            settle(self.server, 8)
        finally:
            silent.close()
            half.close()
        self.assertEqual(self.lines(), [])


class PeerResetTests(unittest.TestCase):
    """A peer that resets mid-request puts nothing but JSON lines on stderr, and nothing at all with logging off."""

    def reset_peer(self, mode):
        stream = io.StringIO()
        with patch.object(sys, "stderr", stream):
            log.configure(mode, stream)
            self.addCleanup(log.close)
            bridge = BridgeServer(("127.0.0.1", 0), TOKEN, Settings())
            thread = threading.Thread(target=bridge.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                for head in (b"GET /healthz HTTP/1.1\r\nHost: x\r\n",
                             b"GET /healthz HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n"):
                    connection = socket.create_connection(bridge.server_address, timeout=2)
                    connection.sendall(head)
                    time.sleep(0.1)  # lets the server read what was sent, so the reset lands mid-request
                    # Linger 0: close() sends a reset instead of a FIN.
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    connection.close()
                settle(bridge)
            finally:
                bridge.shutdown()
                thread.join()
                bridge.server_close()
            log.close()
        return stream.getvalue()

    def test_json_writes_json_lines_only(self):
        text = self.reset_peer("json")
        parse_log(text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("127.0.0.1", text)

    def test_off_writes_nothing(self):
        self.assertEqual(self.reset_peer("off"), "")


class BrokenLogStreamHttpTests(unittest.TestCase):
    """A stderr that raises or never drains costs the requests nothing."""

    def serve(self, stream):
        log.configure("json", stream)
        self.addCleanup(log.close)
        bridge = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=64))
        thread = threading.Thread(target=bridge.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(bridge.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(bridge.shutdown)
        started = time.monotonic()
        for _ in range(20):
            connection = http.client.HTTPConnection(*bridge.server_address, timeout=2)
            try:
                connection.request("POST", "/v1/jobs", json.dumps({"task": "embed", "input": {"texts": ["x"]}}),
                                   {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
                self.assertEqual(connection.getresponse().status, 202)
            finally:
                connection.close()
        self.assertLess(time.monotonic() - started, 5)

    def test_a_stderr_that_raises(self):
        self.serve(BrokenStream())

    def test_a_stderr_that_blocks(self):
        stream = BlockedStream()
        self.addCleanup(stream.released.set)
        self.serve(stream)


class LoggingStartTests(unittest.TestCase):
    """main() reads BRIDGE_LOG, writes start once the port is bound and stop once its children are gone."""

    def main(self, environment, serve=None):
        stream = io.StringIO()
        self.addCleanup(log.close)
        with patch.dict(os.environ, environment), patch.object(sys, "argv", ["ai_bridge.server", "--port", "0"]), \
                patch.object(server.signal, "signal"), patch.object(sys, "stderr", stream), \
                patch.object(server.BridgeServer, "serve_forever", serve or MagicMock()):
            for name in [name for name in os.environ if name.startswith("BRIDGE_") and name not in environment]:
                del os.environ[name]
            server.main()
        return stream.getvalue()

    def test_json_writes_start_and_stop_with_safe_settings_only(self):
        text = self.main({"BRIDGE_LOG": "json", "BRIDGE_TOKENS": f"app={APP},{SENTINEL}={OTHER}",
                          "BRIDGE_CAPACITY": "9", "BRIDGE_OWNER_CAPACITY": "3", "BRIDGE_DRAIN_SECONDS": "7"})
        lines = parse_log(text)
        self.assertEqual([line["event"] for line in lines], ["start", "stop"])
        self.assertEqual({key: lines[0][key] for key in log.EVENTS["start"]},
                         {"backend": "lexical", "concurrency": 2, "capacity": 9, "owner_capacity": 3,
                          "retention_seconds": 300.0, "max_jobs": 1000, "drain_seconds": 7, "tokens": 2})
        self.assertEqual((lines[1]["cancelled"], lines[1]["level"]), (0, "info"))
        for value in (APP, OTHER, SENTINEL, "app"):
            self.assertNotIn(value, text)

    def test_without_an_owner_cap_the_start_line_gives_the_capacity(self):
        lines = parse_log(self.main({"BRIDGE_LOG": "json", "BRIDGE_CAPACITY": "9", "BRIDGE_TOKEN": TOKEN}))
        self.assertEqual((lines[0]["capacity"], lines[0]["owner_capacity"]), (9, 9))

    def serve_with_a_running_job(self, signals):
        """A serve_forever that first starts a long job, then signals the server and serves until it stops."""
        serve_forever = server.BridgeServer.serve_forever

        def serve(bridge, poll_interval=0.5):
            job = bridge.jobs.submit("test.delay", {"seconds": 10})
            deadline = time.monotonic() + 10
            while bridge.jobs.get(job["id"])["status"] != "running":
                self.assertLess(time.monotonic(), deadline, "the job never started")
                time.sleep(0.01)
            for _ in range(signals):
                bridge.stop()
            serve_forever(bridge, poll_interval)

        return serve

    def test_a_job_running_at_shutdown_makes_stop_a_warning(self):
        text = self.main({"BRIDGE_LOG": "json", "BRIDGE_TOKEN": TOKEN, "BRIDGE_TEST_TASKS": "1",
                          "BRIDGE_DRAIN_SECONDS": "0"}, self.serve_with_a_running_job(1))
        lines = parse_log(text)
        self.assertEqual([line["event"] for line in lines], ["start", "drain", "job", "stop"])
        self.assertEqual((lines[1]["running"], lines[1]["interrupted"]), (1, False))
        self.assertGreaterEqual(lines[3]["cancelled"], 1)
        self.assertEqual(lines[3]["level"], "warning")

    def test_a_second_signal_writes_one_interrupted_drain_line_before_stop(self):
        snapshot = jobs.JobStore.snapshot

        def slow_snapshot(store):
            # Widens the moment between the interruption and the drain line, which the stop must wait out.
            time.sleep(0.3)
            return snapshot(store)

        with patch.object(jobs.JobStore, "snapshot", slow_snapshot):
            text = self.main({"BRIDGE_LOG": "json", "BRIDGE_TOKEN": TOKEN, "BRIDGE_TEST_TASKS": "1",
                              "BRIDGE_DRAIN_SECONDS": "30"}, self.serve_with_a_running_job(2))
        lines = parse_log(text)
        self.assertEqual([line["event"] for line in lines], ["start", "drain", "job", "stop"])
        self.assertEqual((lines[1]["interrupted"], lines[1]["running"]), (True, 1))
        self.assertEqual((lines[3]["cancelled"], lines[3]["level"]), (1, "warning"))

    def test_off_or_unset_writes_nothing(self):
        for environment in ({}, {"BRIDGE_LOG": "off"}, {"BRIDGE_LOG": ""}):
            with self.subTest(environment=environment):
                self.assertEqual(self.main({"BRIDGE_TOKEN": TOKEN, **environment}), "")

    def test_any_other_value_stops_the_start_naming_the_variable(self):
        for value in ("JSON", "on", "text", SENTINEL):
            with self.subTest(value=value), patch.object(server, "BridgeServer") as bridge:
                with self.assertRaises(SystemExit) as raised:
                    self.main({"BRIDGE_TOKEN": TOKEN, "BRIDGE_LOG": value})
                self.assertIn("BRIDGE_LOG", str(raised.exception.code))
                self.assertNotIn(SENTINEL, str(raised.exception.code))
                bridge.assert_not_called()
