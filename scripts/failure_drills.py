"""Bounded, standard-library probes for failure_drills.sh's disposable workers."""

import argparse
import errno
import json
import os
import re
import select
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path

TERMINAL = frozenset(("succeeded", "failed", "cancelled", "timed_out"))
BOUND = 15
STATE = Path("/tmp/failure-drill.json")


class DrillFailure(Exception):
    """A fixed diagnostic safe for the driver's verdict line."""


class Unavailable(Exception):
    """The worker stopped answering; never expose a URL or credential."""


def require(condition, why):
    if not condition:
        raise DrillFailure(why)


class Peer:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, body=None):
        request = urllib.request.Request(
            self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"], "Content-Type": "application/json"})
        try:
            try:
                response = self.opener.open(request, timeout=1)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read(1048577)
                require(len(raw) <= 1048576, "response exceeds probe limit")
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise DrillFailure("worker returned invalid JSON") from None
                require(isinstance(value, dict), "worker returned a non-object")
                return response.code, {key.lower(): val for key, val in response.headers.items()}, value
        except (OSError, HTTPException) as error:
            raise Unavailable from error


def eventually(action, accept, why, retry=(Unavailable,)):
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline:
        try:
            value = action()
            if accept(value):
                return value
        except retry:
            pass
        time.sleep(0.05)
    raise DrillFailure(why)


def wait_ready(peer):
    eventually(lambda: peer.request("GET", "/readyz"),
               lambda response: response[0] == 200 and response[2].get("status") == "ready",
               "readiness did not recover within 15 seconds")


def get_job(peer, job_id):
    status, _, value = peer.request("GET", "/v1/jobs/" + job_id)
    require(status == 200 and value.get("id") == job_id, "accepted job missing or ID changed")
    require(value.get("status") in TERMINAL | {"queued", "running"}, "unknown job status")
    return value


def wait_job(peer, job_id, state):
    return eventually(lambda: get_job(peer, job_id), lambda value: value["status"] in state,
                      "job did not reach expected state within 15 seconds")


def submit(peer, seconds=10):
    status, _, value = peer.request("POST", "/v1/jobs", {
        "task": "test.delay", "input": {"seconds": seconds}, "timeout_ms": 30000})
    require(status == 202 and re.fullmatch(r"[a-f0-9]{32}", value.get("id", "")), "submission was not accepted")
    return value["id"]


def pool_pids():
    pids = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Exclude the server, this probe and multiprocessing's resource tracker.
        if b"multiprocessing.spawn" in command and b"--multiprocessing-fork" in command:
            pids.append(int(path.parent.name))
    return pids


def check_crashed(value):
    require(value.get("status") == "failed" and (value.get("error") or {}).get("code") == "worker_crashed",
            "killed child did not end its job as worker_crashed")


def check_startup(kind, exit_code, output):
    require(exit_code == 1, "worker did not refuse startup with exit 1")
    if kind == "missing-model":
        require("/empty-models lacks the model files for " in output and "acquire them with the fetcher:" in output,
                "missing-model startup diagnostic absent")
    else:
        require("BRIDGE_TOKENS_FILE must not be open to group or others: give it mode 0600 or stricter" in output,
                "open token file permission diagnostic absent")


def check_state(peer, job_id):
    status, headers, value = peer.request("POST", "/v1/jobs", {
        "task": "test.delay", "input": {"seconds": 0.01}, "timeout_ms": 30000})
    require(status == 429 and value.get("error", {}).get("code") == "capacity_exceeded",
            "unwritable state did not reject submission with 429 capacity_exceeded")
    require(headers.get("retry-after") == "1", "unwritable state requires Retry-After: 1")
    require(get_job(peer, job_id)["status"] == "succeeded", "retained result unavailable with full state")
    require(peer.request("GET", "/healthz")[0] == 200, "health read failed with full state")


def fill_state():
    # Exhaust actual tmpfs pages, rather than merely creating a sparse file.
    with Path("/state/filler").open("wb", buffering=0) as target:
        try:
            while True:
                target.write(b"x" * 4096)
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise


def events(output):
    result = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def check_drain(output, running):
    lines = [event for event in events(output) if event.get("event") in ("drain", "stop")]
    require([event["event"] for event in lines] == ["drain", "stop"], "missing, repeated or unordered drain/stop lines")
    require(lines[0].get("running") == running and lines[0].get("interrupted") is False,
            "drain did not reach its deadline with expected running jobs")
    require(lines[1].get("cancelled") == running, "stop did not cancel every remaining running job")


def check_terminal(value):
    require(value["status"] in TERMINAL, "accepted job remained nonterminal")
    require(value["status"] in ("succeeded", "cancelled") or
            (value["status"] == "failed" and (value.get("error") or {}).get("code") == "service_restarted"),
            "job ended outside the documented stop behaviour")


def poll_stopping(peer, ids):
    observed = {}
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline:
        for job_id in ids:
            if job_id in observed:
                continue
            try:
                value = get_job(peer, job_id)
            except Unavailable:
                return observed
            if value["status"] in TERMINAL:
                check_terminal(value)
                observed[job_id] = value
        if len(observed) == len(ids):
            return observed
        time.sleep(0.05)
    raise DrillFailure("worker neither stopped nor completed accepted jobs within 15 seconds")


def check_recovered(peer, ids, observed):
    require(len(ids) == len(set(ids)), "accepted duplicate job IDs")
    for job_id in ids:
        value = get_job(peer, job_id)
        check_terminal(value)
        old = observed.get(job_id)
        if old and old["status"] != "cancelled":
            require(value == old, "a retained terminal result changed after restart")
        else:
            require(value["status"] == "failed" and value.get("error", {}).get("code") == "service_restarted",
                    "unfinished job not recovered as service_restarted")


def check_busy(raw):
    head, separator, body = raw.partition(b"\r\n\r\n")
    require(separator and head.startswith(b"HTTP/1.0 503 "), "33rd connection did not receive 503")
    require(b"\r\nretry-after: 1\r\n" in b"\r\n" + head.lower() + b"\r\n", "busy response lacks Retry-After: 1")
    try:
        value = json.loads(body)
    except ValueError:
        raise DrillFailure("busy response is not JSON") from None
    require(isinstance(value, dict) and value.get("error", {}).get("code") == "service_busy",
            "33rd connection did not receive service_busy")


def flood_once(peer, base):
    url = urllib.parse.urlsplit(base)
    connections = []
    try:
        for _ in range(32):
            connection = socket.create_connection((url.hostname, url.port or 80), timeout=1)
            connections.append(connection)
            # Incomplete headers hold a slot, well inside the server's five-second idle timeout.
            connection.sendall(b"GET /healthz HTTP/1.0\r\n")
        time.sleep(0.1)
        readable, _, _ = select.select(connections[:], [], [], 0)
        require(not readable, "one of the first 32 connections was answered or closed")
        extra = socket.create_connection((url.hostname, url.port or 80), timeout=1)
        connections.append(extra)
        raw = b""
        while chunk := extra.recv(4096):
            raw += chunk
            require(len(raw) <= 8192, "busy response exceeds probe limit")
        check_busy(raw)
    finally:
        for connection in connections:
            connection.close()
    eventually(lambda: peer.request("GET", "/healthz"), lambda response: response[0] == 200,
               "normal request failed after idle connections closed")


def flood(peer, base):
    # A readiness request may still hold a slot when its response reaches the client.
    eventually(lambda: flood_once(peer, base), lambda _: True,
               "connection limit or recovery did not match within 15 seconds",
               retry=(Unavailable, DrillFailure, OSError, HTTPException))


def dispatch(args):
    peer = Peer(args.url)
    command = args.command
    if command == "startup":
        check_startup(args.kind, args.exit_code, sys.stdin.read())
    elif command == "ready":
        wait_ready(peer)
    elif command == "child-prepare":
        wait_ready(peer)
        job_id = submit(peer)
        wait_job(peer, job_id, {"running"})
        pids = pool_pids()
        require(len(pids) == 1, "expected exactly one spawn child in /proc")
        STATE.write_text(json.dumps({"id": job_id, "pid": pids[0]}))
    elif command == "child-pid":
        print(json.loads(STATE.read_text())["pid"])
    elif command == "child-check":
        saved = json.loads(STATE.read_text())
        check_crashed(wait_job(peer, saved["id"], TERMINAL))
        wait_ready(peer)
        pids = pool_pids()
        require(len(pids) == 1 and saved["pid"] not in pids, "child was not replaced")
        require(wait_job(peer, submit(peer, 0.01), TERMINAL)["status"] == "succeeded", "replacement could not run a job")
    elif command == "state-prepare":
        wait_ready(peer)
        job_id = submit(peer, 0.01)
        require(wait_job(peer, job_id, TERMINAL)["status"] == "succeeded", "baseline state job failed")
        STATE.write_text(json.dumps({"id": job_id}))
    elif command == "fill-state":
        fill_state()
    elif command == "state-check":
        check_state(peer, json.loads(STATE.read_text())["id"])
    elif command in ("drain-prepare", "load-prepare"):
        wait_ready(peer)
        ids = [submit(peer) for _ in range(1 if command == "drain-prepare" else 20)]
        expected = 1 if command == "drain-prepare" else 2
        eventually(lambda: [get_job(peer, job_id)["status"] for job_id in ids],
                   lambda states: states.count("running") == expected and states.count("queued") == len(ids) - expected,
                   "expected load was not running and queued before TERM")
        STATE.write_text(json.dumps({"ids": ids}))
    elif command == "poll-stop":
        saved = json.loads(STATE.read_text())
        saved["observed"] = poll_stopping(peer, saved["ids"])
        STATE.write_text(json.dumps(saved))
    elif command in ("drain-check", "load-logs"):
        check_drain(sys.stdin.read(), 1 if command == "drain-check" else 2)
    elif command == "load-recovered":
        wait_ready(peer)
        saved = json.loads(STATE.read_text())
        require(len(saved["ids"]) == 20, "load did not accept 20 jobs")
        check_recovered(peer, saved["ids"], saved["observed"])
    elif command == "flood":
        wait_ready(peer)
        flood(peer, args.url)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("startup", "ready", "child-prepare", "child-pid", "child-check",
                        "state-prepare", "fill-state", "state-check", "drain-prepare", "load-prepare", "poll-stop",
                        "drain-check", "load-logs", "load-recovered", "flood"))
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument("--kind", choices=("missing-model", "open-token-file"))
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        dispatch(args)
    except DrillFailure as error:
        print(str(error))
        return 1
    except (OSError, HTTPException, Unavailable, ValueError, KeyError, TypeError):
        print("transport or filesystem operation failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
