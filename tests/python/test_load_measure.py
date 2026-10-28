"""scripts/load_measure.py drives a worker from concurrent threads and prints throughput and latency percentiles.

These check its settings, percentiles, summaries and table offline; the measurement itself needs a running worker
and is informational, run on demand in the model workflow and never a gate.
"""

import contextlib
import importlib.util
import io
import os
import threading
import time
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("load_measure", REPO / "scripts" / "load_measure.py")
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


class SettingsTests(unittest.TestCase):
    def test_defaults_drive_the_onnx_worker_with_the_mix(self):
        self.assertEqual(load.settings({}), {
            "backend": "onnx", "base": "http://model-worker:8090", "concurrency": [1, 4], "seconds": 120.0,
            "case": "mix", "settle": 300.0})

    def test_a_named_target_a_concurrency_list_and_a_case(self):
        got = load.settings({"LOAD_TARGET": "lexical", "LOAD_CONCURRENCY": "1, 4,16", "LOAD_SECONDS": "2.5",
                             "LOAD_CASE": "redact", "LOAD_SETTLE_SECONDS": "0"})
        self.assertEqual(got, {"backend": "lexical", "base": "http://worker:8090", "concurrency": [1, 4, 16],
                               "seconds": 2.5, "case": "redact", "settle": 0.0})

    def test_a_url_target_names_no_backend(self):
        got = load.settings({"LOAD_TARGET": "http://127.0.0.1:8090"})
        self.assertEqual((got["backend"], got["base"]), (None, "http://127.0.0.1:8090"))

    def test_malformed_settings_stop_with_the_variable_s_name(self):
        for name, value in (("LOAD_TARGET", "gpu"), ("LOAD_TARGET", "ftp://worker"), ("LOAD_TARGET", "http://a b"),
                            ("LOAD_CONCURRENCY", ""), ("LOAD_CONCURRENCY", "1,,4"), ("LOAD_CONCURRENCY", "0"),
                            ("LOAD_CONCURRENCY", "65"), ("LOAD_CONCURRENCY", "two"), ("LOAD_CONCURRENCY", "1.5"),
                            ("LOAD_SECONDS", "0"), ("LOAD_SECONDS", "-1"), ("LOAD_SECONDS", "nan"),
                            ("LOAD_SECONDS", "inf"), ("LOAD_SECONDS", "soon"), ("LOAD_CASE", "classify"),
                            ("LOAD_SETTLE_SECONDS", "-1"), ("LOAD_SETTLE_SECONDS", "nan")):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(SystemExit, name):
                load.settings({name: value})


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_on_known_cases(self):
        ten = list(range(1, 11))
        self.assertEqual([load.percentile(ten, p) for p in (50, 95, 99, 100)], [5, 10, 10, 10])
        hundred = list(range(100, 0, -1))  # unsorted input
        self.assertEqual([load.percentile(hundred, p) for p in (1, 50, 95, 99, 100)], [1, 50, 95, 99, 100])
        self.assertEqual([load.percentile([0.3], p) for p in (50, 99)], [0.3, 0.3])
        self.assertEqual(load.percentile([0.2, 0.1, 0.4, 0.3], 50), 0.2)

    def test_no_values_have_no_percentile(self):
        self.assertIsNone(load.percentile([], 50))


class SummaryTests(unittest.TestCase):
    def test_a_single_case_is_one_row(self):
        records = [("rerank", "ok", 0.1), ("rerank", "ok", 0.3), ("rerank", "429", None), ("rerank", "error", None)]
        self.assertEqual(load.summarise("rerank", 4, 2.0, records),
                         [("rerank", 4, 2.0, 2, 1.0, 0.1, 0.3, 0.3, 0.3, 1, 1)])

    def test_the_mix_has_a_row_overall_and_one_per_task_in_case_order(self):
        records = [("redact", "ok", 0.6), ("rerank", "ok", 0.1), ("embed", "429", None), ("rerank", "ok", 0.2)]
        rows = load.summarise("mix", 1, 4.0, records)
        self.assertEqual([row[0] for row in rows], ["mix", "mix: rerank", "mix: embed", "mix: redact"])
        self.assertEqual(rows[0], ("mix", 1, 4.0, 3, 0.75, 0.2, 0.6, 0.6, 0.6, 1, 0))
        self.assertEqual(rows[2], ("mix: embed", 1, 4.0, 0, 0.0, None, None, None, None, 1, 0))

    def test_the_mix_cycles_tasks_and_threads_start_apart(self):
        self.assertEqual([load.pick("mix", 0, k) for k in range(4)], ["rerank", "embed", "redact", "rerank"])
        self.assertEqual(load.pick("mix", 1, 0), "embed")
        self.assertEqual([load.pick("embed", thread, 5) for thread in range(3)], ["embed"] * 3)

    def test_retry_after_is_read_as_whole_seconds_or_one(self):
        def refused(value):
            headers = {"Retry-After": value} if value is not None else {}
            return HTTPError("http://worker/v1/jobs", 429, "Too Many Requests", headers, None)

        self.assertEqual([load.retry_after(refused(v)) for v in ("7", "1", "123456")], [7, 1, 123456])
        self.assertEqual([load.retry_after(refused(v)) for v in (None, "", "1234567", "-1", "2.5", "Wed")], [1] * 6)


class JobTests(unittest.TestCase):
    """one_job against a fake worker: the clock and the sleeps are patched, so nothing waits."""

    def run_job(self, replies, now=100.0, deadline=102.0):
        records, errors = [], []
        clock = iter([now, now + 0.3, now + 0.3, now + 0.3])
        with mock.patch.object(load, "call", side_effect=replies) as call, \
                mock.patch.object(load.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(load.time, "sleep") as sleep:
            load.one_job("http://worker", "embed", deadline, records, errors)
        return records, errors, call, sleep

    def test_a_succeeded_job_records_its_latency_from_before_the_post(self):
        queued, done = {"id": "a" * 32, "status": "queued"}, {"id": "a" * 32, "status": "succeeded"}
        records, errors, call, sleep = self.run_job([queued, {"id": "a" * 32, "status": "running"}, done])
        self.assertEqual(records, [("embed", "ok", mock.ANY)])
        self.assertAlmostEqual(records[0][2], 0.3)
        self.assertEqual(errors, [])
        self.assertEqual(call.call_args_list[1], mock.call("http://worker", "GET", "/v1/jobs/" + "a" * 32))
        self.assertEqual(sleep.call_args_list, [mock.call(load.POLL_S)] * 2)

    def test_a_429_waits_its_retry_after_within_the_run(self):
        refused = HTTPError("http://worker/v1/jobs", 429, "Too Many Requests", {"Retry-After": "7"}, None)
        records, errors, _, sleep = self.run_job(refused)
        self.assertEqual((records, errors), ([("embed", "429", None)], []))
        sleep.assert_called_once_with(mock.ANY)
        self.assertAlmostEqual(sleep.call_args[0][0], 1.7)  # 2 s left at the start, 0.3 s of it gone

    def test_failures_are_counted_with_a_message_and_a_short_pause(self):
        failed = {"id": "a" * 32, "status": "failed", "error": {"code": "task_failed", "message": "x"}}
        for reply, message in ((failed, "embed: failed task_failed"),
                               (HTTPError("http://worker/v1/jobs", 500, "Server Error", {}, None), "embed: HTTP 500"),
                               (URLError("refused"), "embed: URLError: <urlopen error refused>"),
                               ({"status": "failed", "error": "not an object"}, "embed: AttributeError"),
                               (IncompleteRead(b""), "embed: IncompleteRead"),
                               ([], "embed: TypeError")):
            with self.subTest(message=message):
                records, errors, _, sleep = self.run_job([reply])
                self.assertEqual(records, [("embed", "error", None)])
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith(message), errors[0])
                self.assertAlmostEqual(sleep.call_args[0][0], 1.0)

    def test_a_retry_after_shorter_than_the_run_is_waited_in_full(self):
        refused = HTTPError("http://worker/v1/jobs", 429, "Too Many Requests", {"Retry-After": "1"}, None)
        records, errors, _, sleep = self.run_job(refused, deadline=110.0)
        self.assertEqual((records, errors), ([("embed", "429", None)], []))
        sleep.assert_called_once_with(1)

    def test_a_failed_get_after_a_submit_is_one_error(self):
        records, errors, call, _ = self.run_job([{"id": "a" * 32, "status": "queued"}, URLError("reset")])
        self.assertEqual(records, [("embed", "error", None)])
        self.assertEqual(errors, ["embed: URLError: <urlopen error reset>"])
        self.assertEqual(call.call_count, 2)

    def test_an_unknown_status_is_one_error(self):
        records, errors, call, _ = self.run_job([{"id": "a" * 32, "status": "paused"}])
        self.assertEqual(records, [("embed", "error", None)])
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown status 'paused'", errors[0])
        self.assertEqual(call.call_count, 1)

    def test_a_job_that_never_ends_expires_once_after_its_timeout(self):
        records, errors, ticks = [], [], iter(range(10000))
        queued = {"id": "a" * 32, "status": "running"}
        # Each clock reading is 10 s later: the job is given up after the submitted 120 s plus 5.
        with mock.patch.object(load, "call", return_value=queued) as call,                 mock.patch.object(load.time, "monotonic", side_effect=lambda: 100.0 + 10 * next(ticks)),                 mock.patch.object(load.time, "sleep"):
            load.one_job("http://worker", "embed", 101.0, records, errors)
        self.assertEqual(records, [("embed", "error", None)])
        self.assertEqual(len(errors), 1)
        self.assertIn("TimeoutError", errors[0])
        self.assertLess(call.call_count, 20)

    def test_no_pause_runs_past_the_deadline(self):
        refused = HTTPError("http://worker/v1/jobs", 429, "Too Many Requests", {"Retry-After": "7"}, None)
        _, _, _, sleep = self.run_job(refused, now=100.0, deadline=100.1)
        self.assertEqual(sleep.call_args[0][0], 0.0)


class DriveTests(unittest.TestCase):
    def test_threads_stop_submitting_at_the_deadline_and_every_record_is_kept(self):
        first = {}

        def fake_job(base, task, deadline, records, errors):
            first.setdefault(threading.get_ident(), task)
            records.append((task, "ok", 0.01))
            time.sleep(0.01)

        with mock.patch.object(load, "one_job", side_effect=fake_job) as job:
            elapsed, records, errors = load.drive("http://worker", "mix", 3, 0.1)
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(records), job.call_count)
        self.assertEqual(errors, [])
        # Each thread's first job is a different task, whichever thread the scheduler runs first.
        self.assertEqual(sorted(first.values()), ["embed", "redact", "rerank"])

    def test_a_worker_of_another_backend_stops_the_run(self):
        with mock.patch.dict(os.environ, {"LOAD_TARGET": "onnx"}), \
                mock.patch.object(load, "wait_ready", return_value={"backend": "lexical"}), \
                self.assertRaisesRegex(SystemExit, "reports backend 'lexical'"):
            load.main()


class MainTests(unittest.TestCase):
    def run_main(self, settle):
        order = mock.Mock()
        order.drive.return_value = (1.0, [], [])
        environ = {"LOAD_TARGET": "lexical", "LOAD_CONCURRENCY": "1,4,16", "LOAD_SETTLE_SECONDS": settle}
        with mock.patch.dict(os.environ, environ),                 mock.patch.object(load, "wait_ready", return_value={"backend": "lexical"}),                 mock.patch.object(load, "cpu_model", return_value="cpu"),                 mock.patch.object(load, "drive", order.drive), mock.patch.object(load.time, "sleep", order.sleep),                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            load.main()
        return [(name, args[0] if name == "sleep" else args[2]) for name, args, _ in order.mock_calls]

    def test_the_settle_wait_comes_between_levels_never_before_the_first(self):
        self.assertEqual(self.run_main("7"), [("drive", 1), ("sleep", 7.0), ("drive", 4), ("sleep", 7.0), ("drive", 16)])

    def test_a_zero_settle_never_waits(self):
        self.assertEqual(self.run_main("0"), [("drive", 1), ("drive", 4), ("drive", 16)])


class TableTests(unittest.TestCase):
    def test_the_table_carries_the_header_and_n_a_for_an_empty_row(self):
        text = load.render({"date": "2026-10-22", "commit": "abc1234", "runner": "local", "cpu": "Some CPU",
                            "backend": "lexical", "base": "http://worker:8090", "case": "mix", "seconds": 120.0},
                           [("mix", 4, 120.04, 3000, 24.99, 0.05, 0.1, 0.152, 0.3, 12, 0),
                            ("mix: embed", 4, 120.04, 0, 0.0, None, None, None, None, 0, 1)])
        lines = text.splitlines()
        self.assertEqual(lines[0], "Measured 2026-10-22 UTC, commit `abc1234`, runner: local, CPU: Some CPU.")
        self.assertIn("`lexical` at http://worker:8090", lines[1])
        self.assertIn("worker cpus=1, mem_limit=512m", lines[1])
        self.assertIn("120 s", lines[2])
        self.assertEqual(lines[-4], "| Case | Concurrency | Duration s | Jobs completed | Jobs/s | p50 s | p95 s"
                                    " | p99 s | max s | 429s | Errors |")
        self.assertEqual(lines[-3], "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        self.assertEqual(lines[-2], "| mix | 4 | 120.0 | 3000 | 24.99 | 0.05 | 0.10 | 0.15 | 0.30 | 12 | 0 |")
        self.assertEqual(lines[-1], "| mix: embed | 4 | 120.0 | 0 | 0.00 | n/a | n/a | n/a | n/a | 0 | 1 |")

    def test_the_limits_line_names_the_worker_measured(self):
        header = {"date": "d", "commit": "c", "runner": "r", "cpu": "p", "case": "rerank", "seconds": 1.0}
        onnx = load.render(dict(header, backend="onnx", base="http://model-worker:8090"), [])
        self.assertIn("active capacity 128 jobs, result capacity 384 jobs", onnx)
        self.assertIn("model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1", onnx)
        other = load.render(dict(header, backend=None, base="http://127.0.0.1:8090"), [])
        self.assertIn("not read by this script", other)


if __name__ == "__main__":
    unittest.main()
