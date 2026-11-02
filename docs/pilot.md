# Pilot

The tests, drills and load runs in [testing](testing.md) and [operations](operations.md#drills) show how the worker behaves on purpose-built traffic. A pilot shows how it behaves under one real application's traffic. Production grade needs that evidence; this page says what a pilot is, what to watch and what to write down.

## Setup

- One real application, owned by the operator, sends real work to the worker through the PHP client or the MCP server. Synthetic traffic does not count.
- Deploy a released image by digest, as the supported tier in [operations](operations.md#1-deployment-topologies) describes: a private network or loopback, the operator's TLS proxy across hosts, its own token per client in `BRIDGE_TOKENS_FILE`, and `BRIDGE_STATE_DIR` set. Real model work needs `ghcr.io/cristiangirlea/php-python-ai-bridge-worker-onnx` with its models fetched once into the model volume (its `fetch` entrypoint); `ghcr.io/cristiangirlea/php-python-ai-bridge-worker` has the demo backends only, which are not models.
- Turn on `BRIDGE_LOG=json` and collect stderr. Scrape `/metrics` with [the alert rules and dashboard](operations.md#alerts-and-dashboard) loaded.
- Probe authenticated `/readyz` at a fixed interval and keep the results: the metrics and alerts do not measure readiness availability.
- Record before starting: the worker version and image digest, the host (CPU, memory), the settings that differ from the defaults, the tasks and backends in use, and the expected daily job count.

## Duration

At least four weeks, and at least one upgrade or restart of the worker during that time, so that a drain and the job table's recovery happen on real traffic.

## What to watch

Weekly, from the dashboard and the logs, against [the targets](operations.md#targets):

| Measure | Target |
| --- | --- |
| Authenticated `/readyz` probes that answer 200, planned drains excluded | 99.9% |
| p95 job duration per task | Under 5 s, or the figure set for this deployment's hardware |
| Failed jobs (`failed`, `worker_crashed` among them) | Under 0.1% of terminal jobs |
| Timed-out jobs | Each one explained |
| Queue depth over 5 minutes | At most 3 |
| Retained results | Under 90% of `bridge_result_capacity` |

## Incidents

Write down every alert that fires, every failed or timed-out job that was not the caller's fault, and every time someone had to act on the worker. One entry each, in a private issue tracker or a file the operator keeps (never with job inputs, results or tokens):

```text
Date and time (UTC):
Worker version and digest:
What was seen (alert, metric, log line, user report):
Impact (jobs affected, how long):
Cause:
What was done:
Follow-up (issue link, or none):
```

A defect in the worker found this way becomes an issue in this repository; a vulnerability goes through [SECURITY.md](../SECURITY.md).

## Done

The pilot ends with a short report on the [production-grade tracking issue](https://github.com/cristiangirlea/php-python-ai-bridge/issues/112): the dates, the setup above, the weekly measures, the incident count with a line each, and whether each target held. Targets missed with a known cause and a fix count as evidence too; an unexplained miss does not close the pilot.
