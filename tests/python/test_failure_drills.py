"""Failure-drill verdicts against fakes; Docker is exercised by the shell driver."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("failure_drills", ROOT / "scripts/failure_drills.py")
drills = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drills)


class Peer:
    def __init__(self, replies):
        self.replies = iter(replies)

    def request(self, *args):
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(status=200, body=None, headers=None):
    return status, headers or {}, body or {}


def job(status, code=None, job_id="a" * 32):
    return {"id": job_id, "status": status, "error": {"code": code} if code else None}


def logs(*events):
    return "\n".join(json.dumps(event) for event in events)


class FailureDrillTests(unittest.TestCase):
    def run_driver(self, failure, cleanup_failure=False):
        # Shell functions fake the Docker and child-shell boundaries even on the noexec test tmpfs.
        wrapper = r'''
docker() {
    printf '%s\n' "$*" >> "$DRIVER_LOG"
    if [ "$FAIL_CLEANUP" = 1 ]; then
        case "$*" in *' rm '*) return 1 ;; esac
    fi
    return 0
}
sh() {
    [ "$2" = --one ] || return 99
    if [ "$3" = "$FAIL_DRILL" ]; then echo "injected refusal"; return 1; fi
    return 0
}
. "$0"
'''
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "commands"
            result = subprocess.run(["/bin/sh", "-c", wrapper, str(ROOT / "scripts/failure_drills.sh")],
                                    env={**os.environ, "BRIDGE_TOKEN": "test-only-drill-token-never-use-in-production",
                                         "FAIL_DRILL": failure, "FAIL_CLEANUP": str(int(cleanup_failure)),
                                         "DRIVER_LOG": str(log)}, capture_output=True, text=True, timeout=10)
            result.stderr += log.read_text(encoding="utf-8")
            return result

    def test_driver_resets_containers_and_volume_before_first_up(self):
        result = self.run_driver("")
        commands = result.stderr.splitlines()
        reset = next(i for i, line in enumerate(commands) if "rm --stop --force -v" in line)
        volume = commands.index("volume rm bridge-drills_drill-load-state")
        up = next(i for i, line in enumerate(commands) if "up -d" in line)
        self.assertLess(reset, volume)
        self.assertLess(volume, up)

    def test_cleanup_failures_do_not_change_verdict_or_skip_volume_removal(self):
        for failure, code in (("", 0), ("child-killed-mid-job", 1)):
            with self.subTest(failure=failure):
                result = self.run_driver(failure, cleanup_failure=True)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stderr.count("volume rm bridge-drills_drill-load-state"), 2)

    def test_driver_prints_seven_passes_one_skip_and_exits_zero(self):
        result = self.run_driver("")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len([line for line in lines if line.startswith("PASS ")]), 7)
        self.assertEqual(len([line for line in lines if line.startswith("SKIP start-crash-loop: ")]), 1)
        self.assertEqual(len(lines), 8)

    def test_driver_reports_failure_continues_and_exits_nonzero(self):
        result = self.run_driver("child-killed-mid-job")
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "FAIL child-killed-mid-job: injected refusal")
        self.assertIn("PASS sigterm-under-load", lines)
        self.assertEqual(len(lines), 8)

    def test_missing_model_requires_exit_and_documented_message(self):
        text = "/empty-models lacks the model files for rerank, embed, redact; acquire them with the fetcher: docker compose"
        drills.check_startup("missing-model", 1, text)
        for code, output in ((0, text), (1, "import failed"), (125, text), (1, "lacks the model files")):
            with self.subTest(code=code, output=output), self.assertRaises(drills.DrillFailure):
                drills.check_startup("missing-model", code, output)

    def test_open_token_file_requires_specific_permission_refusal(self):
        text = "BRIDGE_TOKENS_FILE must not be open to group or others: give it mode 0600 or stricter"
        drills.check_startup("open-token-file", 1, text)
        for code, output in ((0, text), (1, "BRIDGE_TOKEN cannot be set together with BRIDGE_TOKENS_FILE"),
                             (1, "BRIDGE_TOKENS_FILE cannot be read by the service")):
            with self.subTest(code=code, output=output), self.assertRaises(drills.DrillFailure):
                drills.check_startup("open-token-file", code, output)

    def test_child_crash_requires_failed_and_worker_crashed(self):
        drills.check_crashed(job("failed", "worker_crashed"))
        for state, code in (("succeeded", None), ("failed", "task_failed"), ("running", "worker_crashed")):
            with self.subTest(state=state), self.assertRaises(drills.DrillFailure):
                drills.check_crashed(job(state, code))

    def test_full_state_requires_capacity_error_retry_and_live_reads(self):
        peer = Peer([reply(429, {"error": {"code": "capacity_exceeded"}}, {"retry-after": "1"}),
                     reply(body=job("succeeded")), reply(body={"status": "ok"})])
        drills.check_state(peer, "a" * 32)

    def test_full_state_rejects_wrong_status_code_delay_or_broken_reads(self):
        good = reply(429, {"error": {"code": "capacity_exceeded"}}, {"retry-after": "1"})
        for bad in (reply(202), reply(429, {"error": {"code": "other"}}, {"retry-after": "1"}),
                    reply(429, {"error": {"code": "capacity_exceeded"}}),
                    reply(429, {"error": {"code": "capacity_exceeded"}}, {"retry-after": "0"}),
                    reply(429, {"error": {"code": "capacity_exceeded"}}, {"retry-after": "2"}),
                    reply(429, {"error": {"code": "capacity_exceeded"}}, {"retry-after": "01"}),
                    reply(429, {"error": {"code": "capacity_exceeded"}}, {"retry-after": "tomorrow"})):
            with self.subTest(bad=bad), self.assertRaises(drills.DrillFailure):
                drills.check_state(Peer([bad]), "a" * 32)
        for read in (reply(404), reply(body=job("failed")), drills.Unavailable()):
            with self.subTest(read=read), self.assertRaises((drills.DrillFailure, drills.Unavailable)):
                drills.check_state(Peer([good, read]), "a" * 32)

    def test_drain_requires_ordered_lines_and_exact_counts(self):
        drain = {"event": "drain", "running": 1, "interrupted": False}
        stop = {"event": "stop", "cancelled": 1}
        drills.check_drain(logs(drain, stop), 1)
        for events in ((stop, drain), (drain,), (stop,), (drain, drain, stop),
                       (dict(drain, running=0), stop), (drain, dict(stop, cancelled=0)),
                       (dict(drain, interrupted=True), stop)):
            with self.subTest(events=events), self.assertRaises(drills.DrillFailure):
                drills.check_drain(logs(*events), 1)

    def test_poll_records_terminals_and_unreachable_without_inventing_success(self):
        ids = ["a" * 32, "b" * 32]
        peer = Peer([reply(body=job("failed", "service_restarted")), drills.Unavailable()])
        self.assertEqual(drills.poll_stopping(peer, ids), {ids[0]: job("failed", "service_restarted")})

    def test_poll_rejects_missing_jobs_and_unexpected_failures(self):
        for response in (reply(404), reply(body=job("failed", "task_failed")),
                         reply(body=job("alien")), reply(body=job("succeeded", job_id="b" * 32))):
            with self.subTest(response=response), self.assertRaises(drills.DrillFailure):
                drills.poll_stopping(Peer([response]), ["a" * 32])

    def test_poll_times_out_if_worker_never_stops(self):
        with mock.patch.object(drills.time, "monotonic", side_effect=[0, 0, 16]), \
                mock.patch.object(drills.time, "sleep"), self.assertRaises(drills.DrillFailure):
            drills.poll_stopping(Peer([reply(body=job("running"))]), ["a" * 32])

    def test_restart_covers_every_accepted_job(self):
        ids = ["a" * 32, "b" * 32]
        peer = Peer([reply(body=job("failed", "service_restarted", value)) for value in ids])
        drills.check_recovered(peer, ids, {})

    def test_restart_preserves_results_and_recovers_shutdown_cancellation(self):
        ids = ["a" * 32, "b" * 32]
        old = {ids[0]: job("succeeded"), ids[1]: job("cancelled", job_id=ids[1])}
        peer = Peer([reply(body=old[ids[0]]), reply(body=job("failed", "service_restarted", ids[1]))])
        drills.check_recovered(peer, ids, old)

    def test_restart_rejects_lost_nonterminal_and_changed_terminal(self):
        for response in (reply(404), reply(body=job("running")), reply(body=job("failed", "task_failed")),
                         reply(body=job("failed", "service_restarted", "b" * 32))):
            with self.subTest(response=response), self.assertRaises(drills.DrillFailure):
                drills.check_recovered(Peer([response]), ["a" * 32], {})
        with self.assertRaises(drills.DrillFailure):
            drills.check_recovered(Peer([reply(body=job("failed", "service_restarted"))]),
                                   ["a" * 32], {"a" * 32: job("succeeded")})

    def test_flood_checks_the_busy_response_and_recovery(self):
        busy = b'HTTP/1.0 503 Service Unavailable\r\nRetry-After: 1\r\n\r\n{"error":{"code":"service_busy"}}'
        drills.check_busy(busy)
        for bad in (b"", busy.replace(b"503", b"200"), busy.replace(b"service_busy", b"draining"),
                    busy.replace(b"Retry-After: 1", b"Retry-After: 0")):
            with self.subTest(bad=bad), self.assertRaises(drills.DrillFailure):
                drills.check_busy(bad)

    def test_flood_closes_all_sockets_on_failure(self):
        sockets = [mock.MagicMock() for _ in range(33)]
        sockets[-1].recv.side_effect = [b"bad response", b""]
        with mock.patch.object(drills.socket, "create_connection", side_effect=sockets), \
                mock.patch.object(drills.select, "select", return_value=([], [], [])), \
                mock.patch.object(drills.time, "sleep"), self.assertRaises(drills.DrillFailure):
            drills.flood_once(Peer([]), "http://drill-flood:8090")
        for connection in sockets:
            connection.close.assert_called_once()

    def test_flood_success_closes_connections_and_retries_until_recovered(self):
        sockets = [mock.MagicMock() for _ in range(33)]
        sockets[-1].recv.side_effect = [
            b'HTTP/1.0 503 Service Unavailable\r\nRetry-After: 1\r\n\r\n{"error":{"code":"service_busy"}}', b""]
        peer = mock.Mock()
        peer.request.side_effect = [reply(503), reply(200)]
        with mock.patch.object(drills.socket, "create_connection", side_effect=sockets) as connect, \
                mock.patch.object(drills.select, "select", return_value=([], [], [])) as select, \
                mock.patch.object(drills.time, "sleep"):
            drills.flood(peer, "http://drill-flood:8090")
        self.assertEqual(connect.call_count, 33)
        self.assertEqual(peer.request.call_count, 2)
        select.assert_called_once_with(sockets[:32], [], [], 0)
        for connection in sockets:
            connection.close.assert_called_once()

    def test_flood_rejects_an_answered_or_closed_initial_socket(self):
        sockets = [mock.MagicMock() for _ in range(32)]
        with mock.patch.object(drills.socket, "create_connection", side_effect=sockets) as connect, \
                mock.patch.object(drills.select, "select", return_value=([sockets[0]], [], [])), \
                mock.patch.object(drills.time, "sleep"), \
                mock.patch.object(drills.time, "monotonic", side_effect=[0, 0, 16]), \
                self.assertRaises(drills.DrillFailure):
            drills.flood(Peer([]), "http://drill-flood:8090")
        self.assertEqual(connect.call_count, 32)
        for connection in sockets:
            connection.close.assert_called_once()

    def test_flood_retries_the_whole_attempt_after_a_readiness_slot_race(self):
        with mock.patch.object(drills, "flood_once", side_effect=[drills.DrillFailure("slot held"), None]) as attempt, \
                mock.patch.object(drills.time, "sleep"):
            drills.flood(Peer([]), "http://drill-flood:8090")
        self.assertEqual(attempt.call_count, 2)

    def test_wait_ready_handles_warmup_and_connection_loss(self):
        peer = Peer([drills.Unavailable(), reply(503), reply(200, {"status": "ready"})])
        with mock.patch.object(drills.time, "sleep"):
            drills.wait_ready(peer)

    def test_wait_ready_is_bounded(self):
        with mock.patch.object(drills.time, "monotonic", side_effect=[0, 16]), \
                self.assertRaises(drills.DrillFailure):
            drills.wait_ready(Peer([]))

    def test_command_failure_exits_nonzero_without_exception_details(self):
        output = io.StringIO()
        with mock.patch.object(drills, "dispatch", side_effect=OSError("private payload")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(drills.main(["child-check"]), 1)
        self.assertNotIn("private payload", output.getvalue())
        self.assertIn("transport or filesystem operation failed", output.getvalue())

    def test_command_success_exits_zero_and_verdict_failure_exits_one(self):
        output = io.StringIO()
        with mock.patch.object(drills, "dispatch"), contextlib.redirect_stdout(output):
            self.assertEqual(drills.main(["child-check"]), 0)
        self.assertEqual(output.getvalue(), "")
        with mock.patch.object(drills, "dispatch", side_effect=drills.DrillFailure("child was not replaced")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(drills.main(["child-check"]), 1)
        self.assertEqual(output.getvalue(), "child was not replaced\n")


if __name__ == "__main__":
    unittest.main()
