"""Instance names route jobs without changing ownership or persisted identity."""

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from ai_bridge import log, server
from ai_bridge.jobs import JobStore, Settings
from test_log import capture

TOKEN = "test-only-bridge-token-never-use-in-production"
PAYLOAD = {"query": "capital", "documents": ["Paris", "capital"]}


class InstanceTests(unittest.TestCase):
    def test_generated_ids_and_idempotency(self):
        for instance in (None, "a", "9", "az-09", "a" * 16, "a-"):
            with self.subTest(instance=instance):
                store = JobStore(Settings(instance_id=instance))
                try:
                    first = store.submit("rerank", PAYLOAD, idempotency_key="same")
                    pattern = (re.escape(instance) + "-" if instance is not None else "") + "[a-f0-9]{32}"
                    self.assertRegex(first["id"], "^" + pattern + "$")
                    self.assertEqual(store.submit("rerank", PAYLOAD, idempotency_key="same")["id"], first["id"])
                finally:
                    store.close()

    def test_invalid_instance_names_stop_before_start(self):
        for value in ("", "-a", "A", "a_b", "a.b", "a/", "a" * 17, "a\n", " a", "é"):
            with self.subTest(value=value), patch.dict(os.environ, {"BRIDGE_TOKEN": TOKEN, "BRIDGE_INSTANCE_ID": value}, clear=True), \
                    patch.object(sys, "argv", ["server"]), patch.object(server, "BridgeServer") as built:
                with self.assertRaisesRegex(SystemExit, "BRIDGE_INSTANCE_ID"):
                    server.main()
                built.assert_not_called()

    def test_start_passes_instance_setting_and_unset_stays_legacy(self):
        for value in (None, "a", "9-az"):
            environment = {"BRIDGE_TOKEN": TOKEN}
            if value is not None:
                environment["BRIDGE_INSTANCE_ID"] = value
            with self.subTest(value=value), patch.dict(os.environ, environment, clear=True), \
                    patch.object(sys, "argv", ["server"]), patch.object(server.signal, "signal"), \
                    patch.object(server, "BridgeServer", return_value=MagicMock()) as built:
                server.main()
                self.assertEqual(built.call_args.args[2].instance_id, value)

    def test_store_rejects_invalid_instance_settings(self):
        for value in ("", "-a", "A", "a" * 17, True, 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "BRIDGE_INSTANCE_ID"):
                JobStore(Settings(instance_id=value))

    def test_restart_under_new_name_keeps_ids_results_and_keys(self):
        for instance in (None, "old-name"):
            with self.subTest(instance=instance), tempfile.TemporaryDirectory() as directory:
                store = JobStore(Settings(instance_id=instance, state_dir=directory))
                try:
                    job = store.submit("rerank", PAYLOAD, idempotency_key="retained")
                    deadline = time.monotonic() + 5
                    while store.get(job["id"])["status"] != "succeeded":
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    result = store.get(job["id"])
                finally:
                    store.close()
                store = JobStore(Settings(instance_id="new-name", state_dir=directory))
                try:
                    self.assertEqual(store.get(job["id"]), result)
                    self.assertEqual(store.submit("rerank", PAYLOAD, idempotency_key="retained"), result)
                    self.assertEqual(store.cancel(job["id"]), result)
                    self.assertTrue(store.submit("rerank", PAYLOAD)["id"].startswith("new-name-"))
                finally:
                    store.close()

    def test_log_schema_accepts_only_both_id_forms(self):
        valid = ["a" * 32, "a-" + "f" * 32, "a" * 16 + "-" + "0" * 32, "9-az--" + "a" * 32]
        invalid = ["A-" + "a" * 32, "-a-" + "a" * 32, "a" * 17 + "-" + "a" * 32,
                   "a-" + "A" * 32, "a-" + "a" * 31, "a-" + "a" * 32 + "\n"]
        with capture() as lines:
            for job_id in valid + invalid:
                log.event("job", job_id=job_id)
        self.assertEqual([line["job_id"] for line in lines], valid + [None] * len(invalid))

    def test_http_routes_restored_ids_unknown_prefixes_and_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Settings(instance_id="old", state_dir=directory, test_tasks=True))
            old = store.submit("test.delay", {"seconds": 10})
            store.close()
            service = server.BridgeServer(("127.0.0.1", 0), TOKEN,
                                          Settings(instance_id="a", state_dir=directory, test_tasks=True))
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()

            def request(method, path, body=None):
                connection = http.client.HTTPConnection(*service.server_address, timeout=2)
                try:
                    connection.request(method, path, body, {"Authorization": "Bearer " + TOKEN,
                                                            "Content-Type": "application/json"})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            try:
                with capture() as lines:
                    status, raw = request("POST", "/v1/jobs", json.dumps({"task": "test.delay", "input": {"seconds": 10}}))
                    self.assertEqual(status, 202)
                    job = json.loads(raw)
                    self.assertRegex(job["id"], r"^a-[a-f0-9]{32}$")
                    self.assertEqual(request("GET", "/v1/jobs/" + job["id"])[0], 200)
                    cancelled = request("POST", "/v1/jobs/" + job["id"] + "/cancel", "{}")
                    self.assertEqual((cancelled[0], json.loads(cancelled[1])["status"]), (200, "cancelled"))
                    for job_id in (old["id"],):
                        self.assertEqual(request("GET", "/v1/jobs/" + job_id)[0], 200)
                        self.assertEqual(request("POST", "/v1/jobs/" + job_id + "/cancel", "{}")[0], 200)
                    for method, suffix, body in (("GET", "", None), ("POST", "/cancel", "{}")):
                        missing = request(method, "/v1/jobs/" + "0" * 32 + suffix, body)
                        for job_id in ("b-" + "0" * 32, "a-" + "0" * 32, "A-" + "0" * 32,
                                       "-a-" + "0" * 32, "a" * 17 + "-" + "0" * 32):
                            self.assertEqual(request(method, "/v1/jobs/" + job_id + suffix, body), missing)
                    status, metrics = request("GET", "/metrics")
                    self.assertEqual(status, 200)
                    self.assertIn(b'bridge_jobs_total{owner="default",task="test.delay",status="cancelled"} 1', metrics)
                    self.assertNotIn(job["id"].encode(), metrics)
                self.assertTrue(any(line.get("job_id") == job["id"] for line in lines))
                self.assertTrue(any(line.get("route") == "/v1/jobs/{id}/cancel" for line in lines))
            finally:
                service.shutdown()
                thread.join()
                service.server_close()
