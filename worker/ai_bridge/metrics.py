"""Aggregate counts and timings in the Prometheus text exposition format, version 0.0.4, with the standard library.
Label values come only from fixed sets (the task allowlist, the terminal statuses, the backend name), never from a
job, so nothing a client sent can reach the body and the number of series is bounded."""

import bisect
import math

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# Upper bounds in seconds; a job longer than the last falls only into +Inf.
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)


def new_histogram():
    """Counts per bucket, not cumulative, the last for +Inf only; render() adds them up."""
    return {"buckets": [0] * (len(BUCKETS) + 1), "sum": 0.0, "count": 0}


def observe(histogram, seconds):
    # bisect_left puts a value equal to a bound in that bound's bucket, as le (less or equal) means.
    histogram["buckets"][bisect.bisect_left(BUCKETS, seconds)] += 1
    histogram["sum"] += seconds
    histogram["count"] += 1


def escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def number(value):
    if isinstance(value, float):
        return "+Inf" if value == math.inf else repr(value)
    return str(value)


def _labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{escape(value)}"' for key, value in labels.items()) + "}"


def _family(name, kind, help_text, lines):
    return f"# HELP {name} {help_text}\n# TYPE {name} {kind}\n" + "".join(line + "\n" for line in lines)


def _simple(name, kind, help_text, samples):
    return _family(name, kind, help_text, [f"{name}{_labels(labels)} {number(value)}" for labels, value in samples])


def counter(name, help_text, samples):
    return _simple(name, "counter", help_text, samples)


def gauge(name, help_text, samples):
    return _simple(name, "gauge", help_text, samples)


def histogram(name, help_text, series):
    lines = []
    for labels, values in series:
        total = 0
        for bound, count in zip(BUCKETS + (math.inf,), values["buckets"]):
            total += count
            lines.append(f"{name}_bucket{_labels({**labels, 'le': number(bound)})} {total}")
        lines.append(f"{name}_sum{_labels(labels)} {number(float(values['sum']))}")
        lines.append(f"{name}_count{_labels(labels)} {values['count']}")
    return _family(name, "histogram", help_text, lines)


def render(snapshot):
    """The whole body from JobStore.snapshot(), which is a copy: nothing here holds the store's lock."""
    tasks = snapshot["tasks"]
    return "".join([
        counter("bridge_jobs_total", "Jobs that reached a terminal status since the process started.",
                [({"task": task, "status": status}, count) for (task, status), count in snapshot["jobs"].items()]),
        counter("bridge_jobs_submitted_total", "Jobs accepted since the process started.",
                [({"task": task}, snapshot["submitted"][task]) for task in tasks]),
        counter("bridge_submissions_rejected_total", "Submissions refused with 429 since the process started.",
                [({"reason": reason}, count) for reason, count in snapshot["rejected"].items()]),
        gauge("bridge_queue_depth", "Jobs waiting for a child process.", [({}, snapshot["queued"])]),
        gauge("bridge_jobs_running", "Jobs a child process has started.", [({}, snapshot["running"])]),
        gauge("bridge_pool_size", "Child processes in the pool.", [({}, snapshot["pool_size"])]),
        gauge("bridge_pool_ready", "Child processes in the pool that have said they are ready.",
              [({}, snapshot["pool_ready"])]),
        gauge("bridge_concurrency", "Configured number of child processes.", [({}, snapshot["concurrency"])]),
        gauge("bridge_capacity", "Configured limit of queued, running and retained jobs.",
              [({}, snapshot["capacity"])]),
        histogram("bridge_job_duration_seconds", "Seconds from submission to terminal status.",
                  [({"task": task}, snapshot["duration"][task]) for task in tasks]),
        histogram("bridge_job_queue_seconds", "Seconds from submission to a child starting the job.",
                  [({"task": task}, snapshot["queue"][task]) for task in tasks]),
        gauge("bridge_info", "Protocol version and configured backend.",
              [({"protocol": "1", "backend": snapshot["backend"]}, 1)]),
    ])
