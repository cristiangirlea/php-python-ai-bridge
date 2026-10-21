import contextlib
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from ai_bridge import jobs, metrics, server, tasks
from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer

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
        self.assertNotIn("http-k3", self.server.jobs._keys)

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
                              "bridge_submissions_rejected_total", reason="capacity")
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
                "BRIDGE_WORKER_MAX_JOBS": "7", "BRIDGE_STATE_DIR": state_dir,
            })
            self.assertEqual(settings, Settings(concurrency=3, capacity=9, retention_seconds=1.5, backend="lexical",
                                                model_dir="/elsewhere", test_tasks=True, max_jobs=7,
                                                state_dir=state_dir))

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
        self.assertIn(sample(families, "bridge_submissions_rejected_total", reason="capacity"), (0, 1, 2))
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

        self.assertEqual(moved("bridge_jobs_total", task="test.delay", status="succeeded"), 1)
        self.assertEqual(moved("bridge_jobs_total", task="test.error", status="failed"), 1)
        self.assertEqual(moved("bridge_jobs_total", task="test.delay", status="cancelled"), 1)
        self.assertEqual(moved("bridge_jobs_total", task="test.delay", status="timed_out"), 1)
        self.assertEqual(moved("bridge_jobs_submitted_total", task="test.delay"), 3)
        self.assertEqual(moved("bridge_jobs_submitted_total", task="test.error"), 1)
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
