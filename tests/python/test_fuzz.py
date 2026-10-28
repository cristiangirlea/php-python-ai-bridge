"""Deterministic raw HTTP mutations against the real loopback listener, without third-party fuzzers."""

import contextlib
import errno
import hashlib
import io
import json
import os
import random
import select
import socket
import threading
import time
import unittest

from ai_bridge import log
from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer, _Head

TOKEN = b"test-only-fuzz-token-never-use-in-production"
SENTINEL = b"FUZZ_PRIVATE_SENTINEL_8e19"
ERRORS = {
    400: ("invalid_request", "Request does not match the task contract"),
    401: ("unauthorized", "Service authentication required"),
    404: ("not_found", "Job or route not found"),
    405: ("method_not_allowed", "Only GET and POST are supported"),
    408: ("request_timeout", "Request body was not received in time"),
    413: ("body_too_large", "Request exceeds 262144 bytes"),
    431: ("headers_too_large", "Request line or header block exceeds its limit"),
    429: ("capacity_exceeded", "Worker capacity reached"),
    503: ("service_busy", "Every connection slot is taken"),
}


def request(method=b"GET", path=b"/healthz", body=None, extra=b""):
    head = method + b" " + path + b" HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN + b"\r\n"
    if body is not None:
        head += b"Content-Type: application/json\r\nContent-Length: %d\r\n" % len(body)
    return head + extra + b"\r\n" + (body or b"")


def corpus():
    job = b"/v1/jobs/" + b"a" * 32
    routes = [(b"GET", path) for path in (b"/healthz", b"/readyz", b"/metrics", job)]
    routes += [(b"POST", b"/v1/jobs"), (b"POST", job + b"/cancel")]
    return [request(method, path, body) for method, path in routes for body in (None, b"{}")]+[
        request(b"POST", b"/v1/jobs", b'{"task":"embed","input":{"texts":["hello"]}}')]


def mutations(pass_number=0):
    if pass_number:
        rng = random.Random(f"108:{pass_number}")
        seeds = corpus()
        for raw in seeds:
            for _ in range(24):
                flipped = bytearray(raw)
                for offset in rng.sample(range(len(raw)), rng.randint(2, 8)):
                    flipped[offset] ^= rng.randrange(1, 256)
                yield bytes(flipped)
                offset = rng.randrange(len(raw) + 1)
                yield raw[:offset] + rng.randbytes(rng.randint(1, 8)) + raw[offset:]
                offset = rng.randrange(len(raw))
                yield raw[:offset] + raw[offset + rng.randint(1, 8):]
                other = rng.choice(seeds)
                yield raw[:rng.randrange(len(raw) + 1)] + other[rng.randrange(len(other) + 1):]
        return
    rng = random.Random(108)
    for raw in corpus():
        yield raw
        # Every byte boundary, including EOF inside the request line, each header and the body.
        for end in range(len(raw)):
            yield raw[:end]
        for _ in range(24):
            offset = rng.randrange(len(raw))
            yield raw[:offset] + bytes([raw[offset] ^ rng.randrange(1, 256)]) + raw[offset + 1:]
        yield raw + request(b"POST", b"/v1/jobs", b'{"task":"embed","input":{"texts":["pipeline"]}}')
        for extra in (b"X-Test: one\r\nX-Test: two\r\n", b"X-Test: x\r\n folded\r\n", b"X-Test:\r\n",
                      b"Empty\r\n", b"X-Test: " + SENTINEL + b"\x00\r\n", b"X-Test: a\rb\r\n",
                      b"X-Test: " + b"a" * 16384 + b"\r\n", b"X: a\r\n" * 101):
            yield raw.replace(b"\r\n\r\n", b"\r\n" + extra + b"\r\n", 1)
        for control in range(32):
            yield raw.replace(b"Host: localhost", b"Host: " + bytes([control]) + SENTINEL, 1)
    for value in (b"-1", b"999999999999999999999", b"oops", b"262145", b"2\r\nContent-Length: 3",
                  b"2\r\nContent-Length: 2"):
        yield request(b"POST", b"/v1/jobs", b"{}").replace(b"Content-Length: 2", b"Content-Length: " + value)
    for name in (b"Transfer-Encoding", b"transfer-encoding", b"TRANSFER-ENCODING", b"tRaNsFeR-eNcOdInG"):
        for value in (b"chunked", b"identity", b""):
            yield request(b"POST", b"/v1/jobs", b"{}", name + b": " + value + b"\r\n")
    for body in (b"\xff\xfe", b"{", b"[" * 2000 + b"0" + b"]" * 2000, b'{"n":' + b"9" * 5000 + b"}",
                 b'{"n":1e9999}', b'{"task":"embed","task":"redact","input":{}}',
                 b'{"input":"' + SENTINEL + b'"}'):
        yield request(b"POST", b"/v1/jobs", body)
    for raw in (b"garbage\r\n\r\n", b"GET /healthz\r\n\r\n", b"GET / HTTP/2.0\r\n\r\n",
                request(b"PATCH"), request(path=b"/" + b"x" * 8192), b"\x00\r\n\r\n"):
        yield raw


class HeadTests(unittest.TestCase):
    def test_later_passes_add_reproducible_distinct_mutations(self):
        fixed = set(mutations())
        first = list(mutations(1))
        second = list(mutations(2))
        self.assertEqual(first, list(mutations(1)))
        self.assertGreater(len(set(first) - fixed), 700)
        self.assertGreater(len(set(second) - fixed - set(first)), 700)

    def test_body_readline_negative_limit_is_unbounded(self):
        for limit in (-1, -10):
            with self.subTest(limit=limit), socket.socket() as connection:
                reader = _Head(io.BufferedReader(io.BytesIO(b"GET / HTTP/1.1\r\n\r\nfirst\nlast")), connection)
                self.assertEqual(reader.readline(), b"GET / HTTP/1.1\r\n")
                self.assertEqual(reader.readline(), b"\r\n")
                self.assertEqual(reader.readline(limit), b"first\n")
                self.assertEqual(reader.readline(limit), b"last")
                self.assertEqual(reader.readline(limit), b"")


class FuzzTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        self.capture = contextlib.redirect_stderr(self.stderr)
        self.capture.__enter__()
        self.server = BridgeServer(("127.0.0.1", 0), TOKEN.decode(), Settings(concurrency=1, capacity=8))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        log.close()
        self.capture.__exit__(None, None, None)
        self.assertNotIn("Traceback", self.stderr.getvalue())
        self.assertNotIn(SENTINEL.decode(), self.stderr.getvalue())

    def exchange(self, raw):
        with socket.create_connection(self.server.server_address, timeout=2) as peer:
            peer.sendall(raw)
            try:
                peer.shutdown(socket.SHUT_WR)
            except OSError as error:
                # An early refusal may close/reset before the sender half-closes; still check its full reply.
                if error.errno != errno.ENOTCONN:
                    raise
            return self.receive(peer)

    def receive(self, peer):
        data = bytearray()
        while True:
            try:
                part = peer.recv(65536)
            except ConnectionResetError:
                break
            if not part:
                break
            data.extend(part)
        return bytes(data)

    def check_response(self, raw, allow_closed=False):
        if not raw:
            self.assertTrue(allow_closed, "unexpected closed connection")
            return
        head, body = raw.split(b"\r\n\r\n", 1)
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        self.assertIn(status, (200, 202, *ERRORS))
        headers = dict(line.lower().split(b": ", 1) for line in head.split(b"\r\n")[1:])
        self.assertEqual(headers[b"connection"], b"close")
        self.assertEqual(int(headers[b"content-length"]), len(body), "a second pipelined reply escaped")
        if status in ERRORS:
            code, message = ERRORS[status]
            if status == 503 and json.loads(body).get("status") == "not_ready":
                self.assertIn(json.loads(body)["reason"], ("models_loading", "no_idle_child"))
            else:
                self.assertEqual(json.loads(body), {"error": {"code": code, "message": message}})
            self.assertEqual(headers[b"content-type"], b"application/json")

    def healthy(self):
        reply = self.exchange(request())
        self.check_response(reply)
        self.assertIn(b" 200 ", reply.split(b"\r\n", 1)[0])

    def test_expect_continue_accepts_a_delayed_body(self):
        body = b'{"task":"embed","input":{"texts":["hello"]}}'
        raw = request(b"POST", b"/v1/jobs", body, b"Expect: 100-continue\r\n")
        with socket.create_connection(self.server.server_address, timeout=5) as peer:
            peer.sendall(raw[:-len(body)])
            time.sleep(1.2)
            peer.sendall(body)
            reply = self.receive(peer)
        self.check_response(reply)
        self.assertIn(b" 202 ", reply.split(b"\r\n", 1)[0])

    def test_http_10_and_tab_and_obs_text_header_are_accepted(self):
        body = b'{"task":"embed","input":{"texts":["hello"]}}'
        raw = request(b"POST", b"/v1/jobs", body, b"X-Test: a\t" + bytes(range(128, 256)) + b"\r\n")
        reply = self.exchange(raw.replace(b"HTTP/1.1", b"HTTP/1.0", 1))
        self.check_response(reply)
        self.assertIn(b" 202 ", reply.split(b"\r\n", 1)[0])

    def test_maximum_body_arriving_in_pieces_within_its_phase_is_accepted(self):
        body = b'{"task":"embed","input":{"texts":["hello"]}}'
        body += b" " * (262144 - len(body))
        raw = request(b"POST", b"/v1/jobs", body)
        with socket.create_connection(self.server.server_address, timeout=5) as peer:
            peer.sendall(raw[:-len(body)])
            started = time.monotonic()
            for index in range(4):
                time.sleep(max(0, started + index - time.monotonic()))
                peer.sendall(body[index * 65536:(index + 1) * 65536])
            reply = self.receive(peer)
        self.check_response(reply)
        self.assertIn(b" 202 ", reply.split(b"\r\n", 1)[0])

    def test_parser_regressions(self):
        for raw, status in ((b"broken\r\n\r\n", 400), (request(b"PATCH"), 405),
                            (b"GET / HTTP/2.0\r\n\r\n", 400), (b"GET /healthz\r\n\r\n", 400),
                            (request(extra=b"X: a\r\n folded\r\n"), 400),
                            (request(extra=b"X: a\x00b\r\n"), 400),
                            (request(extra=b"X: a\rb\r\n"), 400),
                            (request(extra=b"X: a\x7fb\r\n"), 400),
                            (request(extra=b"Empty\r\n"), 400),
                            (request(extra=b"Content-Length: 0\r\nContent-Length: 0\r\n"), 400),
                            (request(extra=b"Content-Length: 0\r\nContent-Length: 1\r\n"), 400),
                            (request(extra=b"Content-Length: -1\r\n"), 400),
                            (request(extra=b"transfer-encoding:\r\n"), 400),
                            (request(b"POST", b"/v1/jobs", b"{}", b"Transfer-Encoding:\r\n"), 400)):
            with self.subTest(raw=raw[:30]):
                reply = self.exchange(raw)
                self.check_response(reply)
                self.assertIn(b" %d " % status, reply.split(b"\r\n", 1)[0])
        before = self.server.jobs.snapshot()["submitted"]
        reply = self.exchange(request() + request(b"POST", b"/v1/jobs", b'{"task":"embed","input":{"texts":["x"]}}'))
        self.check_response(reply)
        self.assertIn(b" 200 ", reply.split(b"\r\n", 1)[0])
        self.assertEqual(self.server.jobs.snapshot()["submitted"], before, "the pipelined job must never be submitted")
        self.healthy()

    def test_seeded_mutations(self):
        seconds = float(os.environ.get("BRIDGE_FUZZ_SECONDS", "30"))
        self.assertTrue(0 < seconds <= 14400, "BRIDGE_FUZZ_SECONDS must be in (0, 14400]")
        started = time.monotonic()
        count = 0
        distinct = set()
        next_pass = 1
        for mode in ("off", "json"):
            log.configure(mode)
            deadline = time.monotonic() + seconds / 2
            pass_number = 0
            while True:
                for index, raw in enumerate(mutations(pass_number)):
                    with self.subTest(mode=mode, seed=108, pass_number=pass_number, mutation=index):
                        self.check_response(self.exchange(raw), allow_closed=not raw or not raw.split(b"\n", 1)[0].strip())
                    count += 1
                    distinct.add(hashlib.sha256(raw).digest())
                    if count % 64 == 0:
                        self.healthy()
                self.healthy()
                if time.monotonic() >= deadline:
                    break
                pass_number = next_pass
                next_pass += 1
            log.close()
        lines = [json.loads(line) for line in self.stderr.getvalue().splitlines()]
        self.assertTrue(any(line.get("event") == "request" for line in lines), "JSON mode logged no requests")
        self.assertNotIn(SENTINEL.decode(), self.stderr.getvalue())
        print(f"FUZZ: {count} mutations, {len(distinct)} distinct inputs in {time.monotonic() - started:.3f}s (seed 108)", flush=True)

    def test_slow_headers_and_bodies_release_all_32_slots(self):
        # Parallel sockets keep both five-second phases bounded without shortening the production timeout.
        for body in (False, True):
            with contextlib.ExitStack() as stack:
                peers = [stack.enter_context(socket.create_connection(self.server.server_address, timeout=2))
                         for _ in range(32)]
                started = time.monotonic()
                for peer in peers:
                    peer.sendall(request(b"POST", b"/v1/jobs", b"x" * 100)[:-100] if body else b"GET /healthz HTTP/1.1\r\nX: ")
                deadline = started + 2
                while self.server.slots._value:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertIn(b" 503 ", self.exchange(b"").split(b"\r\n", 1)[0])
                pending = set(peers)
                for tick in range(1, 8):
                    time.sleep(max(0, started + tick - time.monotonic()))
                    for peer in list(pending):
                        if select.select([peer], [], [], 0)[0]:
                            reply = self.receive(peer)
                            self.check_response(reply, allow_closed=not body)
                            if body:
                                self.assertIn(b" 408 ", reply)
                            pending.remove(peer)
                        else:
                            peer.sendall(b"x")
                    if not pending:
                        break
                self.assertFalse(pending, "one byte per second kept a connection past its five-second read budget")
                self.assertLess(time.monotonic() - started, 7.5)
                deadline = time.monotonic() + 1
                while self.server.slots._value != 32:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.healthy()
