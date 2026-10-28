# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `BRIDGE_RERANK_MODEL` selects a pinned ONNX reranker; SQuAD v1.1 is available for quality measurement and the release model workflow compares its NDCG@10 with BM25.
- A non-loopback startup warning and `BRIDGE_TRUST_NETWORK=1` to acknowledge the supported network boundary, with a fixed JSON `start` warning when structured logging is enabled.
- Reproducible raw-socket request fuzzing: a fixed corpus followed by fresh seeded multi-byte flips, inserts, deletes and splices, with `BRIDGE_FUZZ_SECONDS` for longer runs and distinct-input counts; slow-client connection-budget recovery tests.
- Body and header limits and read/write timeouts in the Caddy TLS example; oversized bodies are rejected before contacting the worker.
- Seven no-model failure drills on dedicated compose peers, with bounded standard-library HTTP probes, offline verdict tests and an explicit start-crash-loop skip. CI's required deterministic gate includes the drills and digest-pinned promtool syntax and behavior checks of the seven example alert rules.
- Example Prometheus alert rules and a Grafana dashboard, with starting targets and import instructions in the operations guide.
- The start log line includes `result_capacity`.
- `BRIDGE_RESULT_CAPACITY` limits retained results at admission (default 384, giving 384 / 300 s = 1.28 jobs/s at default retention, three times the old combined limit's 0.43 jobs/s); `bridge_result_capacity` and `bridge_results_retained` expose the limit and current count.

### Changed

- Reranker acquisition downloads about 630 MB of models, plus the pinned wheels. The per-file cap is 150 MiB, raised to 300 MiB only for BGE.
- The model workflow runs on version tags as well as its existing triggers. Publishing now waits for its own read-only SQuAD quality gate; a default below BM25 prevents both image and release publication.
- TinyBERT is intended for question-to-passage reranking; it is below BM25 on ArguAna-style argument retrieval. Select and measure a reranker for the application's domain.
- State the supported deployment: behind a TLS reverse proxy that normalises HTTP and enforces limits, or on loopback/a private interface for a co-located client.
- Parser refusals use fixed JSON errors (400, 405 or 431); ambiguous framing, folded headers and control bytes are refused. Five-second header and body read budgets also stop clients that keep trickling bytes.
- At the active limit, `Retry-After` estimates queue drain time from the median run time (start to finish) of the 100 most recent jobs that started.
- `BRIDGE_OWNER_CAPACITY` now permits values up to the sum of active and result capacities. When unset, the start log line reports that sum as `owner_capacity`.
- `BRIDGE_CAPACITY` now limits queued and running jobs. Deployments that tuned it to bound memory should also set `BRIDGE_RESULT_CAPACITY`.
- Document the Packagist installation and check the published package on demand and on tags.
- The model wheels move to `numpy` 2.5.4, `onnxruntime` 1.31.0 and `tokenizers` 0.23.3 in `requirements-model.txt` and `requirements-model.lock`, which brings `huggingface-hub` 2.3.0 with `httpx2` in place of `requests` and drops `coloredlogs` and `sympy`. ONNX Runtime 1.31 sends telemetry to Microsoft's collector from import on, so every worker child now starts with `ORT_DISABLE_TELEMETRY=1`, whatever the service's environment says.
- The MCP server's dependencies moved to `mcp` 2.3.0 (`mcp-types` 2.3.0, `cryptography` 50.0.2, `httpcore2` and `httpx2` 2.16.0, `pycparser` 3.12, `pydantic` 2.14.1, `pydantic-core` 2.50.1, `pyjwt` 2.15.1, `rpds-py` 2026.9.1, `sse-starlette` 3.6.0, `starlette` 1.10.0, `truststore` 0.10.5, `uvicorn` 0.54.0); `requirements-mcp.lock` now also holds the hashes of the Linux aarch64 wheels.

## [0.4.0] - 2026-10-26

### Added

- A graceful drain: on SIGTERM or SIGINT the worker answers `POST /v1/jobs` with 503 `draining` and `Retry-After: 1`, fails queued jobs as `service_restarted` at once, keeps answering `GET`, and lets running jobs finish for up to `BRIDGE_DRAIN_SECONDS` (integer 0 to 300, default 30) before it stops; a second signal stops it at once.
- `GET /readyz`, behind any configured token like `/healthz`: 200 when the worker is not draining and a child is idle with its backend's models loaded, otherwise 503 with a reason (`draining`, `no_idle_child`, `models_loading`). A child now loads its backend's models as it starts instead of on its first job. Compose's MCP workers report healthy only once ready.
- Optional structured logging: `BRIDGE_LOG=json` writes one JSON object per line on stderr, best effort (a full queue or the exit can drop lines), a `request` line after each reply (method, route template, status, duration, the token's name), a `job` line when a job ends (job ID, task, owner, terminal status, queue and run times), and `start`, `drain` and `stop` lines. A fixed schema checked value by value keeps inputs, results, headers, query strings, tokens and exception text out; a stderr that blocks or fails never holds a request. `off`, the default, writes nothing, and any other value stops the worker at start naming `BRIDGE_LOG`.
- `BRIDGE_OWNER_CAPACITY`, a cap on one token's queued, running and retained jobs (1 to the capacity; unset, the whole capacity, so nothing changes without it). A submission over it is the same 429 `capacity_exceeded` as a full worker, with a `Retry-After` from that token's own first expiry; a repeated idempotency key is still answered, and the total capacity still applies. The `start` log line carries `owner_capacity`.
- Static analysis of the Python code in the required `deterministic` check: `py-analysis` runs `mypy --strict` over the worker and the MCP server and `ruff check` over them, the scripts and the tests, offline, with mypy 2.4.0 and ruff 0.17.2 hash-pinned in `requirements-dev.lock` and configured in `pyproject.toml`; neither is installed at runtime or shipped in the archive.
- A full connection budget (32) is answered 503 `service_busy` with `Retry-After: 1` and `Connection: close` instead of a closed socket, and a request line over 8 KiB or a header block over 16 KiB is refused with 431 `headers_too_large`.
- `docs/operations.md`, the operator's guide: deployment topologies, sizing from the measured figures, the state directory, token rotation, upgrades with a drain, what to graph and alert on, failure modes, and the versioning and support policy.

### Changed

- Locks moved past published advisories: `certifi` 2026.10.19 and `filelock` 4.2.1 in `requirements-model.lock`, `opentelemetry-api` 1.45.1 and `pyjwt` 2.15.0 in `requirements-mcp.lock`.

### Fixed

- The JSON log writes a `request` line only for a reply that was written in full: a peer gone mid-reply, or a busy 503 that could not be sent, no longer logs a status it never received. The `drain` line is no longer lost when a second signal stops the worker while the drain thread is held; it is written once, before `stop`. With `BRIDGE_LOG=off` requests, jobs and the drain no longer time or build lines that are never written.

## [0.3.0] - 2026-10-25

### Added

- Per-client tokens on the worker: `BRIDGE_TOKENS=name=token,...` or `BRIDGE_TOKENS_FILE` (one `name=token` per line, mode 0600 or stricter), at most 64, as an alternative to `BRIDGE_TOKEN`, which is the single token named `default`. A bad token, name, file or combination of the three stops the worker at start with a message naming the variable and never a token.
- A job belongs to the token that submitted it: another token's `GET` or `cancel` of it is 404, exactly as for an unknown job, and idempotency keys are per token. The owner's name is kept in `jobs.sqlite` (a file from an earlier version gains the column, its rows owned by `default`) and is not in the job's snapshot. A worker with only `BRIDGE_TOKEN` behaves as before.
- An `owner` label, the token's name, on `bridge_jobs_total`, `bridge_jobs_submitted_total` and `bridge_submissions_rejected_total`, with every configured name's series present at zero.
- `PhpAiBridge\Laravel\BridgeServiceProvider`, registered by Laravel's package discovery, binds one `Client` built from a publishable `config/ai_bridge.php` (`BRIDGE_URL`, `BRIDGE_TOKEN`, `BRIDGE_REQUEST_TIMEOUT_MS`; `vendor:publish --tag=ai-bridge-config`), so configuration caching works.
- `PhpAiBridge\Symfony\PhpAiBridgeBundle` registers `Client` as an autowirable service from a `php_ai_bridge` configuration whose `url`, `token` and `request_timeout_ms` default to the same environment variables.
- Both fail on a missing or invalid setting with a message that names it and never quotes the token. `laravel/framework` and `symfony/http-kernel` are suggested, not required: the library's requirements are unchanged.
- A TLS example: compose's `tls` profile runs Caddy from the pinned FrankenPHP image in front of the worker (`examples/tls/Caddyfile`, `tls internal`, no plain-HTTP listener, no token held), and `tls-check` drives the unchanged client through it trusting only the example CA. The integration guide's new "Cross-host use behind TLS" section says what to change for production.
- `SECURITY.md` with private vulnerability reporting, response windows and scope; Dependabot now also proposes weekly pip and Composer updates, a weekly `audit` job runs `pip-audit` over both Python locks and `composer audit` over `composer.lock` (accepted advisories in `.github/audit-ignore.txt`), and CodeQL scans the Python code.
- Published worker images: a `v*` tag pushes `ghcr.io/cristiangirlea/php-python-ai-bridge-worker` and `-worker-onnx` (`docker/Dockerfile.worker`, `docker/Dockerfile.worker-onnx`) for linux/amd64 and linux/arm64 with provenance, an SBOM and an attestation, and attaches the source archive to the release. The ONNX image installs the hash-locked wheels, which `requirements-model.lock` now pins for aarch64 too, and fetches models into a volume with `fetch`, never into the image. Compose's `release` profile runs both; CI builds and starts both on every pull request.
- `GET /metrics` on the worker, in the Prometheus text format and behind the service token: jobs by task and terminal status, submissions, 429 refusals, queue depth, running jobs, pool size and readiness, the concurrency and capacity settings, and per-task histograms of job and queue durations. Aggregates only, with labels from fixed sets; they live in memory and reset when the worker restarts.

## [0.2.0] - 2026-10-21

### Added

- `PhpAiBridge\Transport`, with `CurlTransport` as the client's default and `Psr18Transport` for an application's PSR-18 HTTP client (Symfony HttpClient, Guzzle and others): `Client` takes one as an optional last constructor argument. `psr/http-client` and `psr/http-factory` are suggested, not required.
- A 429 from the worker carries `Retry-After` in whole seconds: when the first retained job expires, or 1 when every slot holds an unfinished job. The PHP client exposes it as `BridgeException::$retryAfterSeconds` (and refuses a response header block over 16 KiB as `invalid_response`) without retrying by itself; the MCP server waits for it within a call's budget instead of guessing.
- `BRIDGE_STATE_DIR`: the worker keeps its job table (never inputs) in `jobs.sqlite` there, so retained results survive a restart and unfinished jobs are reported as `failed` with `service_restarted` instead of disappearing; unset, the table stays in memory as before. Compose's `worker` keeps it on a tmpfs.
- An optional `idempotency_key` on `POST /v1/jobs` (1–128 printable ASCII characters): a repeat while its job is retained returns that job with 202 instead of submitting again. The PHP client's `submit`, `submitRerank`, `submitEmbed` and `submitRedact` take it as `?string $idempotencyKey` and, when one is sent, accept any valid status in the response.

### Changed

- The dist archive now ships only `src/`, `composer.json`, `LICENSE`, `README.md`, `CHANGELOG.md` and `docs/`.

## [0.1.0] - 2026-10-20

First release: a typed PHP client for bounded Python AI tasks (rerank, embed, redact), with a FrankenPHP example and a stdio MCP server.

### Added

- The PHP client `PhpAiBridge\Client` for PHP 8.2 to 8.5 (`ext-curl`, `ext-json`): submit, poll, wait and cancel jobs, with typed `RerankResult`, `EmbedResult` and `RedactResult` objects that validate what the service returns. Rerank takes up to 512 documents and an optional `top_k`; embed up to 32 texts; redact one text.
- The Python 3.12 standard-library service: an explicit task allowlist, a pool of warm child processes with one job per child at a time, bounded concurrency and queue/result capacity, request-size limits, execution deadlines, cancellation of queued and running jobs, crash containment (a crashed, cancelled or timed-out child is replaced, and a job whose child died before taking it is tried once more on another) and generic error messages that omit internal exception text.
- Deterministic default backends for testing the integration: lexical word overlap for rerank, feature hashing for embed, and checksum and pattern rules (card numbers, IBANs, emails, IPv4 and IPv6 addresses, phone numbers) for redact. None of them is a model.
- The optional ONNX profile with four pinned, hash-verified models: `cross-encoder/ms-marco-TinyBERT-L2-v2` for rerank, `sentence-transformers/all-MiniLM-L6-v2` for embed, and the int8 cased and uncased `dslim/bert-base-NER` conversions for named-entity masking, all on CPU with no hosted AI account.
- The stdio MCP server: one tool per task over the same HTTP protocol, an index builder over the protocol and a `bridge_search` tool over what it built, a compose service that puts the ONNX models behind it, and an LLM evaluation runner.
- The FrankenPHP worker-mode example that submits work and returns a job ID at once.
- Everything runs inside hardened Docker Compose services; CI runs the deterministic suites on PHP 8.2, 8.3, 8.4 and 8.5 and the MCP suite offline, plus a consumer Composer install of the library.

### Limits

- Experimental prototype, not a production job queue: jobs and results live in memory, a restart loses them, and there is no durable delivery, no automatic retry of a failed or lost job and no idempotency key.
- Trusted services only: one bearer token authorizes every job, so the application must authenticate users and own the mapping from job IDs to their owners.
- Private HTTP transport: deploy behind an authenticated TLS boundary; the example routes have no end-user authentication and publish no ports.

[Unreleased]: https://github.com/cristiangirlea/php-python-ai-bridge/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/cristiangirlea/php-python-ai-bridge/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/cristiangirlea/php-python-ai-bridge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cristiangirlea/php-python-ai-bridge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cristiangirlea/php-python-ai-bridge/releases/tag/v0.1.0
