# Operations

How to deploy, size, upgrade and watch the worker. The [protocol](protocol.md) is the contract and the [security boundaries](security.md) say what the service protects; this guide links to them rather than repeating them. Every figure below comes from the measured tables in [the testing guide](testing.md), and every setting from `worker/ai_bridge/server.py` (`main()`) or `worker/ai_bridge/jobs.py`. The service keeps results, not work: a restart answers retained results and reports unfinished jobs, it never replays them, so it is not a durable job queue.

Some of what follows is available since 0.4.0 ([CHANGELOG.md](../CHANGELOG.md)): the drain and `BRIDGE_DRAIN_SECONDS`, `GET /readyz`, `BRIDGE_OWNER_CAPACITY`, the 503 `service_busy` and 431 answers, and `BRIDGE_LOG`.

## 1. Deployment topologies

The worker uses the standard library's `http.server`. It is supported only behind a reverse proxy that terminates TLS, normalises HTTP and enforces its own limits (the TLS example's Caddy or the operator's equivalent), or bound to loopback or a private interface for a co-located client. It is not supported as a directly exposed HTTP server. `--host` still defaults to `127.0.0.1`.

Binding a non-loopback address without `BRIDGE_TRUST_NETWORK=1` writes one start warning on stderr explaining that rule. With `BRIDGE_LOG=json` it is the `start` line at warning level, with the fixed `network_warning` field; otherwise it is one plain line even when logging is off. Set `BRIDGE_TRUST_NETWORK=1` only to acknowledge that the deployment satisfies the rule; it does not add network protection. Unset, empty and `0` leave the warning enabled; any other value stops startup naming the variable. Compose acknowledges its private internal network explicitly. Hostnames are conservatively treated as non-loopback.

The TLS example caps request bodies at 262144 bytes and buffers them before contacting the worker, caps headers at 16384 bytes, and sets five-second header/body read and idle timeouts and a ten-second write timeout. Apply equivalent limits to an operator-managed proxy; tune them deliberately with the worker's limits in mind.

**A sidecar for one application.** One worker next to one application on a private network, no published port, one token. This is what `docker/compose.yaml` runs (`worker` beside `frankenphp`) and what [the published image](../README.md#run-the-published-image) runs with `docker run --network <internal network>`. The client points at `http://worker:8090`.

**A shared worker behind TLS.** When clients run on other hosts, put a TLS proxy in front of the worker on the worker's host; compose's `tls` profile is the worked example (`tls-proxy` runs Caddy with `examples/tls/Caddyfile`), and [Cross-host use behind TLS](integration.md#cross-host-use-behind-tls) says what to change for production (a real certificate, the client configured with `https://` only, a proxy that never logs `Authorization`):

```sh
docker compose -f docker/compose.yaml --profile tls up -d tls-proxy worker
docker compose -f docker/compose.yaml run --rm --no-deps tls-check
docker compose -f docker/compose.yaml logs --no-log-prefix --no-color worker | docker compose -f docker/compose.yaml run --rm --no-deps -T tls-check php tests/php/tls.php request-log
docker compose -f docker/compose.yaml --profile tls down -v
```

Compose's worker enables JSON logging for these checks. The oversized POST leaves submitted-job counters unchanged; the log check requires both bracketing metrics requests and no worker request between them.

Give each client its own token through `BRIDGE_TOKENS_FILE` or `BRIDGE_TOKENS` (section 4), each under its own name, and the MCP server a token of its own. Better still, give the MCP server a dedicated worker, as compose's `mcp-worker` is: an agent can be prompt-injected into filling a worker's capacity ([security](security.md)).

**N instances behind a load balancer.** Set a unique `BRIDGE_INSTANCE_ID` on each worker (1-16 characters
of `[a-z0-9-]`, starting with a letter or digit). New IDs are `<instance-id>-<32 lowercase hex>`; unset,
they remain 32 lowercase hex. Each instance answers only jobs in its own table, including restored jobs
with an old prefix after an instance-name change. Preserve routes for those old prefixes until retention.
An absent job, including one with another instance's prefix, is the same 404 as an unknown job.

Compose's `ha` profile is the worked example: `worker-a` and `worker-b` share one token but have separate
state volumes and limits, under the same hardening block as the other workers. `ha-proxy`, Caddy in the
pinned FrankenPHP image with `examples/ha/Caddyfile`, sends `POST /v1/jobs` round robin to both workers;
`path_regexp` matchers route GET and cancel with `a-` to worker-a and `b-` to worker-b. Health and readiness
can reach either. There are no published ports; HTTP stays on the internal network. Use the operator's
TLS proxy for cross-host clients, as the TLS example shows. Test-only `test.delay` is enabled on these
two workers for cancellation and the crash drill; leave it disabled in a deployment.

There is **no shared job store and no failover of a running job**. An instance that dies loses execution
of the jobs it held. With its state directory surviving, unfinished jobs become `failed/service_restarted`
on startup, and retained results and keys remain; if the directory is lost, the IDs return 404. The
[client recovery recipe](integration.md#clients-behind-an-instance-router) resubmits with a new key and
capped attempts. Idempotency keys are instance-local too: round robin can send an uncertain POST retry
elsewhere and create duplicate work, so use submission affinity when that guarantee is needed.

```sh
sh scripts/ha_check.sh
```

The driver uses only the disposable `bridge-instances` Compose project: it starts the HA services,
runs the PHP client's routing checks through `http://ha-proxy:8080`, prepares a running keyed job on
worker-a, runs `docker compose ... kill -s SIGKILL worker-a` and `start worker-a`, then checks the
failure, retained key and successful recovery through the same URL. Its exit trap runs `down -v`,
including on failure. Both state directories are distinct named tmpfs volumes; the networkless
`ha-state` keeper holds their mounts across individual worker stops (a tmpfs with no mounts would be
emptied). This is restart-drill state, not host-crash durability: use separate persistent volumes for
real retained results, and do not use this project's name for another deployment.

**Published images.** A `v*` tag publishes `ghcr.io/cristiangirlea/php-python-ai-bridge-worker` (demo backends, standard library only) and `ghcr.io/cristiangirlea/php-python-ai-bridge-worker-onnx` (the hash-locked model wheels, no models), for linux/amd64 and linux/arm64 (`.github/workflows/publish.yml`). Both run as UID 65532 on port 8090. The ONNX image never holds models: its `fetch` entrypoint downloads the pinned, hash-checked files into a volume mounted at `/cache`, once, with network; the worker then reads them without network:

```sh
docker run --rm -v bridge-models:/cache ghcr.io/cristiangirlea/php-python-ai-bridge-worker-onnx:<version> fetch
```

Compose's `release` profile runs both images under the compose limits: `BRIDGE_IMAGE_TAG=<version> docker compose -f docker/compose.yaml --profile release up -d worker-image` (or `model-worker-image`, which reads `.cache/models` from the `fetcher`).

## 2. Sizing

**Memory and CPU per child.** Each child loads its own copy of its backend's models ([memory](testing.md#measured-model-worker-memory-informational)). On the `onnx` backend one child holding all four models and running the widest rerank batch peaked at 1286 MiB, and the container at 1464 MiB, of which about 180 MiB is the container itself (the server, the resource tracker and the wheels in the `/deps` tmpfs). The compose `model-worker` limit of 1536 MiB therefore allows `BRIDGE_CONCURRENCY=1` and no more; two children need up to about 2.7 GiB before any margin. Over 4290 jobs of the load mix the container peak stayed at 1022 MiB ([load](testing.md#measured-throughput-and-latency-under-load-informational)). With one CPU, that single child served about 3.5 jobs/s of the rerank, embed and redact mix with admission relaxed; warm medians are 0.05 s for a rerank of 32 documents, 0.21 s for 512, 0.16 s for an embed of 32 texts and 0.68 s for a redaction of 2000 characters, ([latency](testing.md#measured-latency-informational)). That table's cold first jobs (0.79 s for rerank, 2.31 s for redaction) come from the earlier design, in which a child loaded each model on its first job of that kind; today a child of the `onnx` backend loads its models when it starts, before it reports ready (`_serve` in `jobs.py`), so loading delays readiness, not a job, and that start time has no measurement. The lexical worker (`worker`: one CPU, 512 MiB, `BRIDGE_CONCURRENCY=2`) has no memory measurement; with admission relaxed it reached 18 jobs/s from one client thread and 115 jobs/s from sixteen, with no error. A warm child holds two threads, so `pids_limit: 128` is far from reached ([task counts](testing.md#measured-task-counts-under-a-cancellation-burst-informational)).

**Capacity and retention.** `BRIDGE_CAPACITY` (default 128) bounds queued and running jobs. `BRIDGE_RESULT_CAPACITY` (default 384) bounds retained finished jobs at admission; beyond either limit submissions get 429. A job that finishes while the result store is full is still retained. Retained results may exceed `BRIDGE_RESULT_CAPACITY` by at most `BRIDGE_CAPACITY`, and memory is bounded by the sum. Size the limits and retention together:

- The default sustained admission budget is `result capacity / retention`: 384 / 300 s = 1.28 jobs/s, three times the old combined limit's 128 / 300 s = 0.43 jobs/s, but below the 3.46 jobs/s one ONNX child measured with one client thread and retention 30 s in the historical admission-relaxed table in [the load measurements](testing.md#measured-throughput-and-latency-under-load-informational). Raising `BRIDGE_RESULT_CAPACITY` to 1024 gives 1024 / 300 s = 3.41 jobs/s; raise the container's memory with it. Alternatively, if clients collect results promptly, keeping 384 results and shortening `BRIDGE_RETENTION_SECONDS` to 120 gives 384 / 120 s = 3.2 jobs/s without increasing the result count. These are admission budgets; work, queueing and per-owner limits can lower throughput.
- Retention counts from completion, not submission. Size it for the delay between a job ending and the client collecting the result: the client's poll interval plus any delay before it polls. The MCP workers run with 30 s because the MCP server reads each result the moment its job ends.
- Throughput is also bounded by the work and the number of children. Size active capacity for the queue the application can tolerate.
- `BRIDGE_OWNER_CAPACITY` (1 to the sum of both capacities; unset, no per-owner cap) caps one token's queued, running and retained jobs. Shared limits still apply ([security](security.md)).

In a lexical-worker container, the parent process's `/proc/self/status` RSS grew from 27,744 to 36,576 KiB after retaining 1,024 successful rerank results of 32 short documents each, about 8.4 MiB per 1,000 results. This is a workload-specific estimate, not a bound for larger results or inputs. In a separate lexical-worker measurement with 1,024 successful embed results of 32 texts and 384 dimensions each, using 500 distinct words per text, parent RSS rose from 27,916 to 635,596 KiB (about 593.4 MiB, or 593 KiB of growth per result; total parent RSS divided by 1,024 is about 620 KiB per result). At the former 1,024 + 128 limits this alone extrapolates to about 695 MiB including baseline, exceeding 512 MiB. The default result capacity is therefore 384: 384 + 128 such results extrapolate to about 324 MiB parent RSS, leaving room for children and other container memory under compose's 512 MiB limit. This is a workload measurement, not a bound for all inputs, concurrent work, persistence or allocator behavior; size and monitor for the actual workload.

**Connections.** The worker serves at most 32 connections at once (`threading.BoundedSemaphore(32)` in `server.py`) and answers one more with 503 `service_busy`, `Retry-After: 1`, before reading the request. Every response closes its connection, so a poll is one connection for its duration only.

**Drain deadline.** `BRIDGE_DRAIN_SECONDS` (integer 0 to 300, default 30) is how long running jobs may finish after SIGTERM. Give the container runtime a stop timeout longer than it (Docker's default is 10 s: `docker stop -t 40`, or `stop_grace_period` in compose), or the runtime kills the drain short.

**Request limits** (`server.py`, `tasks.py`): a body of at most 262144 bytes (413 beyond), a request line of 8192 bytes and a header block of 16384 (431 beyond), five-second total read budgets for the request line plus headers and then the body, even for clients sending one byte per second (a header timeout closes the connection; a body timeout gets 408). Rerank takes 1 to 512 documents, embed 1 to 32 texts, both at most 200000 characters in all; redact one text of 1 to 8192 characters. `timeout_ms` is 100 to 300000, default 30000, and counts queue time. A child is replaced after `BRIDGE_WORKER_MAX_JOBS` jobs (default 1000).

## 3. State

Unset, `BRIDGE_STATE_DIR` keeps the job table in memory and a restart loses every job. Set, the worker writes the job table (never a job's input) to `<BRIDGE_STATE_DIR>/jobs.sqlite` with its `-wal` and `-shm` files. Give the directory to UID 65532 alone, mode 0700, as compose's `worker` does:

```
--tmpfs /state:rw,noexec,nosuid,size=64m,uid=65532,gid=65532,mode=0700
```

A tmpfs keeps results off disk but is removed when the container stops, so it survives only a restart of the process inside the same container. For results that survive a container replacement or an upgrade, use a volume, encrypted where the host allows it, owned by 65532 with mode 0700. The table holds results, which can reveal much of their inputs ([security](security.md)).

What survives a restart: finished jobs whose retention, counted from their completion, has not run out by the wall clock, served by `GET` as before, and their idempotency keys. A job that was `queued` or `running` becomes `failed` with `service_restarted`, finished at that start, so its retention begins then; its input was never stored, so it is never replayed. A repeat of its idempotency key returns that failed job until it expires, so resubmit such work with a new key. A row can therefore outlive `BRIDGE_RETENTION_SECONDS` measured from its submission, and a restored copy of the table serves its unexpired results after a restart; treat a backup as holding results ([security](security.md)), and keep one only where serving those results again is wanted.

## 4. Tokens

The worker reads exactly one of `BRIDGE_TOKEN` (one token, named `default`), `BRIDGE_TOKENS=name=token,...` or `BRIDGE_TOKENS_FILE`, once at start ([tokens](protocol.md#tokens)). The file holds one `name=token` per line; blank lines and lines starting with `#` are ignored:

```
# BRIDGE_TOKENS_FILE: a regular file, mode 0600, owned by the service user
shop=<at least 32 non-whitespace ASCII characters>
reports=<another token>
mcp=<the MCP server's own token>
```

A name is 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `_`, `.` and `-`; at most 64 tokens, none repeated. The name is the job's owner, the `owner` label in `/metrics` and the `owner` key in the log; the token itself appears nowhere. A client with another token gets 404 for a job it did not submit, exactly as for an unknown job, and its idempotency keys are its own. The MCP server takes its token from `BRIDGE_TOKEN` in its own environment; on a shared worker that is its own entry, for example `mcp`, so an injected agent sees only its own jobs ([MCP guide](mcp.md)).

Rotation needs two restarts, and each drains (section 5): during a drain every submission is refused with 503 `draining`, and jobs still queued fail as `service_restarted`. Plan for that refusal, and before each restart either let clients collect their outstanding results or keep the job table in a persistent `BRIDGE_STATE_DIR` (a volume, section 3) so finished results survive it:

1. Add the new token under a new name (`shop-2`) and restart the worker.
2. Switch the client to the new token. Jobs the old token submitted answer 404 to the new one, so keep the old token in the client for collecting them until they are collected or expire.
3. Remove the old entry and restart the worker. Removing an entry is also how one client is revoked.

With several instances, roll the change through them one at a time.

## 5. Upgrades

1. Read [CHANGELOG.md](../CHANGELOG.md) from your version to the new one; anything under Changed or Removed may need action.
2. Check the protocol version: `GET /healthz` returns `{"status":"ok","protocol":1,"backend":...}` and `/metrics` carries `bridge_info{protocol="1",...}`. Protocol 1 covers the shared routes and fields; an optional feature added since works only with a worker from the release that introduced it, as [CHANGELOG.md](../CHANGELOG.md) records (0.1.0 reports protocol 1 but refuses `idempotency_key`, added in 0.2.0). Check the worker's version for every optional feature a client uses.
3. Restart with a drain: SIGTERM makes the worker answer every `POST /v1/jobs` with 503 `draining` and `Retry-After: 1`, fail queued jobs as `service_restarted` at once, keep answering `GET`, and let running jobs finish for up to `BRIDGE_DRAIN_SECONDS`; a second signal stops it at once ([drain](protocol.md#drain-and-readiness)). With several instances, take one out of the balancer's new submissions, signal it, start its replacement, wait for `/readyz` to answer 200, then move on. A replacement serves the old process's finished results only when it opens the same job table: the same `BRIDGE_STATE_DIR` on a volume (section 3), reached through the same address the clients poll. Readiness says nothing about them; without that table, results not collected before the drain are gone.
4. For the ONNX worker, run `fetch` again when the new version adds a model: a worker whose model directory lacks a model's files refuses to start.

Images are tagged with the version without its `v` (`0.3.0`). A stable release also moves `latest`; a prerelease (`1.2.0-rc.1`) gets its version tag only. Deploy a pinned version, never `latest`, so a restart cannot upgrade by surprise; `gh attestation verify oci://ghcr.io/cristiangirlea/php-python-ai-bridge-worker:<version> --owner cristiangirlea` checks an image's provenance.

## 6. Monitoring

`/healthz` is liveness: it answers 200 whenever the process serves HTTP, a drain included. Use it to restart a dead container. `/readyz` answers 200 `{"status":"ready"}` when a child is idle with its models loaded, otherwise 503 with a reason: `draining`, `no_idle_child` (every child is starting, loading or busy, which a single busy child also is) or `models_loading` (an idle child failed to load its models). Use it to gate new submissions and the end of a rolling restart, never to restart a worker or to stop routing polls to an instance that holds jobs. Compose's health check probes `/readyz`.

`GET /metrics` takes any configured token and returns the Prometheus text format; counters reset when the worker restarts, so graph rates ([metrics](protocol.md#metrics)). Scrape it from inside the private network.

| Watch | From | Alert when |
| --- | --- | --- |
| Queue depth | `bridge_queue_depth` against `bridge_concurrency` | It stays above zero: the children cannot keep up |
| 429 rate per owner | `rate(bridge_submissions_rejected_total[5m])` by `owner` | It is sustained: the admission limit or that owner's cap is reached |
| Failed jobs, `worker_crashed` among them | `rate(bridge_jobs_total{status="failed"}[5m])` by `task` | It rises; the metric does not split error codes, so the client's job error says whether it was `worker_crashed` |
| Readiness | `/readyz`, and `bridge_pool_ready` against `bridge_pool_size` | Not ready for longer than a model load, or ready children stay below the pool size |
| p95 job duration | `histogram_quantile(0.95, sum by (le, task) (rate(bridge_job_duration_seconds_bucket[5m])))` | It nears the clients' `timeout_ms` |

`bridge_job_queue_seconds` separates waiting from running, and `bridge_jobs_total{status="timed_out"}` counts deadlines. Structured logs (since 0.4.0): `BRIDGE_LOG=json` writes one JSON line on stderr per request and per finished job, plus start, drain and stop, with a fixed schema that never holds inputs, results, headers, tokens or exception text; off by default. Logging is best effort: lines are written behind a bounded queue and are dropped when it is full or at exit, so a log is no audit record and no substitute for `/metrics`. [The protocol](protocol.md#logging) has the schema.

## 7. Failure modes

| Symptom | Cause | What the service does | What the operator does |
| --- | --- | --- | --- |
| 429 `capacity_exceeded` | Active jobs reach `BRIDGE_CAPACITY` or retained results reach `BRIDGE_RESULT_CAPACITY` | Refuses the submission; `Retry-After` estimates the active queue or gives the first result expiry | Resubmit after the delay; if sustained, size capacity and retention (section 2) |
| 429 for one token only | That token reaches `BRIDGE_OWNER_CAPACITY` | The same 429, with `Retry-After` from that token's own first expiry | Raise the cap, or find why that client holds so many jobs |
| Jobs fail as `worker_crashed` | A child died while running a job (out of memory, a native crash) | Fails the job, replaces the child; a child that dies before it is ready is replaced after a 1 s backoff (`BACKOFF_SECONDS` in `jobs.py`), and while no child can start, queued jobs fail as `worker_start_failed` | Check the container's memory events against section 2; lower concurrency or raise the limit |
| ONNX worker exits at start | A model directory lacks its files | Refuses to start, naming the directory and the missing models | Run `fetch` (image) or the compose `fetcher` into that volume |
| Worker exits at start naming `BRIDGE_STATE_DIR` | The directory cannot hold `jobs.sqlite` | Refuses to start | Fix the directory's owner (65532) and mode |
| Worker exits at start naming `BRIDGE_TOKENS_FILE` | The file is readable by group or others, missing, unreadable or malformed | Refuses to start; the message never quotes a token | `chmod 0600` the file, owned by the service user; fix the entry the message names |
| Jobs end `cancelled` at a restart | Running jobs outlast `BRIDGE_DRAIN_SECONDS` | Stops the children; the stopping process reports those jobs `cancelled`, and with `BRIDGE_STATE_DIR` the next start reports them `service_restarted` | Raise the drain deadline above the longest job; clients resubmit |
| Jobs vanish or end `service_restarted` after a stop | The runtime killed the process (SIGKILL after its stop timeout, 10 s by default in Docker) before the drain ended | Nothing: no job is marked `cancelled`; with `BRIDGE_STATE_DIR` the next start fails the unfinished rows as `service_restarted`, without it every job is gone | Give the runtime a stop timeout longer than `BRIDGE_DRAIN_SECONDS` (section 2); clients resubmit |
| 503 `service_busy` | 32 connections are open | Answers at once with `Retry-After: 1` | Find the client holding connections; put rate limiting in front of the worker |

## 8. Versioning and support policy

Releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and each is described in [CHANGELOG.md](../CHANGELOG.md). Before 1.0, a breaking change comes in a new minor version (0.3 to 0.4) and is listed under Changed or Removed; from 1.0, only in a new major version.

- **Breaking in the protocol:** removing or renaming a route, a request or response field, an error code or a job status, adding a job status (`Job::fromArray()` refuses one it does not know), changing a field's type or meaning or an answer's HTTP status, or narrowing what a request accepts. Adding a route, an optional request field, a response field (the PHP client ignores keys it does not read) or a metric is not breaking. A breaking protocol change also raises the version `/healthz` reports.
- **Breaking in the PHP API:** changing a public signature of the classes in `src/` (`Client`, `Job`, the result types, `BridgeException`, the transports), removing a public method, property or class, or narrowing what a method accepts. A new optional trailing parameter or a new class is not breaking.
- **PHP versions:** one package supports PHP 8.2, 8.3, 8.4 and 8.5, the upstream-supported branches; 8.2's security support ends 2026-12-31, 8.3's 2027-12-31, 8.4's 2028-12-31 and 8.5's 2029-12-31 ([support](testing.md#one-package-supported-php-branches)). A branch past its end leaves the matrix and the Composer range in a documented release.
- **Deprecation:** anything to be removed is marked deprecated in the CHANGELOG at least one minor release before the release that removes it.
- **Security fixes:** only the latest minor release receives them ([SECURITY.md](../SECURITY.md)); upgrade to it first.
