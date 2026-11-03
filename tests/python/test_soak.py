"""scripts/soak.py drives a worker in windows for hours and prints one JSON line per window.

These check its settings, the window summary, the /metrics reading and the window loop offline; the soak itself
needs a running worker and is informational, run on demand and never a gate.
"""

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest import mock
from urllib.error import URLError

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("soak", REPO / "scripts" / "soak.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

METRICS_TEXT = """# HELP bridge_jobs_total Terminal jobs.
# TYPE bridge_jobs_total counter
bridge_jobs_total{task="rerank",status="succeeded"} 10
bridge_jobs_total{task="embed",status="succeeded"} 5
bridge_jobs_total{task="embed",status="failed"} 1
bridge_results_retained 384
bridge_queue_depth 0
bridge_pool_ready 1
bridge_pool_size 1
bridge_job_seconds_bucket{task="embed",le="+Inf"} 6
"""


class SettingsTests(unittest.TestCase):
    def test_defaults_drive_the_onnx_worker_for_eight_hours_in_ten_minute_windows(self):
        self.assertEqual(soak.settings({}), {"base": "http://model-worker:8090", "concurrency": 4,
                                             "seconds": 28800.0, "window": 600.0})

    def test_a_target_a_concurrency_and_times(self):
        got = soak.settings({"SOAK_TARGET": "http://127.0.0.1:8090", "SOAK_CONCURRENCY": "16",
                             "SOAK_SECONDS": "60", "SOAK_WINDOW_SECONDS": "2.5"})
        self.assertEqual(got, {"base": "http://127.0.0.1:8090", "concurrency": 16, "seconds": 60.0, "window": 2.5})

    def test_malformed_settings_stop_with_the_variable_s_name(self):
        for name, value in (("SOAK_TARGET", "model-worker"), ("SOAK_TARGET", "ftp://worker"),
                            ("SOAK_TARGET", "http://a b"), ("SOAK_TARGET", "http://worker/path"),
                            ("SOAK_CONCURRENCY", ""), ("SOAK_CONCURRENCY", "0"), ("SOAK_CONCURRENCY", "65"),
                            ("SOAK_CONCURRENCY", "100"), ("SOAK_CONCURRENCY", "two"), ("SOAK_CONCURRENCY", "1.5"),
                            ("SOAK_SECONDS", "0"), ("SOAK_SECONDS", "nan"), ("SOAK_SECONDS", "inf"),
                            ("SOAK_WINDOW_SECONDS", "-1"), ("SOAK_WINDOW_SECONDS", "soon")):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(SystemExit, name):
                soak.settings({name: value})


class WindowTests(unittest.TestCase):
    def test_a_window_counts_succeeded_jobs_refusals_and_errors_with_per_task_p95(self):
        records = [("rerank", "ok", 0.1), ("embed", "ok", 0.4), ("rerank", "ok", 0.3), ("redact", "429", None),
                   ("embed", "error", None)]
        self.assertEqual(soak.window(records, 2.0), {
            "jobs": 3, "jobs_per_s": 1.5, "p50": 0.3, "p95": 0.4, "max": 0.4, "refused_429": 1, "errors": 1,
            "p95_rerank": 0.3, "p95_embed": 0.4, "p95_redact": None})

    def test_an_empty_window_has_no_latency(self):
        row = soak.window([("embed", "429", None)], 1.0)
        self.assertEqual((row["jobs"], row["p50"], row["max"], row["refused_429"]), (0, None, None, 1))


class MetricsTests(unittest.TestCase):
    def answer(self, text):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = text.encode()
        return mock.patch.object(soak, "urlopen", return_value=response)

    def test_jobs_are_summed_by_status_and_gauges_kept_others_ignored(self):
        with self.answer(METRICS_TEXT) as urlopen:
            got = soak.metrics("http://worker:8090")
        self.assertEqual(got, {"bridge_jobs_total:succeeded": 15.0, "bridge_jobs_total:failed": 1.0,
                               "bridge_results_retained": 384.0, "bridge_queue_depth": 0.0,
                               "bridge_pool_ready": 1.0, "bridge_pool_size": 1.0})
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://worker:8090/metrics")
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))

    def test_an_unreachable_or_broken_worker_is_an_error_entry_not_a_crash(self):
        for failure in (URLError("refused"), ConnectionResetError("reset"), IncompleteRead(b"x", 10)):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(soak, "urlopen", side_effect=failure):
                got = soak.metrics("http://worker:8090")
            self.assertEqual(list(got), ["error"])
            self.assertTrue(got["error"].startswith(type(failure).__name__))

    def test_an_undecodable_answer_is_an_error_entry(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"\xff\xfe"
        with mock.patch.object(soak, "urlopen", return_value=response):
            self.assertIn("UnicodeDecodeError", soak.metrics("http://worker:8090")["error"])


class MainTests(unittest.TestCase):
    def test_windows_run_until_the_time_is_used_and_the_last_one_is_shorter(self):
        clock = [0.0]

        def drive(base, case, concurrency, seconds):
            clock[0] += seconds
            return seconds, [("embed", "ok", 0.2)], []

        out = io.StringIO()
        env = {"SOAK_SECONDS": "25", "SOAK_WINDOW_SECONDS": "10", "SOAK_TARGET": "http://worker:8090"}
        with mock.patch.dict(soak.os.environ, env), \
                mock.patch.object(soak, "wait_ready") as wait_ready, \
                mock.patch.object(soak, "drive", side_effect=drive) as driven, \
                mock.patch.object(soak, "metrics", return_value={"bridge_pool_ready": 1.0}), \
                mock.patch.object(soak.time, "monotonic", side_effect=lambda: clock[0]), \
                contextlib.redirect_stdout(out):
            soak.main()
        wait_ready.assert_called_once_with("http://worker:8090")
        self.assertEqual([c.args for c in driven.call_args_list],
                         [("http://worker:8090", "mix", 4, 10.0), ("http://worker:8090", "mix", 4, 10.0),
                          ("http://worker:8090", "mix", 4, 5.0)])
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([line["window"] for line in lines], [0, 1, 2])
        self.assertEqual(lines[2]["elapsed_s"], 5.0)
        self.assertEqual(lines[0]["worker"], {"bridge_pool_ready": 1.0})


# A docker function, since tmpfs /tmp is noexec in the test container; sh -c's NAME argument keeps $0 the script.
STUB_DOCKER = """docker() {
    printf '%s\n' "$*" >> "$SOAK_TRACE"
    case " $* " in
        *" up "*) return "${STUB_UP:-0}" ;;
        *" run "*) sleep "${STUB_RUN_SECONDS:-0}"; echo '{"window": 0}'; return "${STUB_RUN:-0}" ;;
        *" restart "*) return "${STUB_RESTART:-0}" ;;
        *" inspect "*) echo "restarts=0 oom_killed=false status=running" ;;
    esac
    return 0
}
. "$0"
"""


class ShellTests(unittest.TestCase):
    """scripts/soak.sh with a stub docker: every compose call is traced and nothing starts."""

    TOKEN = "soak-test-token-that-must-never-be-written"

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.trace = self.directory / "trace"
        self.trace.touch()
        self.out = self.directory / "out"

    def environment(self, **extra):
        return {**os.environ, "SOAK_TRACE": str(self.trace), "BRIDGE_TOKEN": self.TOKEN, **extra}

    def start(self, **extra):
        # Stdout goes nowhere: the sampler's sleep outlives the script and would hold a pipe open.
        with (self.directory / "stderr").open("w") as stderr:
            return subprocess.Popen(["sh", "-c", STUB_DOCKER, str(REPO / "scripts" / "soak.sh"), str(self.out)],
                                    env=self.environment(**extra), stdout=subprocess.DEVNULL, stderr=stderr)

    def finish(self, **extra):
        process = self.start(**extra)
        return process.wait(timeout=20), self.trace.read_text().splitlines()

    def events(self):
        return (self.out / "events.log").read_text()

    def assert_cleaned_up(self, commands):
        self.assertTrue(commands[-1].endswith("--profile model down -v --remove-orphans"), commands)
        self.assertIn("worker state: restarts=0 oom_killed=false", self.events())
        self.assertFalse((self.out / "driver.exit").exists())

    def test_a_short_run_loads_restarts_once_and_cleans_up(self):
        status, commands = self.finish(SOAK_SECONDS="2", STUB_RUN_SECONDS="2")
        self.assertEqual(status, 0, (self.directory / "stderr").read_text())
        self.assertEqual(sum(" restart model-worker" in command for command in commands), 1)
        run = next(command for command in commands if " run " in command)
        self.assertIn("-p bridge-soak", run)
        self.assertIn("-T -e SOAK_SECONDS=2 -e SOAK_CONCURRENCY=4 -e SOAK_WINDOW_SECONDS=600 model-load", run)
        self.assertIn("restarted", self.events())
        self.assertIn("driver finished", self.events())
        self.assertEqual((self.out / "soak.jsonl").read_text(), '{"window": 0}\n')
        self.assert_cleaned_up(commands)
        for written in [self.trace, *self.out.iterdir()]:
            self.assertNotIn(self.TOKEN, written.read_text(), written.name)

    def test_malformed_seconds_stop_before_docker_or_the_output_directory(self):
        for value in ("0", "abc", "-5", "2.5", "07"):
            with self.subTest(value=value):
                status, commands = self.finish(SOAK_SECONDS=value)
                self.assertEqual((status, commands), (2, []))
                self.assertFalse(self.out.exists())

    def test_a_failed_start_still_cleans_up_and_fails(self):
        status, commands = self.finish(SOAK_SECONDS="60", STUB_UP="7")
        self.assertEqual(status, 7)
        self.assertFalse(any(" run " in command for command in commands))
        self.assert_cleaned_up(commands)

    def test_a_driver_that_stops_early_skips_the_wait_and_the_restart_and_fails(self):
        started = time.monotonic()
        status, commands = self.finish(SOAK_SECONDS="600", STUB_RUN="3")
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(status, 3)
        self.assertFalse(any(" restart " in command for command in commands))
        self.assertIn("driver exited 3", self.events())
        self.assert_cleaned_up(commands)

    def test_a_failed_restart_fails_the_run(self):
        status, commands = self.finish(SOAK_SECONDS="2", STUB_RUN_SECONDS="2", STUB_RESTART="5")
        self.assertEqual(status, 1)
        self.assertIn("restart failed", self.events())
        self.assert_cleaned_up(commands)

    def test_term_mid_run_takes_the_project_down(self):
        process = self.start(SOAK_SECONDS="600", STUB_RUN_SECONDS="30")
        deadline = time.monotonic() + 10
        while " run " not in self.trace.read_text() and time.monotonic() < deadline:
            time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 143)
        self.assert_cleaned_up(self.trace.read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
