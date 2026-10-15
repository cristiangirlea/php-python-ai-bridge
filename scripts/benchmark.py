"""Informational job latency against running workers. Asserts correctness, never speed.

The worker keeps a pool of warm child processes, so only a child's first job of a kind imports
dependencies and loads that kind's model. Each backend's first job is reported on its own as the
cold row, on the assumption that the worker has served nothing yet (restart it first); every other
row is the median of warm jobs after one job of that case has run. The output is a Markdown block
for docs/testing.md; progress goes to stderr.
"""

import datetime
import json
import os
import platform
import statistics
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

TOKEN = os.environ.get("BRIDGE_TOKEN", "")


def targets() -> list:
    pairs = [target.split("=", 1) for target in
             os.environ.get("BENCH_TARGETS", "onnx=http://model-worker:8090,lexical=http://worker:8090").split(",")]
    if any(len(target) != 2 or not all(target) for target in pairs):
        raise SystemExit("BENCH_TARGETS entries must look like backend=http://host:port, separated by commas")
    return pairs


RUNS = int(os.environ.get("BENCH_RUNS", "5"))
POLL_S = 0.05
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled"}

WORDS = ("garden rain seed river stone market bridge window paper cloud harbour lantern valley "
         "orchard meadow copper ribbon candle station ladder").split()
QUERY = "Where is the harbour lantern kept?"


def sentence(seed: int, count: int) -> str:
    return " ".join(WORDS[(seed * 7 + i * 3) % len(WORDS)] for i in range(count)) + "."


# Deterministic inputs: the same text on every run and every runner. The first case is the one the cold row runs.
CASES = [
    ("rerank 32 documents of ~40 words", "rerank",
     {"query": QUERY, "documents": [sentence(i, 40) for i in range(32)]}, "1 batch of 32"),
    ("rerank 512 documents of ~40 words", "rerank",
     {"query": QUERY, "documents": [sentence(i, 40) for i in range(512)], "top_k": 10}, "16 batches of 32"),
    # The worst reachable batch: 32 x 6000 characters fits the 200000-character budget and every
    # document still truncates to 512 tokens, so this is one full batch at the widest possible padding.
    ("rerank 32 documents of 6000 characters", "rerank",
     {"query": QUERY, "documents": [(sentence(i, 1200) * 2)[:6000] for i in range(32)], "top_k": 5},
     "1 full batch of 32 at the 512-token limit, the worst case"),
    ("embed 32 texts of ~40 words", "embed", {"texts": [sentence(i, 40) for i in range(32)]}, "1 forward pass"),
    ("redact 2000 characters, PER+ORG+LOC", "redact",
     {"text": ("Maria Lopez of Northwind Traders met John Carter in Lisbon on Tuesday; write to "
               "maria.lopez@example.com or call +44 20 7946 0958. " * 20)[:2000], "entities": ["PER", "ORG", "LOC"]},
     "rules plus windows of 512"),
]
# What the ONNX backend loads on a child's first job of each task; the lexical backend loads nothing.
MODELS = {"rerank": "the cross-encoder", "embed": "the embedding model", "redact": "both NER models"}


def call(base: str, method: str, path: str, body=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def wait_ready(base: str) -> dict:
    # A restarted model worker installs its wheels before it answers.
    deadline = time.monotonic() + 180
    while True:
        try:
            return call(base, "GET", "/healthz")
        except (URLError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def run_once(base: str, task: str, payload: dict) -> float:
    started = time.monotonic()
    job = call(base, "POST", "/v1/jobs", {"task": task, "input": payload, "timeout_ms": 120000})
    while job["status"] not in TERMINAL:
        time.sleep(POLL_S)
        job = call(base, "GET", "/v1/jobs/" + job["id"])
    elapsed = time.monotonic() - started
    progress = job.get("progress") or {}
    if job["status"] != "succeeded" or progress.get("completed") != progress.get("total"):
        raise SystemExit(f"{task} did not succeed cleanly: {job['status']} {job.get('error')} {progress}")
    return elapsed


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def main() -> None:
    rows = []
    for backend, base in targets():
        reported = wait_ready(base)["backend"]
        if reported != backend:
            raise SystemExit(f"{base} reports backend {reported!r}, expected {backend!r}")
        loaded = set()
        for index, (label, task, payload, note) in enumerate(CASES):
            # One job of the case first: the backend's very first job is the cold row, and a first job that loads
            # a model is named in its row's note. Then the warm jobs the row measures.
            first = run_once(base, task, payload)
            print(f"{backend} {label} first job: {first:.2f} s", file=sys.stderr)
            loads = backend == "onnx" and task not in loaded
            loaded.add(task)
            if index == 0:
                rows.append((backend, "first job after start: " + label, 1, first, first, first,
                             "cold; the child imports its dependencies"
                             + (f" and loads {MODELS[task]}" if loads else "")))
            times = []
            for run in range(RUNS):
                times.append(run_once(base, task, payload))
                print(f"{backend} {label} run {run + 1}: {times[-1]:.2f} s", file=sys.stderr)
            if backend == "onnx":
                detail = "warm; " + note
                if index and loads:
                    detail += f"; the first job, loading {MODELS[task]}, took {first:.2f} s"
            else:
                detail = "warm process, no model"
            rows.append((backend, label, RUNS, min(times), statistics.median(times), max(times), detail))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    print(f"""Measured {now} UTC, commit `{os.environ.get("BENCH_COMMIT", "unknown")}`, runner: {os.environ.get("BENCH_RUNNER", "unspecified")}, CPU: {cpu_model()}.
Limits: model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1; worker cpus=1, mem_limit=512m.
N={RUNS} sequential warm jobs per row, after one job of the case; wall time from POST /v1/jobs to the first GET showing a terminal state, polled every {POLL_S:g} s.

| Backend | Case | N | min s | p50 s | max s | Note |
| --- | --- | --- | --- | --- | --- | --- |""")
    for backend, label, count, low, median, high, note in rows:
        print(f"| {backend} | {label} | {count} | {low:.2f} | {median:.2f} | {high:.2f} | {note} |")


if __name__ == "__main__":
    main()
