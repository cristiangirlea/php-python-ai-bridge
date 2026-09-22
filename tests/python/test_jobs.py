import time
import unittest
from unittest.mock import Mock, patch

from ai_bridge.jobs import CapacityError, JobStore, Settings, TERMINAL
from ai_bridge.tasks import InvalidInput, validate


class ValidationTests(unittest.TestCase):
    def test_invalid_worker_limits(self):
        for name in ["concurrency", "capacity"]:
            for value in [True, 0, -1, 1.5, "1", 1025]:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    JobStore(Settings(**{name: value}))

    def test_invalid_retention(self):
        for value in [0, -1, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                JobStore(Settings(retention_seconds=value))

    def test_valid_rerank(self):
        payload = {"query": "capital", "documents": ["Paris"]}
        self.assertEqual(validate("rerank", payload), payload)

    def test_invalid_contracts(self):
        for payload in [None, [], {}, {"query": "", "documents": ["x"]},
                        {"query": "x", "documents": []}, {"query": "x", "documents": [1]},
                        {"query": "x", "documents": [""]}, {"query": "x", "documents": ["x"] * 513},
                        {"query": "x", "documents": ["x"], "code": "ignored?"},
                        {"query": "x" * 8193, "documents": ["x"]}]:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(InvalidInput):
                validate("rerank", payload)

    def test_large_document_sets_are_accepted(self):
        payload = {"query": "capital", "documents": ["Paris"] * 512}
        self.assertEqual(validate("rerank", payload), payload)

    def test_total_input_size_is_bounded(self):
        # The 200000 character budget covers the query and every document together.
        accepted = {"query": "x" * 8000, "documents": ["y" * 8000] * 24}
        self.assertEqual(validate("rerank", accepted), accepted)
        for query, documents in [("x", ["y" * 8000] * 25), ("x" * 8001, ["y" * 8000] * 24)]:
            with self.subTest(query=len(query)), self.assertRaises(InvalidInput):
                validate("rerank", {"query": query, "documents": documents})

    def test_top_k_is_optional_and_bounded(self):
        payload = {"query": "capital", "documents": ["Paris", "Rome"], "top_k": 1}
        self.assertEqual(validate("rerank", payload), payload)
        for value in [0, -1, 3, 1.0, True, "1", None]:
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                validate("rerank", {"query": "x", "documents": ["a", "b"], "top_k": value})

    def test_tests_are_disabled_by_default(self):
        with self.assertRaises(InvalidInput):
            validate("test.crash", {})

    def test_no_dynamic_imports(self):
        for task in ["os.system", "__import__", "../../shell", "eval"]:
            with self.subTest(task=task), self.assertRaises(InvalidInput):
                validate(task, {})

    def test_non_finite_test_delay_rejected(self):
        for value in [float("nan"), float("inf"), True, -1, 11, "1"]:
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                validate("test.delay", {"seconds": value}, True)


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(Settings(concurrency=1, test_tasks=True))

    def tearDown(self):
        self.store.close()

    def wait(self, job_id, states=TERMINAL, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get(job_id)
            if job["status"] in states:
                return job
            time.sleep(0.01)
        self.fail(f"job did not reach {states}")

    def test_lexical_rerank_is_explicit_and_stable(self):
        job = self.store.submit("rerank", {"query": "red apple", "documents": ["blue", "apple red", "red"]})
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual([x["index"] for x in done["result"]["rankings"]], [1, 2, 0])
        self.assertEqual(done["result"]["model"], "lexical-demo-not-a-model")
        self.assertEqual(done["progress"], {"completed": 3, "total": 3})

    def test_top_k_truncates_without_disturbing_order(self):
        documents = ["blue", "apple red", "red"]
        full = self.wait(self.store.submit("rerank", {"query": "red apple", "documents": documents})["id"])
        limited = self.wait(self.store.submit(
            "rerank", {"query": "red apple", "documents": documents, "top_k": 2})["id"])
        self.assertEqual(limited["status"], "succeeded")
        self.assertEqual(limited["result"]["rankings"], full["result"]["rankings"][:2])

    def test_large_rerank_bounds_progress_events(self):
        documents = [f"red apple {i}" for i in range(512)]
        done = self.wait(self.store.submit(
            "rerank", {"query": "red apple", "documents": documents, "top_k": 5})["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(len(done["result"]["rankings"]), 5)
        # Progress must still land exactly on the total after throttling.
        self.assertEqual(done["progress"], {"completed": 512, "total": 512})

    def test_input_and_snapshot_are_copied(self):
        payload = {"seconds": 0, "value": {"name": "before"}}
        job = self.store.submit("test.delay", payload)
        payload["value"]["name"] = "after"
        job["status"] = "fake"
        done = self.wait(job["id"])
        self.assertEqual(done["result"]["value"]["name"], "before")
        done["result"]["value"]["name"] = "changed"
        self.assertEqual(self.store.get(job["id"])["result"]["value"]["name"], "before")

    def test_timeout_terminates_process_and_releases_capacity(self):
        job = self.store.submit("test.delay", {"seconds": 5}, 150)
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["error"]["code"], "deadline_exceeded")
        recovery = self.store.submit("test.delay", {"seconds": 0})
        self.assertEqual(self.wait(recovery["id"])["status"], "succeeded")
        self.assertFalse(self.store._active)

    def test_queued_time_counts_towards_deadline(self):
        first = self.store.submit("test.delay", {"seconds": 2})
        self.wait(first["id"], {"running"})
        queued = self.store.submit("test.delay", {"seconds": 0}, 100)
        self.assertEqual(self.wait(queued["id"])["status"], "timed_out")
        self.store.cancel(first["id"])

    def test_cancel_running_and_repeat_cancel(self):
        job = self.store.submit("test.delay", {"seconds": 5})
        self.wait(job["id"], {"running"})
        self.assertEqual(self.store.cancel(job["id"])["status"], "cancelled")
        self.assertEqual(self.store.cancel(job["id"])["status"], "cancelled")
        self.assertFalse(self.store._active)

    def test_cancel_queued_job(self):
        first = self.store.submit("test.delay", {"seconds": 5})
        self.wait(first["id"], {"running"})
        second = self.store.submit("test.delay", {"seconds": 0})
        self.assertEqual(second["status"], "queued")
        self.assertEqual(self.store.cancel(second["id"])["status"], "cancelled")

    def test_cancel_completed_does_not_rewrite_history(self):
        job = self.store.submit("test.delay", {"seconds": 0})
        done = self.wait(job["id"])
        self.assertEqual(self.store.cancel(job["id"]), done)

    def test_crash_is_contained_and_next_job_succeeds(self):
        job = self.store.submit("test.crash", {})
        self.assertEqual(self.wait(job["id"])["error"]["code"], "worker_crashed")
        next_job = self.store.submit("test.delay", {"seconds": 0, "value": "recovered"})
        self.assertEqual(self.wait(next_job["id"])["result"]["value"], "recovered")

    def test_pipe_allocation_failure_does_not_stop_coordination(self):
        real_pipe = self.store._context.Pipe
        calls = 0

        def transient_failure(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(24, "Too many open files")
            return real_pipe(*args, **kwargs)

        with patch.object(self.store._context, "Pipe", side_effect=transient_failure):
            job = self.store.submit("test.delay", {"seconds": 0})
            done = self.wait(job["id"], timeout=1)
            self.assertEqual(done["error"]["code"], "worker_start_failed")
            recovery = self.store.submit("test.delay", {"seconds": 0, "value": "recovered"})
            self.assertEqual(self.wait(recovery["id"])["result"]["value"], "recovered")
            self.assertTrue(self.store._thread.is_alive())

    def test_result_arriving_between_poll_and_exit_check_is_drained(self):
        receive, send, process = Mock(), Mock(), Mock()
        # The first poll misses the result; the child exits before is_alive().
        receive.poll.side_effect = [False, True, False]
        receive.recv.return_value = ("result", {"value": "completed-before-exit"})
        process.is_alive.return_value = False
        with patch.object(self.store._context, "Pipe", return_value=(receive, send)), \
                patch.object(self.store._context, "Process", return_value=process):
            job = self.store.submit("test.delay", {"seconds": 0})
            done = self.wait(job["id"])
            self.assertEqual(done["status"], "succeeded")
            self.assertEqual(done["result"]["value"], "completed-before-exit")
            receive.recv.assert_called_once()
            receive.close.assert_called_once()

    def test_process_initialization_failures_close_allocated_resources(self):
        for stage in ["constructor", "start"]:
            with self.subTest(stage=stage):
                receive, send, process = Mock(), Mock(), Mock()
                factory = Mock(return_value=process)
                if stage == "constructor":
                    factory.side_effect = OSError("process allocation failed")
                else:
                    process.start.side_effect = OSError("process startup failed")
                # A failed cleanup must not prevent cleanup of the other handles.
                send.close.side_effect = OSError("close failed")
                with patch.object(self.store._context, "Pipe", return_value=(receive, send)), \
                        patch.object(self.store._context, "Process", factory):
                    job = self.store.submit("test.delay", {"seconds": 0})
                    self.assertEqual(self.wait(job["id"])["error"]["code"], "worker_start_failed")
                    send.close.assert_called_once()
                    receive.close.assert_called_once()
                    if stage == "start":
                        process.close.assert_called_once()
                recovery = self.store.submit("test.delay", {"seconds": 0})
                self.assertEqual(self.wait(recovery["id"])["status"], "succeeded")

    def test_exception_detail_is_not_exposed(self):
        job = self.store.submit("test.error", {})
        done = self.wait(job["id"])
        self.assertEqual(done["error"]["code"], "task_failed")
        self.assertNotIn("internal exception", str(done))

    def test_missing_model_is_a_task_failure(self):
        self.store.close()
        self.store = JobStore(Settings(backend="onnx", model_dir="/missing-model"))
        job = self.store.submit("rerank", {"query": "x", "documents": ["x"]})
        self.assertEqual(self.wait(job["id"])["error"]["code"], "task_failed")

    def test_retained_results_bound_memory(self):
        self.store.close()
        self.store = JobStore(Settings(capacity=1, retention_seconds=0.15, test_tasks=True))
        job = self.store.submit("test.delay", {"seconds": 0})
        self.wait(job["id"])
        with self.assertRaises(CapacityError):
            self.store.submit("test.delay", {"seconds": 0})
        time.sleep(0.2)
        self.assertIsNone(self.store.get(job["id"]))
        self.store.submit("test.delay", {"seconds": 0})

    def test_unknown_jobs(self):
        self.assertIsNone(self.store.get("a" * 32))
        self.assertIsNone(self.store.cancel("a" * 32))

    def test_deadline_validation(self):
        for value in [True, 99, 300001, 100.0, "100"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.submit("test.delay", {}, value)

    def test_shutdown_cancels_jobs_and_refuses_submission(self):
        job = self.store.submit("test.delay", {"seconds": 5})
        self.store.close()
        self.assertEqual(self.store.get(job["id"])["status"], "cancelled")
        with self.assertRaises(CapacityError):
            self.store.submit("test.delay", {})


if __name__ == "__main__":
    unittest.main()
