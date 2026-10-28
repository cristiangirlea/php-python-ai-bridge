import contextlib
import io
import json
import logging
import re
import threading
import time
import unittest

from ai_bridge import log, server

TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


@contextlib.contextmanager
def capture():
    """JSON logging into a buffer for the duration; the list it yields fills with the parsed lines once the block ends,
    when the writer has written everything and logging is off again."""
    stream = io.StringIO()
    lines = []
    log.configure("json", stream)
    try:
        yield lines
    finally:
        log.close()
        # The raw text too, for searches through everything written.
        capture.text = stream.getvalue()
        lines.extend(parse(capture.text))


def parse(text):
    """Every line as JSON, each holding exactly the keys of its event; fails on anything else."""
    parsed = []
    for line in text.splitlines():
        record = json.loads(line)
        assert isinstance(record, dict), line
        assert set(record) == {"ts", "level", "event", *log.EVENTS[record["event"]]}, line
        assert TIMESTAMP.fullmatch(record["ts"]), line
        assert record["level"] in ("info", "warning"), line
        parsed.append(record)
    return parsed


class BlockedStream:
    """A stderr whose writes wait until released, as a full pipe nobody reads would."""

    def __init__(self):
        self.released = threading.Event()
        self.written = []

    def write(self, text):
        self.released.wait()
        self.written.append(text)

    def flush(self):
        pass


class BrokenStream:
    def write(self, text):
        raise OSError("stderr is gone")

    def flush(self):
        raise OSError("stderr is gone")


class FormatterTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.close)

    def test_each_event_writes_exactly_its_keys_with_null_for_a_missing_one(self):
        with capture() as lines:
            log.event("request", method="GET", route="/healthz", status=200, duration_ms=1.5, owner="default")
            log.event("job", job_id="a" * 32, task="rerank", owner="default", job_status="succeeded")
            log.event("start", backend="lexical", concurrency=2, capacity=128, owner_capacity=64,
                      retention_seconds=300.0, max_jobs=1000, drain_seconds=30, tokens=1)
            log.event("drain", duration_ms=0.0, running=0, interrupted=False)
            log.event("stop", cancelled=0)
        self.assertEqual([line["event"] for line in lines], ["request", "job", "start", "drain", "stop"])
        self.assertEqual(lines[0], {**lines[0], "method": "GET", "route": "/healthz", "status": 200,
                                    "duration_ms": 1.5, "owner": "default", "level": "info"})
        self.assertEqual((lines[1]["queue_ms"], lines[1]["run_ms"]), (None, None))
        self.assertEqual((lines[2]["owner_capacity"], lines[2]["tokens"]), (64, 1))

    def test_the_schema_is_the_documented_one(self):
        # Written out here, not read from log.EVENTS, so a change to the schema has to change this test too.
        expected = {
            "request": ["method", "route", "status", "duration_ms", "owner"],
            "job": ["job_id", "task", "owner", "job_status", "queue_ms", "run_ms"],
            "start": ["backend", "concurrency", "capacity", "result_capacity", "owner_capacity", "retention_seconds", "max_jobs",
                      "drain_seconds", "tokens"],
            "stop": ["cancelled"],
            "drain": ["duration_ms", "running", "interrupted"],
        }
        with capture() as lines:
            for name in expected:
                log.event(name)
        self.assertEqual({line["event"]: list(line) for line in lines},
                         {name: ["ts", "level", "event", *keys] for name, keys in expected.items()})

    def test_a_key_outside_the_event_s_schema_is_dropped(self):
        with capture() as lines:
            log.event("request", method="GET", route="/healthz", status=200, duration_ms=1, owner=None,
                      path="/healthz?token=sentinel", input="sentinel", error="sentinel", job_id="a" * 32)
            log.event("stop", cancelled=0, message="sentinel")
        self.assertNotIn("sentinel", capture.text)
        self.assertNotIn("job_id", lines[0])

    def test_a_value_outside_its_fixed_set_or_type_is_null(self):
        with capture() as lines:
            log.event("request", method="sentinel", route="/healthz?sentinel", status="200", duration_ms=float("nan"),
                      owner="sentinel token with spaces")
            log.event("request", method="GET", route="/v1/jobs", status=True, duration_ms=-1, owner="x" * 65)
            log.event("job", job_id="sentinel" * 4, task="sentinel", owner=["default"], job_status="sentinel",
                      queue_ms="1", run_ms=float("inf"))
            log.event("start", backend="sentinel", concurrency=1.5, capacity=-1, owner_capacity="1",
                      retention_seconds="300", max_jobs=True, drain_seconds=None, tokens="2")
            log.event("drain", duration_ms="1", running=-1, interrupted=1)
        self.assertNotIn("sentinel", capture.text)
        # A route that is not a template and a method outside the fixed set are null rather than echoed.
        self.assertEqual({key: lines[1][key] for key in log.EVENTS["request"]},
                         {"method": "GET", "route": "/v1/jobs", "status": None, "duration_ms": None, "owner": None})
        for line in [lines[0]] + lines[2:]:
            with self.subTest(event=line["event"]):
                self.assertEqual({line[key] for key in log.EVENTS[line["event"]]}, {None})

    def test_an_unknown_event_or_a_plain_message_writes_nothing(self):
        with capture() as lines:
            log.event("sentinel", status=200)
            logging.getLogger("ai_bridge").warning("sentinel %s", "argument")
            logging.getLogger("ai_bridge.jobs").error("sentinel", exc_info=RuntimeError("sentinel"))
            try:
                raise RuntimeError("sentinel")
            except RuntimeError:
                logging.getLogger("ai_bridge").exception("sentinel")
        self.assertEqual(capture.text, "")
        self.assertEqual(lines, [])

    def test_the_message_and_exception_of_an_event_never_reach_the_line(self):
        with capture() as lines:
            logging.getLogger("ai_bridge").error("sentinel", exc_info=RuntimeError("sentinel"), stack_info=True,
                                                 extra={"bridge": ("stop", {"cancelled": 1})})
        self.assertNotIn("sentinel", capture.text)
        self.assertNotIn("Traceback", capture.text)
        self.assertEqual(lines[0]["level"], "warning")

    def test_the_level_is_info_or_warning(self):
        with capture() as lines:
            log.event("stop", cancelled=0)
            log.event("stop", warning=True, cancelled=1)
        self.assertEqual([line["level"] for line in lines], ["info", "warning"])

    def test_the_timestamp_is_utc_with_milliseconds(self):
        before = time.time()
        with capture() as lines:
            log.event("stop", cancelled=0)
        stamp = time.mktime(time.strptime(lines[0]["ts"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        self.assertLess(abs(stamp - before), 2)

    def test_the_owner_pattern_is_the_token_name_pattern(self):
        self.assertEqual(log._NAME.pattern, server.TOKEN_NAME.pattern)


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.close)

    def test_off_writes_nothing(self):
        stream = io.StringIO()
        log.configure("off", stream)
        log.event("stop", cancelled=0)
        log.close()
        self.assertEqual(stream.getvalue(), "")

    def test_nothing_is_written_before_configure_or_after_close(self):
        stream = io.StringIO()
        log.event("stop", cancelled=0)
        log.configure("json", stream)
        log.close()
        log.event("stop", cancelled=0)
        self.assertEqual(stream.getvalue(), "")

    def test_an_unknown_mode_is_refused(self):
        for mode in ("JSON", "on", "text", "1"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                log.configure(mode, io.StringIO())

    def test_configure_twice_keeps_one_writer(self):
        first, second = io.StringIO(), io.StringIO()
        log.configure("json", first)
        log.configure("json", second)
        log.event("stop", cancelled=0)
        log.close()
        self.assertEqual((first.getvalue(), len(second.getvalue().splitlines())), ("", 1))


class BrokenStderrTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.close)

    def test_a_stream_that_raises_is_swallowed(self):
        log.configure("json", BrokenStream())
        log.event("stop", cancelled=0)
        log.close()

    def test_a_stream_that_blocks_never_holds_the_caller(self):
        stream = BlockedStream()
        log.configure("json", stream)
        started = time.monotonic()
        for _ in range(5000):
            log.event("stop", cancelled=0)
        self.assertLess(time.monotonic() - started, 5)
        # A writer stuck on the stream does not hold close() for long either.
        started = time.monotonic()
        log.close()
        self.assertLess(time.monotonic() - started, 3)
        stream.released.set()

    def test_a_writer_left_with_a_full_queue_ends_once_its_stream_is_released(self):
        stream = BlockedStream()
        self.addCleanup(stream.released.set)
        log.configure("json", stream)
        _, lines, writer, _ = log._active
        # One line held by the blocked write and a full queue behind it, so close() cannot queue its end.
        log.event("stop", cancelled=0)
        deadline = time.monotonic() + 5
        while lines.qsize():
            self.assertLess(time.monotonic(), deadline, "the writer never took the first line")
            time.sleep(0.01)
        for _ in range(log.QUEUE_LINES + 1):
            log.event("stop", cancelled=0)
        log.close()
        stream.released.set()
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(len(stream.written), log.QUEUE_LINES + 1)


if __name__ == "__main__":
    unittest.main()
