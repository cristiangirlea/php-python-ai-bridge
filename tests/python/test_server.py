import http.client
import json
import threading
import unittest

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

    def test_invalid_bodies(self):
        for body in ["[]", "null", "{", '{"task":"rerank","task":"test.crash","input":{}}',
                     '{"task":"rerank","input":{},"extra":1}',
                     '{"task":"test.crash","input":{}}',
                     '{"task":"rerank","input":{"query":NaN,"documents":["x"]}}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"],"top_k":2}}',
                     '{"task":"rerank","input":{"query":"x","documents":["x"],"top_k":null}}',
                     '{"task":"embed","input":{"texts":[]}}',
                     '{"task":"embed","input":{"texts":["x"],"query":"x"}}']:
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
