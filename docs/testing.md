# PHP support and verification

## One package, supported PHP branches

As of 2026-09-17, PHP's upstream-supported branches are **8.2, 8.3, 8.4 and 8.5**. Security-only maintenance counts as supported. See [PHP's official schedule](https://www.php.net/supported-versions.php).

| PHP | Upstream maintenance on that date | Security support ends |
| --- | --- | --- |
| 8.2 | Security fixes only | 2026-12-31 |
| 8.3 | Security fixes only | 2027-12-31 |
| 8.4 | Active | 2028-12-31 |
| 8.5 | Active; latest stable minor | 2029-12-31 |

All four use the same source and Composer package. The current requirement is `>=8.2 <8.6`, with `ext-curl` and `ext-json`. It deliberately excludes future unverified minor versions as well as EOL PHP branches. The default Docker environment uses PHP 8.5. The Python service and HTTP protocol do not need separate versions for each PHP minor.

This is a maintained release policy, not an automatic date-dependent Composer constraint. On a new stable PHP release, review its migration guide, add a pinned runtime to CI, fix incompatibilities, then widen the constraint in a release. When a branch reaches EOL, remove it from the supported matrix and update the minimum in a documented release. Existing published versions remain immutable. Refresh pinned images for security/patch updates; a test against one patch is not a test against every historical patch of that minor.

Framework support is separate: a Symfony/Laravel application must satisfy both its framework's PHP requirements and this package's requirements. Manual framework configuration examples are documented, but dedicated adapters and framework-version test matrices are not implemented.

## Reproduce the checks

Use the commands in [README](../README.md#test-layers). All execution is inside bounded Linux amd64 containers, with no external test network. HTTP peers share only a private Docker network. Model dependency acquisition is a separate network-enabled phase.

To select another PHP version, set `BRIDGE_PHP_IMAGE` to its exact pinned image from [the CI matrix](../.github/workflows/tests.yml), then run the same Compose commands. For example:

```sh
export BRIDGE_PHP_IMAGE='dunglas/frankenphp:1.12.7-php8.2-bookworm@sha256:15646d56650cc5a6c68275ac934eab8f8648a5e776690035f9278a93e0577088'
docker compose -f docker/compose.yaml --profile test config
docker compose -f docker/compose.yaml run --rm --no-deps php-tests
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration
```

PowerShell uses `$env:BRIDGE_PHP_IMAGE = '...'` instead of `export`. Set a disposable `BRIDGE_TOKEN` as described in the README first. Changing the image and rerunning `up` recreates this project's PHP service; do this only in a disposable test environment, not a live application.

CI checks the actual PHP minor before testing so a misconfigured image cannot silently test the default version four times. The stable required check `deterministic` depends on all four matrix jobs and rejects a failed, cancelled or skipped matrix. PHP contract/HTTP tests promote warnings and deprecations to exceptions.

## What the tests establish

Local verification on 2026-09-17 used the pinned FrankenPHP 1.12.7 images:

| Observed PHP runtime | PHP contract/HTTP checks | Reused-worker HTTP integration | Syntax |
| --- | --- | --- | --- |
| 8.2.33 | 65 passed | 12 jobs + boundary cases passed | 10 files passed |
| 8.3.33 | 65 passed | 12 jobs + boundary cases passed | 10 files passed |
| 8.4.25 | 65 passed | 12 jobs + boundary cases passed | 10 files passed |
| 8.5.10 | 65 passed | 12 jobs + boundary cases passed | 10 files passed |

The Python suite passed all 34 tests in 5.164 seconds in the same local Docker environment. This is a suite duration, not inference latency or a reproducible performance benchmark. The PHP 8.5 regression failed before the client fix and passed afterward. Consult the PR's current Actions checks for remote results; a local matrix pass does not imply a hosted CI run.

The optional real-model smoke tests also passed on PHP 8.5.10: two CLI cases and two FrankenPHP HTTP cases, with cached dependencies and no internet access. These model checks were not rerun on every PHP minor in this verification. Three PHP documentation snippets passed syntax validation and the fenced JSON examples parsed successfully; that is not a framework boot test.

| Layer | Scope and counting |
| --- | --- |
| Python | 34 lifecycle, validation and HTTP tests; includes process crashes, cancellation, timeouts, retention and allocation/exit-race regressions |
| PHP contracts + HTTP | 65 checks per PHP version; this **includes** the 35 contract checks, not 65 + 35 |
| FrankenPHP | 12 jobs, submitted by six concurrent test threads through a reused PHP worker, plus invalid-input, missing-job and multibyte boundary checks |
| PHP syntax | All 10 PHP source/example/test files |
| Optional ONNX | Two simple query cases via PHP CLI and two via FrankenPHP HTTP; smoke checks, not four independent quality datasets |

The default backend is deterministic word overlap (`lexical-demo-not-a-model`). Passing its tests proves plumbing/lifecycle behavior, not AI quality. The optional ONNX checks use the real pinned TinyBERT model and local inference. The model workflow is manually dispatched, not part of every PR's required checks.

The original prototype was not entirely developed with TDD. Review corrections used observed red-green regression tests. PHP 8.5 testing similarly reproduced a deprecated `curl_close()` call under the strict error handler before replacing it with object release. Passing tests alone do not prove a test-first history or complete correctness.

## What is not established

- No measured line/branch coverage percentage, formal security audit or production certification.
- No full Symfony kernel or Laravel application/Octane integration test, including configuration caching and multi-user authorization.
- No fresh consumer Composer-install smoke test or Packagist release yet; repository examples use a small local autoloader.
- No sustained load/soak benchmark, throughput claim, p50/p95/p99 latency, measured memory-per-job limit or CPU cost per inference.
- No representative ranking-quality evaluation (for example NDCG/MRR), multilingual accuracy evaluation or GPU benchmark.
- No verified native Windows/macOS service execution or ARM runtime; Docker tests target Linux amd64/Python 3.12.
- No durable delivery, restart recovery, automatic retry, idempotency or multi-instance result routing. Jobs and results live in memory.

Configured resources are **limits, not measurements**: ordinary services get one CPU and 512 MiB each; the model worker gets one CPU and 1536 MiB. Normal task concurrency defaults to two, total capacity to 128 and terminal retention to 300 seconds. The model profile sets concurrency to one and loads a model per job. None of these imply 128 simultaneous inference jobs or a specific requests-per-second rate.

Before calling this production-ready, test the actual application/framework combinations, measure cold and warm latency separately on documented hardware and workloads, exercise overload/restarts, validate tenant isolation, and decide whether the in-memory lifecycle is acceptable. The current result is an experimental, testable integration—not a replacement for a durable job system.
