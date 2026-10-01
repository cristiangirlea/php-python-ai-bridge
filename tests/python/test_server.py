import http.client
import json
import os
import sys
import tempfile
import threading
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
