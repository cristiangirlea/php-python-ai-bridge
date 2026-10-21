# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/cristiangirlea/php-python-ai-bridge/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cristiangirlea/php-python-ai-bridge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cristiangirlea/php-python-ai-bridge/releases/tag/v0.1.0
