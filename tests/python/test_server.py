import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_bridge import server, tasks
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
                     '{"task":"embed","input":{"texts":[]}}',
                     '{"task":"embed","input":{"texts":["x"],"query":"x"}}',
                     '{"task":"redact","input":{"text":""}}',
                     '{"task":"redact","input":{"text":"x","entities":["MISC"]}}']:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/v1/jobs", body, self.auth())[0], 400)

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

    def test_a_shutting_down_store_says_one_second(self):
        self.server.jobs.close()
        status, retry_after, _ = self.submit()
        self.assertEqual((status, retry_after), (429, "1"))


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
        return bridge.call_args.args[2]

    def test_defaults_when_nothing_is_set(self):
        self.assertEqual(self.main({}), Settings())

    def test_every_variable_is_read(self):
        settings = self.main({
            "BRIDGE_CONCURRENCY": "3", "BRIDGE_CAPACITY": "9", "BRIDGE_RETENTION_SECONDS": "1.5",
            "BRIDGE_BACKEND": "lexical", "BRIDGE_MODEL_DIR": "/elsewhere", "BRIDGE_TEST_TASKS": "1",
            "BRIDGE_WORKER_MAX_JOBS": "7",
        })
        self.assertEqual(settings, Settings(concurrency=3, capacity=9, retention_seconds=1.5, backend="lexical",
                                            model_dir="/elsewhere", test_tasks=True, max_jobs=7))

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
