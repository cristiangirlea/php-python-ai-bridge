"""Informational soak of a running worker: steady load for hours, one JSON line per window. Never a gate.

Runs scripts/load_measure.py's threads in windows of SOAK_WINDOW_SECONDS until SOAK_SECONDS have passed, and after
each window prints its completed jobs, rate, latency percentiles, 429s and errors with the worker's own counters
from /metrics, so drift, leaks and failures show up as a trend and a run cut short still leaves every finished window.
Memory is sampled outside the container (scripts/soak.sh), since the worker cannot see its own cgroup limit.

    SOAK_TARGET=http://model-worker:8090 SOAK_SECONDS=28800 SOAK_CONCURRENCY=4 BRIDGE_TOKEN=... python scripts/soak.py
"""

import datetime
import json
import os
import re
import sys
import time
from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import TOKEN, wait_ready  # noqa: E402  (the same protocol)
from load_measure import ORDER, _seconds, drive, percentile  # noqa: E402  (the same load)

# The worker's own counters worth a trend: retained results and children, and terminal jobs by status.
METRICS = re.compile(r"^(bridge_results_retained|bridge_queue_depth|bridge_pool_ready|bridge_pool_size"
                     r"|bridge_jobs_total)(\{[^}]*\})? ([0-9.e+-]+)$")


def settings(environ) -> dict:
    target = environ.get("SOAK_TARGET", "http://model-worker:8090")
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?", target):
        raise SystemExit("SOAK_TARGET must be a URL such as http://model-worker:8090")
    concurrency = environ.get("SOAK_CONCURRENCY", "4")
    if not re.fullmatch(r"\d{1,2}", concurrency) or not 1 <= int(concurrency) <= 64:
        raise SystemExit("SOAK_CONCURRENCY must be a thread count from 1 to 64")
    return {"base": target, "concurrency": int(concurrency),
            "seconds": _seconds(environ, "SOAK_SECONDS", "28800", True),
            "window": _seconds(environ, "SOAK_WINDOW_SECONDS", "600", True)}


def metrics(base: str) -> dict:
    """The counters above, summed over labels except status; an error entry when the worker does not answer."""
    request = Request(base + "/metrics", headers={"Authorization": "Bearer " + TOKEN})
    try:
        with urlopen(request, timeout=10) as response:
            text = response.read().decode()
    except (OSError, HTTPException, ValueError) as error:
        return {"error": f"{type(error).__name__}: {error}"}
    values: dict = {}
    for line in text.splitlines():
        found = METRICS.match(line)
        if not found:
            continue
        name, labels, value = found.groups()
        status = re.search(r'status="([a-z_]+)"', labels or "")
        key = f"{name}:{status.group(1)}" if name == "bridge_jobs_total" and status else name
        values[key] = values.get(key, 0) + float(value)
    return values


def window(records: list, elapsed: float) -> dict:
    times = [latency for _, outcome, latency in records if outcome == "ok"]
    row = {"jobs": len(times), "jobs_per_s": round(len(times) / elapsed, 3),
           "p50": percentile(times, 50), "p95": percentile(times, 95), "max": max(times, default=None),
           "refused_429": sum(outcome == "429" for _, outcome, _ in records),
           "errors": sum(outcome == "error" for _, outcome, _ in records)}
    for task in ORDER:
        task_times = [latency for name, outcome, latency in records if name == task and outcome == "ok"]
        row[f"p95_{task}"] = percentile(task_times, 95)
    return row


def main() -> None:
    run = settings(os.environ)
    wait_ready(run["base"])
    started = time.monotonic()
    index = 0
    while time.monotonic() - started < run["seconds"]:
        length = min(run["window"], run["seconds"] - (time.monotonic() - started))
        elapsed, records, errors = drive(run["base"], "mix", run["concurrency"], length)
        line = {"window": index, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "elapsed_s": round(elapsed, 1), **window(records, elapsed),
                "error_kinds": sorted(set(errors))[:5], "worker": metrics(run["base"])}
        print(json.dumps(line), flush=True)
        index += 1


if __name__ == "__main__":
    main()
