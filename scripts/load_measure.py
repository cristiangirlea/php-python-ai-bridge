"""Informational throughput and latency of a running worker under concurrent load. Never a gate.

N threads each submit a job, poll it every 0.05 s until it is terminal, and submit the next, until the stated duration
has passed; a thread then finishes the job in hand, given up 5 s after its 120 s timeout. A 429 is answered by waiting
its Retry-After, within the run. The inputs are scripts/benchmark.py's: rerank 32 documents of ~40 words, embed 32
texts, redact 2000 characters, or a mix that cycles the three. Each concurrency level after the first waits
LOAD_SETTLE_SECONDS first, so that the jobs the level before it left in retention have expired and every level starts
from an empty job table. The output is a Markdown block for docs/testing.md; progress goes to stderr.

    LOAD_TARGET=lexical LOAD_CONCURRENCY=1,4,16 LOAD_SECONDS=120 LOAD_CASE=mix BRIDGE_TOKEN=... python scripts/load_measure.py
"""

import datetime
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import CASES, POLL_S, TERMINAL, call, cpu_model, wait_ready  # noqa: E402  (the same protocol and inputs)

TARGETS = {"onnx": "http://model-worker:8090", "lexical": "http://worker:8090"}
TASKS = {"rerank": CASES[0][2], "embed": CASES[3][2], "redact": CASES[4][2]}
ORDER = ("rerank", "embed", "redact")
# The compose limits of each named target, as scripts/benchmark.py states them; capacity and retention are the defaults.
LIMITS = {"onnx": "model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1",
          "lexical": "worker cpus=1, mem_limit=512m, BRIDGE_CONCURRENCY=2"}
MAX_CONCURRENCY = 64
TIMEOUT_MS = 120000
ACTIVE = {"queued", "running"}


def _seconds(environ, name: str, default: str, positive: bool) -> float:
    try:
        value = float(environ.get(name, default))
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise SystemExit(f"{name} must be a finite number of seconds{' above zero' if positive else ''}")
    return value


def settings(environ) -> dict:
    """The run's settings from the environment; a malformed one stops the run with the variable's name."""
    target = environ.get("LOAD_TARGET", "onnx")
    if target in TARGETS:
        backend, base = target, TARGETS[target]
    elif re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?", target):
        backend, base = None, target
    else:
        raise SystemExit("LOAD_TARGET must be onnx, lexical or a URL such as http://worker:8090")
    levels = [level.strip() for level in environ.get("LOAD_CONCURRENCY", "1,4").split(",")]
    if not all(re.fullmatch(r"\d{1,2}", level) and 1 <= int(level) <= MAX_CONCURRENCY for level in levels):
        raise SystemExit(f"LOAD_CONCURRENCY must be a comma list of thread counts from 1 to {MAX_CONCURRENCY}")
    case = environ.get("LOAD_CASE", "mix")
    if case not in TASKS and case != "mix":
        raise SystemExit("LOAD_CASE must be rerank, embed, redact or mix")
    return {"backend": backend, "base": base, "concurrency": [int(level) for level in levels],
            "seconds": _seconds(environ, "LOAD_SECONDS", "120", True), "case": case,
            # The worker's default retention: a level's jobs have all expired this long after its last one ended.
            "settle": _seconds(environ, "LOAD_SETTLE_SECONDS", "300", False)}


def pick(case: str, thread: int, count: int) -> str:
    """The task of a thread's count-th job; the mix starts each thread on a different task so they overlap."""
    return ORDER[(thread + count) % len(ORDER)] if case == "mix" else case


def percentile(values: list, p: float):
    """Nearest rank: the smallest value with at least p percent of the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p * len(ordered) / 100)) - 1]


def retry_after(error: HTTPError) -> int:
    """The worker's Retry-After in whole seconds, read as the PHP client reads it; anything else waits 1 s."""
    value = error.headers.get("Retry-After", "") if error.headers else ""
    return int(value) if re.fullmatch(r"[0-9]{1,6}", value) and int(value) >= 1 else 1


def one_job(base: str, task: str, deadline: float, records: list, errors: list) -> None:
    """Submits one job and follows it to its end, appending (task, outcome, latency) to records."""
    started = time.monotonic()
    try:
        job = call(base, "POST", "/v1/jobs", {"task": task, "input": TASKS[task], "timeout_ms": TIMEOUT_MS})
        # The job's own bound, not the level's: a job in hand may finish after submissions stop, but never hangs.
        expires = started + TIMEOUT_MS / 1000 + 5
        while job["status"] not in TERMINAL:
            if job["status"] not in ACTIVE:
                raise ValueError(f"unknown status {job['status']!r}")
            if time.monotonic() > expires:
                raise TimeoutError(f"still {job['status']} {TIMEOUT_MS / 1000 + 5:g} s after its submission")
            time.sleep(POLL_S)
            job = call(base, "GET", "/v1/jobs/" + job["id"])
        if job["status"] == "succeeded":
            records.append((task, "ok", time.monotonic() - started))
            return
        message = f"{task}: {job['status']} {(job.get('error') or {}).get('code')}"
    except HTTPError as error:
        if error.code == 429:
            records.append((task, "429", None))
            time.sleep(min(retry_after(error), max(0.0, deadline - time.monotonic())))
            return
        message = f"{task}: HTTP {error.code}"
    # Every failure is counted: a thread lost to a surprise would leave a table that looks normal.
    except Exception as error:
        message = f"{task}: {type(error).__name__}: {error}"
    records.append((task, "error", None))
    errors.append(message)
    # A failing worker is not hammered: the thread pauses as it would for a 429.
    time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def drive(base: str, case: str, concurrency: int, seconds: float) -> tuple:
    """Runs one level and returns its wall time, until the last thread ended, with its records and error messages."""
    records, errors = [], []
    started = time.monotonic()
    deadline = started + seconds

    def thread_body(thread: int) -> None:
        count = 0
        while time.monotonic() < deadline:
            one_job(base, pick(case, thread, count), deadline, records, errors)
            count += 1

    threads = [threading.Thread(target=thread_body, args=(index,)) for index in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return time.monotonic() - started, records, errors


def summarise(case: str, concurrency: int, elapsed: float, records: list) -> list:
    """A row for the level, and for the mix one more per task: completed jobs, their rate and latency percentiles,
    and the 429s and errors answered."""
    groups = [(case, records)]
    if case == "mix":
        groups += [(f"mix: {task}", [record for record in records if record[0] == task]) for task in ORDER]
    rows = []
    for label, group in groups:
        times = [latency for _, outcome, latency in group if outcome == "ok"]
        rows.append((label, concurrency, elapsed, len(times), len(times) / elapsed,
                     percentile(times, 50), percentile(times, 95), percentile(times, 99), max(times, default=None),
                     sum(outcome == "429" for _, outcome, _ in group), sum(outcome == "error" for _, outcome, _ in group)))
    return rows


def render(header: dict, rows: list) -> str:
    limits = (LIMITS[header["backend"]] + "; active capacity 128 jobs, result capacity 384 jobs and retention 300 s, the defaults"
              if header["backend"] else "as configured at that worker, not read by this script")
    lines = [
        f"Measured {header['date']} UTC, commit `{header['commit']}`, runner: {header['runner']}, CPU: {header['cpu']}.",
        f"Target: `{header['backend'] or 'unnamed'}` at {header['base']}; limits: {limits}.",
        f"Case `{header['case']}`, {header['seconds']:g} s per level: each thread submits a job, polls it every "
        f"{POLL_S:g} s until it is terminal and submits the next; at the end it finishes the job in hand. Latency runs "
        "from POST /v1/jobs to the first GET showing a terminal state, for jobs that succeeded. A 429 is answered by "
        "waiting its Retry-After within the run, which is in no latency. Jobs/s is succeeded jobs over the level's wall "
        "time, until its last thread ended.",
        "",
        "| Case | Concurrency | Duration s | Jobs completed | Jobs/s | p50 s | p95 s | p99 s | max s | 429s | Errors |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, concurrency, elapsed, done, rate, p50, p95, p99, high, refused, failed in rows:
        figures = " | ".join("n/a" if value is None else f"{value:.2f}" for value in (p50, p95, p99, high))
        lines.append(f"| {label} | {concurrency} | {elapsed:.1f} | {done} | {rate:.2f} | {figures} | {refused} | {failed} |")
    return "\n".join(lines)


def main() -> None:
    run = settings(os.environ)
    reported = wait_ready(run["base"])["backend"]
    if run["backend"] and reported != run["backend"]:
        raise SystemExit(f"{run['base']} reports backend {reported!r}, expected {run['backend']!r}")
    rows = []
    for index, concurrency in enumerate(run["concurrency"]):
        if index and run["settle"]:
            print(f"waiting {run['settle']:g} s for the previous level's jobs to leave retention", file=sys.stderr)
            time.sleep(run["settle"])
        elapsed, records, errors = drive(run["base"], run["case"], concurrency, run["seconds"])
        level = summarise(run["case"], concurrency, elapsed, records)
        rows += level
        print(f"concurrency {concurrency}: {level[0][3]} succeeded, {level[0][9]} answered 429, {len(errors)} errors"
              f" in {elapsed:.1f} s", file=sys.stderr)
        for message in sorted(set(errors))[:5]:
            print("  error: " + message, file=sys.stderr)
    print(render({"date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
                  "commit": os.environ.get("BENCH_COMMIT", "unknown"),
                  "runner": os.environ.get("BENCH_RUNNER", "unspecified"), "cpu": cpu_model(),
                  "backend": reported if run["backend"] else None, "base": run["base"], "case": run["case"],
                  "seconds": run["seconds"]}, rows))


if __name__ == "__main__":
    main()
