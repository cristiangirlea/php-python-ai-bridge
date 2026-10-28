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

Framework support is separate: a Symfony/Laravel application must satisfy both its framework's PHP requirements and this package's requirements. The Laravel service provider and the Symfony bundle are tested with one locked version of each framework on PHP 8.5 (below); there is no framework-version matrix.

## Reproduce the checks

Use the commands in [README](../README.md#test-layers). All execution is inside bounded Linux amd64 containers, with no external test network. HTTP peers share only a private Docker network. Model dependency acquisition is a separate network-enabled phase.

The framework project's lock, `tests/php/frameworks/composer.lock`, keeps a copy of the library's own `extra`, `autoload` and `require`, which is what Laravel's package discovery reads, and `laravel-tests` fails when they differ from `composer.json`. A commit alone never makes the lock stale: the library's entry has no git reference and a fixed version. After changing one of those three keys, or to move a framework to a newer release (its name in place of the library's), lock it again with network in a throwaway container from the repository root, then rerun `php-frameworks-fetcher`:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work:ro" -v "$PWD/tests/php/frameworks:/out" -e COMPOSER_HOME=/tmp/composer --entrypoint sh \
  composer:2@sha256:af98f42dfff7c68ba8d53c2164fd9fde1087b7d449514baa38c418b1f6bc4bac -ec \
  'mkdir /tmp/p && cp /out/composer.json /out/composer.lock /tmp/p && cd /tmp/p && composer update cristiangirlea/php-python-ai-bridge --no-install --no-scripts --no-plugins && cp composer.lock /out/'
```

`requirements-dev.lock` pins the wheels `py-analysis` installs for the versions in `requirements-dev.txt`, one hash per wheel for CPython 3.12 on Linux x86_64. To move mypy or ruff to a newer release, change `requirements-dev.txt`, then resolve and hash the wheel set again with network in a throwaway container from the repository root, review the diff, and rerun `py-dev-fetcher` and `py-analysis`:

```sh
docker run --rm -v "$PWD:/work" -w /work python:3.12-bookworm@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970 sh -ec '
python -m pip download -q --disable-pip-version-check --only-binary=:all: --dest /tmp/w -r requirements-dev.txt
{ echo "# Resolved wheel set for CPython 3.12, Linux x86_64. No source distributions."
  for f in /tmp/w/*.whl; do n=$(basename "$f" | cut -d- -f1 | tr _ - | tr A-Z a-z); v=$(basename "$f" | cut -d- -f2)
    echo "$n==$v --hash=sha256:$(sha256sum "$f" | cut -d" " -f1)"; done; } > requirements-dev.lock'
```

A package both locks pin (today `typing-extensions`) must have the same version in each, since `py-analysis` installs both.

To select another PHP version, set `BRIDGE_PHP_IMAGE` to its exact pinned image from [the CI matrix](../.github/workflows/tests.yml), then run the same Compose commands. For example:

```sh
export BRIDGE_PHP_IMAGE='dunglas/frankenphp:1.12.7-php8.2-bookworm@sha256:15646d56650cc5a6c68275ac934eab8f8648a5e776690035f9278a93e0577088'
docker compose -f docker/compose.yaml --profile test config
docker compose -f docker/compose.yaml run --rm --no-deps php-tests
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile tokens-worker
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration
```

PowerShell uses `$env:BRIDGE_PHP_IMAGE = '...'` instead of `export`. Set a disposable `BRIDGE_TOKEN` as described in the README first. Changing the image and rerunning `up` recreates this project's PHP service; do this only in a disposable test environment, not a live application.

To run the integration suite against the worker image instead of the checkout, build it under the published name (`docker build -t ghcr.io/cristiangirlea/php-python-ai-bridge-worker:local -f docker/Dockerfile.worker .`), then with `BRIDGE_IMAGE_TAG=local` and `BRIDGE_IMAGE_PULL_POLICY=never` set: `docker compose -f docker/compose.yaml --profile release up -d --wait worker-image`, `docker compose -f docker/compose.yaml run -d --no-deps --name frankenphp-image -e BRIDGE_URL=http://worker-image:8090 frankenphp` and `docker compose -f docker/compose.yaml run --rm --no-deps -e FRANKENPHP_URL=http://frankenphp-image:8080 -e WORKER_URL=http://worker-image:8090 integration`; remove `frankenphp-image` with `docker rm -f` afterwards.

### CI runners

CI runs on GitHub's hosted runners, `ubuntu-latest` for every job, because the repository is public: GitHub does not meter Actions minutes for public repositories, and a fork's pull request then runs its own code, workflow edits included, on one of GitHub's disposable runners and nowhere else. No workflow names a self-hosted runner and no repository variable chooses one; keep requiring approval for outside contributors' workflow runs until no self-hosted runner is registered for the repository at all, since a fork's pull request can edit the workflows and the test alike. `tests/python/test_workflows.py` fails if a `runs-on` expression, a runner variable, a self-hosted label or a local action under `.github/actions` comes back. GitHub's runner images ship the Docker Compose plugin, so the jobs drive compose directly. The measurements below name the runner they ran on.

Every job starts on a fresh runner with an empty image store and pulls `python:3.12-bookworm` and the FrankenPHP images, and the MCP job `composer:2`, from Docker Hub, which limits unauthenticated pulls per IP; GitHub's runners share address ranges, so a pull that fails with a rate-limit error is a rerun, not a regression. The model workflow keeps both model workers up, each allowed 1536 MiB with its wheels in tmpfs, beside FrankenPHP, the MCP server, the demo worker and one client, whose limits add up to about 5 GiB; a Docker daemon that reproduces that workflow locally needs about 6 GiB.

CI checks the actual PHP minor before testing so a misconfigured image cannot silently test the default version four times. The stable required check `deterministic` depends on all four matrix jobs and on the MCP server job, which also runs mypy and ruff, the consumer install, PHPStan, the PSR-18 transport checks, the Laravel and Symfony tests and the TLS example, and on the `images` job, which builds both worker images for linux/amd64 and starts each, and rejects any of them failed, cancelled or skipped. `publish.yml` pushes those images for amd64 and arm64 on a `v*` tag only. PHP contract/HTTP tests promote warnings and deprecations to exceptions. Dependabot proposes weekly updates through `.github/dependabot.yml` for three ecosystems: the GitHub Actions pins, the pip wheel sets (`requirements-model.*`, `requirements-mcp.*`) and `composer.lock`.

## What the tests establish

PHP branch support was verified locally on 2026-09-17 on PHP 8.2.33, 8.3.33, 8.4.25 and 8.5.10 with the pinned FrankenPHP 1.12.7 images. For every pull request and every push to `main`, CI runs the Python worker, PHP contract/HTTP, FrankenPHP and syntax layers below on each of the four branches, and the MCP server layer once in its own job, which does not depend on PHP. The optional ONNX layer runs only on PHP 8.5, weekly, on pull requests that touch the model path, and on demand. The PHP and MCP counts below were observed locally on 2026-09-27 on the default PHP 8.5.10 image; the worker count reflects the current suite. A local pass does not imply a hosted CI run, so consult the PR's Actions checks for remote results.

| Layer | Scope and counting |
| --- | --- |
| Python worker | 423 tests: lifecycle, validation and HTTP, separate active and retained-result limits, their retry delays and overshoot, restored results and the new gauges, the per-token capacity cap (one token refused at its cap with its own `Retry-After` while another submits, cancelled jobs holding it until they expire, a repeated key answered there, the total still enforced with the `Retry-After` of whichever limit applies, jobs restored from the table counted until they expire, the default without an owner cap, and every out-of-range value refused at start), the optional JSON log (every line parses and holds only its event's keys, an input, token, header, query string, idempotency key and a failing task's exception text never reach it, `off` writes nothing and a bad `BRIDGE_LOG` stops the start, the route templates, the 401 without an owner, the 429 and the draining 503 with their owner, no line for a reply not written in full or a busy 503 not sent, one `drain` line before `stop` when a second signal meets a held drain thread and none after it when the stop has stopped waiting, a HEAD refused with headers only, nothing timed or built with `off`, consistent job timings, and a stderr that fails or blocks costing a request nothing), the drain (submissions refused with 503, queued jobs failed at once, running ones finished within `BRIDGE_DRAIN_SECONDS`, a second signal), `/readyz` and its reasons for any configured token, the 503 for a full connection budget and the 431 for an oversized request line or header block, per-client tokens (each source and every way it is refused at start, a job and its idempotency key scoped to the submitting token, through the job table and a table from before owners, the `owner` metrics series, and no token in an error, the metrics, the table, a child's environment or a start message), the job table in SQLite (a store reopened on its file fails unfinished jobs as `service_restarted`, serves retained results, deletes expired rows from the file and never holds an input; a submission the table refuses is a 429, a later write it refuses leaves the job served, and a key whose row outlived a failed delete runs again) and idempotency keys, process crashes, cancellation, timeouts, retention, the Retry-After a full or closing store gives and its header on every 429, allocation/exit races, the warm pool (that successive jobs share a child in which no live container or frame holds a reference to a finished job's input or result, after a success, a failure and real task payloads, with the detector checked against a planted needle; that a cancelled, timed-out or crashed child is replaced; that a child is recycled after its share of jobs; that reads, submissions and cancellations answer at once while a child is being started, that a child whose start ends after the store closed does not outlive it, and that a job assigned to or waiting for a live child does not fail when no replacement can start; that a job whose child died before taking it is tried once more on another, only once, that a job sent but not yet taken can still be cancelled or time out, and that a job whose child crashed after taking it is not; that a child, idle or running a job, ignores the SIGINT a terminal's Ctrl+C sends the whole process group; that the service token is absent from a child's environment and from its environment block when the service started it; that a message for an earlier job lands nowhere; that a child that never reports ready is replaced and that children dying at start are not restarted in a loop; and that a cancellation does not hold up reads), redaction rules including IPv6, lower-case IBANs and email addresses in any script and their speed on hostile input, NER aggregation, name joining, the second title-cased reading, the uncased model's reading of the whole text and the merge driven by stubs, the fetcher's pins and the model directories an ONNX worker checks for before it starts, batched cross-encoder scoring, the NER measurement's sample, scoring and WNUT-17 reader, the load measurement's settings, percentiles, error counting and table, the ranking measurement's metrics, BM25, dataset reader and fit to the worker's limits, the public datasets' pins and licence gate, the workflows' runners, action pins and checkout credentials, both Claude Code hooks (the post-edit rules, and the pre-call push guard through its own cases), progress bounds, the service's reading of its BRIDGE_* settings (defaults, every variable, malformed and out-of-range values, a state directory that cannot hold the table), and the model-name table every result reports from |
| MCP server | 125 tests, offline with hash-pinned wheels: the protocol client against an in-process worker, every tool through the SDK's in-memory client with test tasks enabled, the entry point as a subprocess, result validators on synthetic data, the index builder (chunking, the format and each corruption it refuses, building against the worker, caching, its command line) `bridge_search` (the model gate, path confinement, reloads, rerank on and off), the reachability of every answer in the LLM evaluation, that a default search sees only part of its corpus and that its two-step question takes two steps, that the demo worker reaches none of the semantic evaluation's answers, that a tool waits out a full worker for its Retry-After within the call's budget or with its own backoff without one, before each of a search's jobs and within one budget, and says so when it stays full, the evaluation runner's command, isolation checks, scoring and per-backend launcher and index check without starting `claude`, a check that the server's model names match the worker's, that every path parameter says where it resolves and every position in a result says what it counts, and a check that every timing claim in a tool description is a measured median |
| PHP contracts + HTTP | 192 checks per PHP version, a full fault-worker's Retry-After read through the client among them and a two-token `tokens-worker` where another token's read and cancel are 404 and the same idempotency key is two jobs (on which `tests/integration.py` then fills one token's `BRIDGE_OWNER_CAPACITY` of 8 while the other still submits up to the worker's retained-result capacity of 12); this **includes** the 148 contract checks (Retry-After from a loopback peer, present, absent, malformed and repeated, and a header block over 16 KiB refused), not 192 + 148; the contract checks include a fake transport's view of each request, the wait deadline across a transport failure, and that no file in `src/` outside `Laravel/` and `Symfony/` names a framework |
| PSR-18 transport | The 148 contract checks plus 30 through `Psr18Transport` and in-repo fakes of the PSR-7, PSR-17 and PSR-18 interfaces (`tests/php/psr18_fakes.php`): a round trip with its method, URL, headers and body, a body at the 262144-byte cap and one over it read one byte past the cap and no further, five redirect statuses refused unread, a non-JSON or missing content type, malformed JSON, a 429's Retry-After from one header, a repeated one and none, a client exception and a body read failure as `transport_error` without their text; 178 checks by `php-psr18-tests` with `.cache/php-vendor` on the include path, once, in the MCP server job; `php-tests` skips them with one printed line; no real Symfony HttpClient or Guzzle runs |
| FrankenPHP | 12 jobs submitted by six concurrent test threads through a reused PHP worker, a 512-document `top_k` request, embedding and redaction round trips, and invalid-input, missing-job and multibyte boundary checks; then, directly against `tokens-worker`, the same ownership checks as the PHP layer and the `owner` metrics |
| PHP syntax | All 25 PHP source/example/test files |
| Consumer install | `composer validate --strict` on the library, then a throwaway consumer project in tmpfs, with Packagist disabled and the platform pinned to PHP 8.2.0, installs its `git archive` zip from an `artifact` repository (or from a path repository in a git worktree whose git directory is outside the mount; the archive is built from the committed `HEAD`, so an uncommitted change is not what it tests), checks that `vendor/` holds `src/Client.php` and `docs/` and not `worker/`, `tests/` or `docker/`, that the library brought no other vendor into `vendor/` (so no framework), and autoloads `PhpAiBridge\Client`; runs once, in the MCP server job, with no network |
| Packagist install | The model workflow's `packagist` job installs the newest `^0.4` release on Packagist on manual dispatch and each `v*` tag, then checks the client autoloads and its document limit |
| PHPStan | Level max over `src`, `examples` and `tests/php` with `phpstan.neon`, PHPStan 2.3.1 from `composer.lock` installed into `.cache/php-vendor` by `php-fetcher` and run offline by `php-analysis`, with the frameworks' autoloader from `.cache/php-frameworks-vendor` as its bootstrap file so the provider, the bundle and their tests are analysed against Laravel and Symfony; `phpstan-baseline.neon` holds 9 findings in examples and tests, each with its reason, and none in `src`; runs once, in the MCP server job |
| Python static analysis | `ruff check` over `worker`, `mcp_server`, `scripts` and `tests` with pycodestyle's errors and warnings, pyflakes, bugbear and the import order (`E`, `W`, `F`, `B`, `I`, but not `E501`, line length, or `B905`, `zip()` without `strict=`), then `mypy --strict` over `worker` and `mcp_server` with the MCP SDK's own types; mypy 2.4.0 and ruff 0.17.2 from `requirements-dev.lock`, downloaded by `py-dev-fetcher` and installed offline with the MCP server's wheels into a tmpfs by `py-analysis`, configured in `pyproject.toml`; `numpy`, `onnxruntime` and `tokenizers` are not installed there and are typed as unknown, and three `type: ignore` comments remain, each with its reason; runs once, in the MCP server job |
| Laravel and Symfony | `php-frameworks-fetcher` installs what `tests/php/frameworks/composer.lock` pins (Laravel 13.38.0, Symfony 8.0.15, resolved for PHP 8.4 and later) into `.cache/php-frameworks-vendor`, with the library linked from the checkout by a path repository, as Composer installs a package; then, offline, against the real `worker`: `laravel-tests`, 94 checks, boots an `Illuminate\Foundation\Application` with no skeleton whose provider comes from package discovery, publishes the configuration with `vendor:publish`, resolves one client twice, reranks, boots again from a configuration cache with the environment changed, and refuses a missing, empty or malformed URL, a missing, empty or short token and a timeout of 0, 300001 or not a number, naming the setting and never quoting the token; `symfony-tests`, 96 checks, boots a bare `Kernel` with only the bundle and debug off, resolves the client and an autowired service, reranks, loads the dumped container in a second kernel, and refuses the same cases, plus an empty URL and a zero timeout written in the configuration at build time; once, on PHP 8.5, in the MCP server job; no full application, FrameworkBundle, Octane, Messenger or queue worker runs |
| TLS proxy | `tls-check` runs `tests/php/tls.php` against `tls-proxy`, Caddy with `tls internal` in front of the real `worker`: with the system CA store emptied by a tmpfs, so the client trusts only the example root: 7 checks with `curl.cainfo` set to that root (that setting itself, the empty system store, a rerank over HTTPS with the certificate verified, 401 for a wrong token, the worker's own 401 for a request with no `Authorization` header, Caddy's 400 for plain HTTP on the TLS port, no listener on port 80) and 3 with no CA configured (no example CA set, a TLS handshake without verification succeeds, the client fails as `transport_error`); once, on PHP 8.5, in the MCP server job |
| Optional ONNX | Through both the PHP CLI and FrankenPHP HTTP: 2 rerank cases, a 70-document rerank across three batches with its relevant document in the second, a parity check that three documents score within 1e-3 alone and inside that set, 1 embedding and 3 redaction cases: a name, an email address and a place; a sentence whose lower-case person, place and organisation only the uncased model finds, which also checks its label order; and clean text, which must produce no spans; and the ONNX-backed MCP server end to end, every tool through compose's `mcp-model` service over stdio, including a search over an index the embedding model built, with a query worded unlike the note it must find, and the calls for every question in the semantic evaluation, whose answers the models must all reach; smoke checks, not quality datasets |

Test durations reported by the runners are suite durations, not inference latency or a reproducible performance benchmark.

The optional real-model smoke tests run on the default PHP 8.5 image with cached dependencies and no internet access during inference. They were not rerun on every PHP minor. On 2026-09-17, three PHP documentation snippets passed syntax validation and the fenced JSON examples parsed successfully; that check has not been repeated since, and it is not a framework boot test.

The default backend is deterministic word overlap (`lexical-demo-not-a-model`). Passing its tests proves plumbing/lifecycle behavior, not AI quality. The embedding and redaction demos are `hashing-bow-not-a-model` and `rules-only-not-a-model`: the first proves the contract only, while the second runs the real checksum and pattern rules with no model. The optional ONNX checks use the four pinned, hash-verified models (the TinyBERT cross-encoder, MiniLM embeddings and the int8 conversions of the cased and uncased BERT NER models) with local inference. The model workflow runs weekly, on pull requests that touch the model path, and on demand; it is not a required check.

The original prototype was not entirely developed with TDD. Review corrections used observed red-green regression tests. PHP 8.5 testing similarly reproduced a deprecated `curl_close()` call under the strict error handler before replacing it with object release. Passing tests alone do not prove a test-first history or complete correctness.

## What is not established

- No measured line/branch coverage percentage, formal security audit or production certification.
- No full Symfony application (FrameworkBundle, Flex, Messenger, Runtime) or Laravel skeleton/Octane/queue test, no multi-user authorization test, and no framework run on PHP 8.2, 8.3 or 8.4 or on another framework version than the locked one.
- No install through the VCS repository recipe (an unreleased commit) has been exercised; the repository's own examples use a small local autoloader.
- No throughput or tail-latency figure beyond one run per row on one host, with the client and the worker on the same machine and no PHP client or FrankenPHP in the path, under [Measured throughput and latency under load](#measured-throughput-and-latency-under-load-informational); no measured memory-per-job limit or CPU cost per inference. The other timing figures are the medians of sequential jobs below, warm after one cold first job. The peak of a child holding all four ONNX models during the worst-case rerank batch is measured once, on one machine, under [Measured model worker memory](#measured-model-worker-memory-informational); the model worker's 1536 MiB is a limit, which grows with `BRIDGE_CONCURRENCY` children each holding its own copies.
- Ranking quality is measured on one public dataset, ArguAna, a task unlike the reranker's training; none on short-query search and none in another language. NER quality on real data is one public set, WNUT-17's English social media text, with four of its six types merged into three labels; no other domain or language, and no GPU benchmark. The invented-sample NER figures below come from 48 sentences by the same author as the development set used to tune the redact task.
- No repeated or semantic LLM evaluation. The one recorded run below is a single session per question per model on the demo worker, so it shows that the tools can be used, not a success rate or retrieval quality.
- No real certificate and no mutual TLS: the TLS example signs with Caddy's local CA, runs on one Docker network rather than two hosts, and checks no client certificate.
- No type checking of `scripts/` and `tests/`: mypy covers the worker and the MCP server only, and ruff all four.
- No verified native Windows/macOS service execution or ARM runtime; Docker tests target Linux amd64/Python 3.12.
- No durable delivery, replay or automatic retry, and no multi-instance result routing. With `BRIDGE_STATE_DIR` set the job table (never inputs) survives a restart, so retained results and idempotency keys are answered and unfinished jobs are reported as `service_restarted`; without it, jobs and results live in memory.

Configured resources are **limits, not measurements**: ordinary services, including the MCP server's own `mcp-worker`, get one CPU and 512 MiB each; the model worker gets one CPU and 1536 MiB. Normal task concurrency defaults to two, total capacity to 128 and terminal retention to 300 seconds; the MCP workers keep finished jobs for 30 seconds, because their one client reads each result as its job ends. Every worker keeps as many child processes as its concurrency and replaces one after a cancellation, a deadline, a crash or `BRIDGE_WORKER_MAX_JOBS` jobs (default 1000). The model profile sets concurrency to one; its one child loads every model it needs when it starts, before it reports ready, so the first job pays no load, and it keeps them and scores rerank documents 32 pairs per forward pass. None of these imply 128 simultaneous inference jobs or a specific requests-per-second rate.

## Measured latency (informational)

Measured 2026-10-15 UTC locally on the warm worker pool, branch `feat/warm-worker-pool`, on Windows 11 with Docker Desktop, CPU AMD Ryzen 9 9950X 16-Core Processor, with the compose limits: model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1; worker cpus=1, mem_limit=512m. Each backend's first row is the first job its worker ever served, which imports the child's dependencies and, on the ONNX backend, loads the cross-encoder. Every other row is N=5 sequential jobs after one job of that case, so each child already holds that case's model; where that one job was the first to load a model, the note says what it took. Wall time runs from `POST /v1/jobs` to the first `GET` showing a terminal state, polled every 0.05 s, so figures carry up to 0.05 s of polling, and 0.05 s is the floor a job can show.

| Backend | Case | N | min s | p50 s | max s | Note |
| --- | --- | --- | --- | --- | --- | --- |
| onnx | first job after start: rerank 32 documents of ~40 words | 1 | 0.79 | 0.79 | 0.79 | cold; the child imports its dependencies and loads the cross-encoder |
| onnx | rerank 32 documents of ~40 words | 5 | 0.05 | 0.05 | 0.05 | warm; 1 batch of 32 |
| onnx | rerank 512 documents of ~40 words | 5 | 0.21 | 0.21 | 0.21 | warm; 16 batches of 32 |
| onnx | rerank 32 documents of 6000 characters | 5 | 0.26 | 0.27 | 0.42 | warm; 1 full batch of 32 at the 512-token limit, the worst case |
| onnx | embed 32 texts of ~40 words | 5 | 0.16 | 0.16 | 0.22 | warm; 1 forward pass; the first job, loading the embedding model, took 0.79 s |
| onnx | redact 2000 characters, PER+ORG+LOC | 5 | 0.63 | 0.68 | 0.69 | warm; rules plus windows of 512; the first job, loading both NER models, took 2.31 s |
| lexical | first job after start: rerank 32 documents of ~40 words | 1 | 0.05 | 0.05 | 0.05 | cold; the child imports its dependencies |
| lexical | rerank 32 documents of ~40 words | 5 | 0.05 | 0.05 | 0.05 | warm process, no model |
| lexical | rerank 512 documents of ~40 words | 5 | 0.05 | 0.05 | 0.05 | warm process, no model |
| lexical | rerank 32 documents of 6000 characters | 5 | 0.05 | 0.05 | 0.05 | warm process, no model |
| lexical | embed 32 texts of ~40 words | 5 | 0.06 | 0.06 | 0.06 | warm process, no model |
| lexical | redact 2000 characters, PER+ORG+LOC | 5 | 0.05 | 0.05 | 0.05 | warm process, no model |

Before the pool, every job was cold by design: a fresh process imported its dependencies and, on the ONNX backend, loaded the model before any work. The same machine measured that worker, `main` at `e0f49b7`, the same day with the same benchmark: ONNX medians of 1.00 s for 32 documents, 1.24 s for 512, 1.40 s for the worst-case batch, 1.50 s for embedding and 2.94 s for redaction, and 0.42-0.48 s on every lexical row. The warm rows above are therefore what scoring itself costs on one CPU: 512 documents take four times what 32 do, the worst-case batch ran within the model worker's memory limit at 0.27 s with the cross-encoder the only model loaded, and a lexical job is all polling. A first job costs less than a cold job did (0.79 s against 1.00 s for the first rerank) because the child is started with the service and has done its imports before the job arrives; the lexical child's start is hidden in the 0.05 s floor for the same reason. The model smoke workflow measured the cold worker on GitHub's runners too: 0.74-0.90 s for 32 documents, 1.00-1.20 s for 512, 1.10-1.40 s for embedding and, since pull request #19 loads the uncased NER model as well, 3.40 s for redaction on an AMD EPYC 7763 (run 36902794112 at `e49e388`) against 2.76 s locally; those figures now describe a worker's first job of each kind, not every job. 8192 lower-case characters, which every NER reading covers, took 5.8 s cold locally.

These are worker time only; the PHP client, FrankenPHP and the MCP server add their own polling and HTTP overhead. The benchmark asserts that every job succeeds with progress ending on the total, and a failure fails the smoke run; it asserts nothing about speed. The workflow restarts the model worker before the benchmark so that its first job is cold; a worker that has served a job of a kind shows the warm figure for that kind from then on, until the child is replaced after a cancellation, a deadline, a crash or `BRIDGE_WORKER_MAX_JOBS` jobs, when the next job of each kind pays the first-job cost again.

Every model smoke run prints a fresh table to its job summary. To reproduce locally after acquiring the models:

```sh
docker compose -f docker/compose.yaml up -d worker model-worker
docker compose -f docker/compose.yaml run --rm --no-deps -T model-bench
docker compose -f docker/compose.yaml --profile model down
```

## Measured model worker memory (informational)

Measured 2026-10-20 UTC locally at `5d3ceb3` with this change's `BENCH_ORDER=worst-last`, on Windows 11 with Docker Desktop 29.8.2 (cgroup v2), CPU AMD Ryzen 9 9950X 16-Core Processor, with the compose limits: model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1, `onnx` backend. That order runs embed, redact with PER, ORG and LOC, the two smaller rerank cases and the worst rerank batch (32 documents of 6000 characters, every one at the 512-token limit) last, so the one child holds the embedding model, both NER models and the cross-encoder, with every arena ONNX Runtime has grown for them, when the widest batch runs. Each run starts from a recreated container. The container peak is the cgroup's `memory.peak`, read after the benchmark; the child's peak is its `VmHWM`; `docker stats --no-stream` was sampled throughout, and shows the usage during the worst batch.

| Run | Container peak MiB | Child peak MiB | `docker stats` in the worst batch |
| --- | --- | --- | --- |
| worst-last, N=5 | 1464 | 1286 | 1.425 GiB |
| worst-last, N=20 | 1460 | 1273 | 1.42 GiB, flat over the 20 batches |
| default order, N=5 | 1446 | 1268 | not sampled in that batch |

The container peak carries more than the child: the wheels the model worker installs into its `/deps` tmpfs are charged to the cgroup as shared memory (167 MiB), and the server and multiprocessing's resource tracker take another 45 MiB or so, so the idle container uses 222 MiB with the child at 29 MiB. On the worst-last run the child grew to 240 MiB with the embedding model, 776 MiB with both NER models and 822 MiB with the cross-encoder, and the worst batch added about 465 MiB of arena on top; in the default order the same batch, with only the cross-encoder loaded, took the child from 108 to 572 MiB. The arena does not shrink but did not grow over 20 repeated worst batches either. No run reached the limit: `memory.events` counted no `high`, `max` or `oom` event and swap stayed at zero, leaving 72 MiB, about 5 %, between the highest peak and 1536 MiB.

Each child loads its own copies, so the limit's budget is about 180 MiB for the container itself plus up to 1290 MiB per child at this peak: 1536 MiB allows `BRIDGE_CONCURRENCY=1` on the `onnx` backend and no more, and two children would need at most about 2.7 GiB (an upper bound: the `/deps` libraries are charged once) before any margin. The limit in `docker/compose.yaml` stays as it is: it holds the configured concurrency with every model and the worst rerank batch together, and a higher concurrency must raise it with it. Only the benchmark's inputs were measured, so a wider embed or redact batch on top of these, or another CPU, may sit closer to the limit. To reproduce after acquiring the models, recreating the worker first so that the peak starts at zero (on cgroup v1 the file is `/sys/fs/cgroup/memory/memory.max_usage_in_bytes`). The fourth command prints the child's `VmHWM`: a child is a process whose command line runs multiprocessing's `spawn_main`, one per `BRIDGE_CONCURRENCY`; the others are the init, the server and the resource tracker:

```sh
docker compose -f docker/compose.yaml up -d --force-recreate worker model-worker
docker compose -f docker/compose.yaml run --rm --no-deps -T -e BENCH_ORDER=worst-last model-bench
docker exec "$(docker compose -f docker/compose.yaml ps -q model-worker)" cat /sys/fs/cgroup/memory.peak
docker compose -f docker/compose.yaml exec model-worker sh -c 'for p in /proc/[0-9]*; do grep -q "spawn_mai[n]" $p/cmdline 2>/dev/null && grep -H VmHWM $p/status; done'
docker compose -f docker/compose.yaml --profile model down
```

## Measured throughput and latency under load (informational)

`scripts/load_measure.py` drives one worker from N concurrent threads for a stated time. Each thread submits a job, polls it every 0.05 s until it is terminal and submits the next; at the end of the time it finishes the job in hand. The mix cycles scripts/benchmark.py's inputs, rerank 32 documents of ~40 words, embed 32 texts and redact 2000 characters with PER, ORG and LOC, with each thread starting on a different task. Latency runs from `POST /v1/jobs` to the first `GET` showing a terminal state, for the jobs that succeeded, so it carries up to 0.05 s of polling. A 429 is answered by waiting its `Retry-After` within the run, and that wait is in no latency. Jobs/s is succeeded jobs over the level's wall time. The driver waits for the jobs from the previous level to leave retention when levels share a container; the runs below instead recreate the target before every level, so each starts from an empty job table.

Measured 2026-10-28 UTC, commit `a1198de` plus this change, runner: local, Windows 11 Pro with Docker Desktop 29.8.2, CPU: AMD Ryzen 9 9950X 16-Core Processor.

Both client and worker ran on the same Windows host in separate containers limited to one CPU. These are single runs and do not include network or PHP overhead.

**With the limits in force.** Each level ran against a freshly recreated target container with the defaults: active capacity 128, result capacity 384 and retention 300 s. The lexical worker has two children and the ONNX worker one. The limits labels and tables are copied from the load driver's output.

Target: `lexical` at http://worker:8090; limits: worker cpus=1, mem_limit=512m, BRIDGE_CONCURRENCY=2; active capacity 128 jobs, result capacity 384 jobs and retention 300 s, the defaults.

| Case | Concurrency | Duration s | Jobs completed | Jobs/s | p50 s | p95 s | p99 s | max s | 429s | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mix | 1 | 120.0 | 384 | 3.20 | 0.06 | 0.06 | 0.06 | 0.22 | 1 | 0 |
| mix: rerank | 1 | 120.0 | 128 | 1.07 | 0.06 | 0.06 | 0.06 | 0.22 | 1 | 0 |
| mix: embed | 1 | 120.0 | 128 | 1.07 | 0.06 | 0.06 | 0.06 | 0.07 | 0 | 0 |
| mix: redact | 1 | 120.0 | 128 | 1.07 | 0.06 | 0.06 | 0.06 | 0.06 | 0 | 0 |
| mix | 4 | 120.0 | 385 | 3.21 | 0.06 | 0.07 | 0.32 | 0.33 | 4 | 0 |
| mix: rerank | 4 | 120.0 | 128 | 1.07 | 0.06 | 0.07 | 0.32 | 0.33 | 2 | 0 |
| mix: embed | 4 | 120.0 | 129 | 1.07 | 0.06 | 0.07 | 0.07 | 0.32 | 0 | 0 |
| mix: redact | 4 | 120.0 | 128 | 1.07 | 0.06 | 0.07 | 0.07 | 0.33 | 2 | 0 |
| mix | 16 | 120.0 | 397 | 3.31 | 0.18 | 0.25 | 0.34 | 0.35 | 16 | 0 |
| mix: rerank | 16 | 120.0 | 133 | 1.11 | 0.18 | 0.27 | 0.34 | 0.34 | 5 | 0 |
| mix: embed | 16 | 120.0 | 132 | 1.10 | 0.16 | 0.25 | 0.32 | 0.34 | 6 | 0 |
| mix: redact | 16 | 120.0 | 132 | 1.10 | 0.18 | 0.24 | 0.34 | 0.35 | 5 | 0 |

Target: `onnx` at http://model-worker:8090; limits: model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1; active capacity 128 jobs, result capacity 384 jobs and retention 300 s, the defaults.

| Case | Concurrency | Duration s | Jobs completed | Jobs/s | p50 s | p95 s | p99 s | max s | 429s | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mix | 1 | 600.0 | 768 | 1.28 | 0.22 | 0.85 | 1.08 | 4.29 | 3 | 0 |
| mix: rerank | 1 | 600.0 | 255 | 0.42 | 0.05 | 0.06 | 0.06 | 4.29 | 2 | 0 |
| mix: embed | 1 | 600.0 | 257 | 0.43 | 0.22 | 0.27 | 0.33 | 0.40 | 0 | 0 |
| mix: redact | 1 | 600.0 | 256 | 0.43 | 0.79 | 1.02 | 1.13 | 1.46 | 1 | 0 |
| mix | 4 | 600.0 | 774 | 1.29 | 1.21 | 2.63 | 3.54 | 5.75 | 159 | 0 |
| mix: rerank | 4 | 600.0 | 262 | 0.44 | 1.01 | 1.97 | 3.16 | 5.75 | 49 | 0 |
| mix: embed | 4 | 600.0 | 262 | 0.44 | 1.17 | 2.34 | 2.79 | 4.78 | 49 | 0 |
| mix: redact | 4 | 600.0 | 250 | 0.42 | 1.72 | 2.90 | 3.62 | 5.72 | 61 | 0 |

Every level reached the retained-result admission limit and received 429s, with no errors. The lexical runs fill the result store within the 120 s window; jobs already accepted can finish beyond its 384-result admission threshold. Both ONNX runs are now limited by admission at about 384 jobs per 300 s once retained results fill, a sustained budget of 1.28 jobs/s. More client threads add queueing rather than removing that limit. The historical table below records 3.46 jobs/s for one ONNX child with one client thread and retention 30 s, without a 429; the default admission budget is now below that processing rate.

**Historical run with admission relaxed.** Measured 2026-10-21 on main at e77310f, before active and retained limits were separated. To see the work rather than the admission limit, the same runs used one-off containers of the same services, with the same CPU and memory limits and only admission changed: the lexical worker with `BRIDGE_CAPACITY=1024` and `BRIDGE_RETENTION_SECONDS=5` (a limit of about 205 jobs/s), the ONNX worker with `BRIDGE_RETENTION_SECONDS=30`, the value the MCP workers in compose run with (about 4.3 jobs/s). No run reached its limit: there was no 429.

| Target | Case | Concurrency | Duration s | Jobs completed | Jobs/s | p50 s | p95 s | p99 s | max s | 429s | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| lexical | mix | 1 | 120.1 | 2196 | 18.29 | 0.05 | 0.06 | 0.06 | 0.07 | 0 | 0 |
| lexical | mix | 4 | 120.0 | 8207 | 68.38 | 0.06 | 0.06 | 0.07 | 0.14 | 0 | 0 |
| lexical | mix | 16 | 120.1 | 13814 | 115.01 | 0.13 | 0.21 | 0.32 | 0.46 | 0 | 0 |
| onnx | mix | 1 | 600.0 | 2074 | 3.46 | 0.16 | 0.68 | 0.73 | 2.75 | 0 | 0 |
| onnx | mix: rerank | 1 | 600.0 | 692 | 1.15 | 0.05 | 0.05 | 0.05 | 0.73 | 0 | 0 |
| onnx | mix: embed | 1 | 600.0 | 691 | 1.15 | 0.16 | 0.22 | 0.22 | 1.54 | 0 | 0 |
| onnx | mix: redact | 1 | 600.0 | 691 | 1.15 | 0.63 | 0.68 | 0.79 | 2.75 | 0 | 0 |
| onnx | mix | 4 | 601.0 | 2216 | 3.69 | 0.97 | 2.06 | 2.58 | 4.36 | 0 | 0 |
| onnx | mix: rerank | 4 | 601.0 | 739 | 1.23 | 0.80 | 1.64 | 2.08 | 3.86 | 0 | 0 |
| onnx | mix: embed | 4 | 601.0 | 738 | 1.23 | 0.91 | 1.68 | 2.15 | 2.64 | 0 | 0 |
| onnx | mix: redact | 4 | 601.0 | 739 | 1.23 | 1.41 | 2.44 | 2.99 | 4.36 | 0 | 0 |

A lexical job is mostly the 0.05 s polling floor, so one thread manages about 18 jobs/s and four about 68. Sixteen threads reached 115 jobs/s with the median at 0.13 s; whether the worker's one CPU or the client's set that ceiling was not measured. The ONNX worker's one child runs the jobs one at a time, about 3.5 jobs/s for this mix, which [the latency table](#measured-latency-informational)'s warm medians, polling included, predict. Four threads add only the polling gaps the child no longer waits through, and every job waits behind the other three, so the median rises to 0.97 s and the p99 to 2.58 s. The worker's own `/metrics` counted the same 4290 succeeded jobs as the client. A child is replaced after `BRIDGE_WORKER_MAX_JOBS` jobs (1000), so over this soak the next job of each kind reloaded its model about four times; those reloads, like the first loads, show only in the max column. The worker exposes no retirement count, so that number comes from the setting, not from an observation.

**Memory after the historical ONNX soaks** (two levels of 600 s and the wait between them, 25 and 21 minutes), read before the container stopped: `docker stats --no-stream` at that moment, the cgroup's `memory.peak` over the container's life, and the `VmHWM` of the child serving at the end. `VmHWM` is per process: on the first run one child served every job, on the second the last of several, after about 290 jobs, so only the container peak covers that whole run. No run reached the limit: `memory.events` counted no `high`, `max` or `oom` event, and swap stayed at zero.

| Run | `docker stats` after the soak | Container peak MiB | Peak of the child serving at the end MiB |
| --- | --- | --- | --- |
| limits in force, 512 jobs | 993.9 MiB / 1.5 GiB | 1008 | 794 |
| retention 30 s, 4290 jobs | 1016 MiB / 1.5 GiB | 1022 | 819 |

Both sit where [the memory section](#measured-model-worker-memory-informational) puts a child holding all four models without the worst rerank batch, which this mix does not send: 167 MiB of the container is the `/deps` wheels in tmpfs, charged as shared memory. Over 4290 jobs and the child's replacements, the peak stayed within 14 MiB of the 512-job run, with about 500 MiB left below the limit. The model workflow's on-demand `load` job (`workflow_dispatch` only) runs the first table's runs on GitHub's runner and appends the tables and the memory figures to its job summary; it gates nothing. To reproduce after acquiring the models, the first table's way:

```sh
for threads in 1 4 16; do
    docker compose -f docker/compose.yaml up -d --force-recreate worker
    LOAD_TARGET=lexical LOAD_CONCURRENCY=$threads LOAD_SECONDS=120 docker compose -f docker/compose.yaml run --rm --no-deps -T model-load
done
for threads in 1 4; do
    docker compose -f docker/compose.yaml up -d --force-recreate model-worker
    LOAD_TARGET=onnx LOAD_CONCURRENCY=$threads LOAD_SECONDS=600 docker compose -f docker/compose.yaml run --rm --no-deps -T model-load
done
docker exec "$(docker compose -f docker/compose.yaml ps -q model-worker)" cat /sys/fs/cgroup/memory.peak
docker compose -f docker/compose.yaml --profile model down
```

The historical second table used the commands below against one-off workers; on current code the separate result limit changes their admission behavior:

```sh
docker compose -f docker/compose.yaml run -d --no-deps --name bridge-load-lexical -e BRIDGE_CAPACITY=1024 -e BRIDGE_RETENTION_SECONDS=5 worker
LOAD_TARGET=http://bridge-load-lexical:8090 LOAD_CONCURRENCY=1,4,16 LOAD_SETTLE_SECONDS=10 docker compose -f docker/compose.yaml run --rm --no-deps -T model-load
docker compose -f docker/compose.yaml run -d --no-deps --name bridge-load-worker -e BRIDGE_RETENTION_SECONDS=30 model-worker
LOAD_TARGET=http://bridge-load-worker:8090 LOAD_CONCURRENCY=1,4 LOAD_SECONDS=600 LOAD_SETTLE_SECONDS=60 docker compose -f docker/compose.yaml run --rm --no-deps -T model-load
docker rm -f bridge-load-lexical bridge-load-worker
```

## Measured NER precision and recall (informational)

`scripts/ner_measure.py` runs every sentence of `tests/fixtures/ner-sample.json` through the ONNX worker's redact task with PER, ORG and LOC, at the task's default threshold and a lower one, each its own job, and compares the models' spans with the sample's labels; rule spans are not counted. The model workflow prints a fresh table to its job summary on every run, and fails only if a job fails, never on a score. The table below was measured locally with this script over `main` at `dd2cd9d`, the commit its header names; a label's Masked counts that label's entities hidden under any label, so it can exceed the label's overlapping recall. The sample is invented and small, so these figures describe these pinned models on these three registers, not a quality guarantee.

Measured 2026-10-26 UTC, commit `dd2cd9d`, runner: local, Windows 11 Pro with Docker Desktop 29.8.2, CPU: AMD Ryzen 9 9950X 16-Core Processor.
Model `Xenova/bert-base-NER:int8+Xenova/bert-base-NER-uncased:int8`; `tests/fixtures/ner-sample.json`, 48 invented sentences with 104 labelled entities; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted span. Masked: a shared character under any label, one gold span per predicted span, the share of entities a redaction hides; a label's row counts that label's entities hidden under any label.

| Threshold | Register or label | Gold | Predicted | Precision, exact | Recall, exact | Precision, overlapping | Recall, overlapping | Masked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.85 | news | 38 | 38 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.85 | informal | 30 | 24 | 0.75 | 0.60 | 0.88 | 0.70 | 0.80 |
| 0.85 | records | 36 | 36 | 0.94 | 0.94 | 0.94 | 0.94 | 1.00 |
| 0.85 | PER | 42 | 37 | 0.92 | 0.81 | 1.00 | 0.88 | 0.93 |
| 0.85 | ORG | 24 | 23 | 0.91 | 0.88 | 0.91 | 0.88 | 0.96 |
| 0.85 | LOC | 38 | 38 | 0.92 | 0.92 | 0.92 | 0.92 | 0.95 |
| 0.5 | news | 38 | 38 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.5 | informal | 30 | 31 | 0.68 | 0.70 | 0.74 | 0.77 | 0.97 |
| 0.5 | records | 36 | 36 | 0.94 | 0.94 | 0.94 | 0.94 | 1.00 |
| 0.5 | PER | 42 | 38 | 0.95 | 0.86 | 1.00 | 0.90 | 1.00 |
| 0.5 | ORG | 24 | 28 | 0.75 | 0.88 | 0.75 | 0.88 | 0.96 |
| 0.5 | LOC | 38 | 39 | 0.92 | 0.95 | 0.92 | 0.95 | 1.00 |

Since pull request #18, the redact task reads a sentence written all in lower case or all in capitals a second time, from a title-cased copy with the same offsets, and likewise a surname in capitals before a comma and a given name (`WIERZBICKI, Tomasz`), the records convention. It joins pieces of one name that touch or meet at a hyphen, full stop or apostrophe before the threshold, scored by the most confident piece, so the initial of `A. Okonkwo` carries the surname; and of two readings of the same span it keeps the more confident. Since pull request #19, the uncased `dslim/bert-base-NER-uncased` reads the whole text once more, ignoring case, beside the cased model, which keeps what capitals tell in news and records: alone, the uncased model masked only 0.54 of the development set's records against 0.77 for the cased readings. Those choices were made on a separate development set, `tests/fixtures/ner-dev.json`, which before the uncased model was measured gained lower-case names among capitals, news sentences and sentences of common words that are also names (`pay the bill`, `a rose`, `Will the courier`); the uncased model invented no name in any of them. This sample stayed unseen until each final measurement; both sets are small and were written by the same author, so the figures describe these sentences, not informal text in general.

In the model workflow's run for pull request #19 on GitHub's EPYC runners, against pull request #18's run on the same CPU type, exact recall at 0.85 rose from 0.47 to 0.60 in informal messages and from 0.92 to 0.94 in records, and news stayed at 1.00; the share of entities masked under any label, which is what a redaction hides, rose from 0.63 to 0.80 in informal messages and from 0.97 to 1.00 in records, and at 0.5 it is 1.00 in all three. Exact precision held at 0.94 in records and moved from 0.74 to 0.72 in informal messages. The local table above differs from that run in informal messages, with 24 spans instead of 25 at 0.85 (exact precision 0.75) and 31 instead of 33 at 0.5, where 0.97 of the entities are masked, and in ORG, with 23 spans instead of 24 at 0.85 and 28 instead of 30 at 0.5. What still leaks at 0.85: a name after a sign-off (`Regards, olumide`), a handle (`@nadia`), some lower-case places and first names (`porto`, `galway`, `fatima`) and a capitalised company in an informal note (`Halvern`). A reading can still take a filler word into a name (`lol tomasz`) or give a name the wrong label, which still hides it, and the uncased model tags a domain name such as `example.com` as an organisation.

int8 inference is not bit-identical across CPUs: a local run on an AMD Ryzen 9 9950X has differed from GitHub's EPYC runners by a span or two, so read such differences as noise. The redact tool's description quotes these figures; re-measure and update both together. To reproduce locally after acquiring the models:

```sh
docker compose -f docker/compose.yaml up -d model-worker
docker compose -f docker/compose.yaml run --rm --no-deps -T model-ner
NER_SAMPLE=tests/fixtures/ner-dev.json docker compose -f docker/compose.yaml run --rm --no-deps -T model-ner
```

## Measured reranking quality on ArguAna (informational)

`scripts/relevance_measure.py` measures the rerank task on public data: BEIR's copy of ArguAna, the counter-argument retrieval task of Wachsmuth et al., with 8674 arguments, 1406 test queries and one relevant document per query, under CC BY 4.0 as appendix E of BEIR's paper (arXiv:2104.08663) states. `scripts/public_data.py` pins its URL, size and SHA-256, and `quality-fetcher`, the only quality step with network, stores it under `.cache/datasets/`. The script takes 200 test queries evenly spaced in file order, ranks the whole corpus for each with a standard-library BM25 (k1 1.2, b 0.75, Lucene's idf, lower-cased word tokens, no stemming or stop words), leaves out the query's own argument as BEIR does, and sends the top 100 to the worker's rerank task, one job per query. It fails only if a job fails or returns a candidate other than once, never on a score. Both runs below were made on this change's working tree over `main` at `e77310f`, the commit their headers name.

ArguAna is the only publicly downloadable BEIR dataset that is both small and under a licence allowing commercial use (CC BY 4.0): the others are non-commercial, unlicensed, GPL-3.0 or under their own agreement, or hold hundreds of thousands of documents or more.

Measured 2026-10-21 UTC, commit `e77310f`, runner: local, Windows 11 with Docker Desktop, CPU: AMD Ryzen 9 9950X 16-Core Processor.
Model `cross-encoder/ms-marco-TinyBERT-L2-v2` (onnx backend); ArguAna from BEIR, `arguana.zip` at SHA-256 `cfdf79adce27`, CC-BY-4.0: 200 of its 1406 test queries, evenly spaced in file order, over its 8674 documents (title and text); judged documents per query, on average: 1. Candidates: the BM25 top 100 per query (k1 1.2, b 0.75, lower-cased word tokens, no stemming or stop words), the query's own argument left out as BEIR does; each list reranked by one job, 2903 of its 20000 documents cut to fit the worker's limits. Wall time: BM25 indexing and ranking all 200 queries in one process; reranking, the sum of the jobs from POST /v1/jobs to a terminal state, median 0.58 s per job.

| Order | Queries | NDCG@10 | MRR@10 | Relevant document among the candidates | Wall time s |
| --- | --- | --- | --- | --- | --- |
| BM25 | 200 | 0.449 | 0.356 | 0.95 | 14.0 |
| BM25, reranked | 200 | 0.348 | 0.269 | 0.95 | 123.0 |

On this task the cross-encoder makes the ranking worse: NDCG@10 falls from 0.449 to 0.348 and MRR@10 from 0.356 to 0.269, with the relevant counter-argument among the 100 candidates for 95 % of the queries. BEIR found the same of its own BM25-plus-cross-encoder, which makes a training-domain mismatch the plausible explanation, though this measurement does not demonstrate it: better than BM25 on 16 of 18 datasets, worse only on ArguAna and Touché-2020, "two retrieval tasks that are extremely different to the MS MARCO training dataset". An ArguAna query is a whole argument (median 1077 characters) whose answer argues the opposite on the same topic, while `ms-marco-TinyBERT-L2-v2` was trained to find the passage that answers a short web query; beside such a query the 512-piece window leaves about half its length to the document. Each candidate list was cut to the worker's 200000 characters, about 1990 per document, which shortened 2903 of the 20000 documents sent, mostly past what that window reads. The sample's BM25 baseline is close to the split's: over all 1406 queries the same BM25, run once with the script's functions and no worker, gave NDCG@10 0.462, MRR@10 0.375 and 0.95; the reranking was not run over the full split, so how far its loss on the sample holds there is not measured. A second run gave the same scores to the third decimal; the reranked wall time is 200 sequential jobs of 100 documents on one CPU, a median of 0.57-0.58 s each.

These figures say that, in this configuration (ArguAna, this BM25's top 100, the worker's input limits), this reranker should not be put in front of BM25. They do not say how it ranks short questions against passages, its training task, for which no small, freely licensed dataset was found; nor anything about other languages. This BM25 is not BEIR's (Elasticsearch, with an English analyser), so its figures are not comparable to BEIR's tables, only to the reranked row beside them. To reproduce after acquiring the models, with 15 s retention so finished jobs expire during the measurements rather than filling the worker's retained-result capacity of 384:

```sh
docker compose -f docker/compose.yaml run --rm --no-deps quality-fetcher
BRIDGE_MODEL_RETENTION_SECONDS=15 docker compose -f docker/compose.yaml up -d model-worker
docker compose -f docker/compose.yaml run --rm --no-deps -T model-quality
docker compose -f docker/compose.yaml --profile model down
```

`model-quality` prints this table and the next. The model workflow's `quality` job runs the same steps on GitHub's runners when the workflow is started by hand, and appends both tables to its job summary; it does not run on pull requests or on the schedule.

## Measured NER quality on WNUT-17 (informational)

`NER_DATASET=wnut17 scripts/ner_measure.py` runs the WNUT-17 test split (Derczynski et al., emerging and rare entities in social media and forum posts) through the redact task, one sentence per job, at the same two thresholds as the invented sample. The split is CC BY 4.0 as its repository's README states, pinned to a commit and a SHA-256 in `scripts/public_data.py` and stored by `quality-fetcher`. Its person entities count as PER, corporation and group as ORG, and location as LOC; product and creative-work have no label among the task's three, so they are not gold spans and a span over one counts against precision. Rule spans are not counted. The table adds F1 to the invented sample's columns and replaces its registers with one row over all three labels.

Measured 2026-10-26 UTC, commit `dd2cd9d`, runner: local, Windows 11 Pro with Docker Desktop 29.8.2, CPU: AMD Ryzen 9 9950X 16-Core Processor.
Model `Xenova/bert-base-NER:int8+Xenova/bert-base-NER-uncased:int8`; WNUT-17 test split, `emerging.test.annotated` at SHA-256 `2aa79b764e56`, CC-BY-4.0: 1287 sentences with 810 entities mapped to labels, person to PER, corporation and group to ORG, location to LOC; product and creative-work have no label here, so a span over one counts against precision. Each sentence is its tokens joined by single spaces; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted span. Masked: gold spans that share a character with a predicted span of any label, one gold span per predicted span; a label's row counts that label's gold spans hidden under any label, so it can exceed overlapping recall.

| Threshold | Label | Gold | Predicted | Precision, exact | Recall, exact | F1, exact | Precision, overlapping | Recall, overlapping | F1, overlapping | Masked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.85 | all labels | 810 | 939 | 0.42 | 0.49 | 0.45 | 0.45 | 0.52 | 0.48 | 0.59 |
| 0.85 | PER | 429 | 310 | 0.75 | 0.54 | 0.63 | 0.78 | 0.56 | 0.65 | 0.63 |
| 0.85 | ORG | 231 | 487 | 0.17 | 0.36 | 0.23 | 0.19 | 0.40 | 0.26 | 0.45 |
| 0.85 | LOC | 150 | 142 | 0.57 | 0.54 | 0.55 | 0.62 | 0.59 | 0.60 | 0.69 |
| 0.5 | all labels | 810 | 1892 | 0.24 | 0.55 | 0.33 | 0.26 | 0.60 | 0.36 | 0.78 |
| 0.5 | PER | 429 | 395 | 0.63 | 0.58 | 0.61 | 0.67 | 0.62 | 0.65 | 0.85 |
| 0.5 | ORG | 231 | 1278 | 0.08 | 0.45 | 0.14 | 0.09 | 0.52 | 0.16 | 0.59 |
| 0.5 | LOC | 150 | 219 | 0.43 | 0.63 | 0.51 | 0.48 | 0.70 | 0.57 | 0.85 |

On real user-generated text the models find about half of the entities at the default threshold: exact F1 0.45 over all three labels, 0.63 for persons (precision 0.75, recall 0.54), 0.55 for places and 0.23 for organisations, where 487 predicted spans meet 231 gold ones. At 0.85, 0.59 of the entities share a character with a predicted span of some label (the Masked column, one entity per predicted span; an overlap, not a count of characters hidden; by label 0.63 of persons, 0.45 of organisations and 0.69 of places, some of them under another label), against 0.80 in the invented sample's informal messages; at 0.5 the share is 0.78, at the cost of 1278 ORG spans, nine in ten of them wrong. Part of the ORG excess may be WNUT-17's 269 products and creative works, which this mapping leaves without a label; how much was not counted. WNUT-17 was built from entities that are rare or new, which makes it harder than text naming well-known people and companies.

These figures carry what the models do with this public, English, informal text, at character-level matching on tokens joined by single spaces, so `&gt;` reads as `& gt ;` and punctuation stands apart from words, as in the file. They are not WNUT-17's own entity F1, which counts six types; and they are one run: int8 inference is not bit-identical across CPUs, as the section above notes. The redact tool's description still quotes the invented sample's figures. To reproduce, run `model-quality` as above, or the NER half alone:

```sh
NER_DATASET=wnut17 docker compose -f docker/compose.yaml run --rm --no-deps -T -e NER_DATASET model-quality python scripts/ner_measure.py
```

## Measured task counts under a cancellation burst (informational)

A child that is cancelled or passes its deadline is terminated at retirement and killed 0.2 s later if it is still there, while its replacement starts on the same tick, so the pool can briefly hold one child more than `BRIDGE_CONCURRENCY`. Every container here has `pids_limit: 128`, which counts threads, not processes. This section records how close the model worker comes to it.

Measured 2026-10-21 UTC locally on `main` at `5d3ceb3` and on the same tree with the change below, on Windows 11 with Docker Desktop, CPU AMD Ryzen 9 9950X 16-Core Processor, the Docker VM showing the container 24 CPUs (`os.cpu_count()` and `sched_getaffinity`) under a quota of one (`cpu.max` 100000 100000), with model-worker's compose limits and `BRIDGE_CONCURRENCY` overridden with `-e`. A script on the isolated network warmed the children with two rounds of two concurrent rerank jobs of 512 documents of ~40 words (scripts/benchmark.py's input), waited 3 s, submitted 12 such jobs, waited until two were running and had reported progress, and cancelled all 12 in one burst. Inside the container a sampler read `/sys/fs/cgroup/pids.current` and every process's `Threads` from `/proc/<pid>/status` every 50 ms; its own task is in every figure below. A replacement child holds one thread until its first job; the figures for fresh children are after each served one more job.

| Build | `BRIDGE_CONCURRENCY` | Threads in one warm idle child | Tasks, cold pool | Tasks, warm idle pool | Peak, burst included |
| --- | --- | --- | --- | --- | --- |
| `main` | 2 | 25 | 7 | 55 | 56 |
| `main` | 8 | 25 | 13 | 61, two of eight children warm | 128, the limit |
| one-thread pins | 2 | 2 | 7 | 9 | 10 |
| one-thread pins | 8 | 2 | 13 | 15, two of eight children warm | 16 |

The burst itself added nothing visible: in no run did the count rise after the first cancellation. It fell as the cancellations reached their children (the 12 requests took 0.01-0.10 s), and every sample showed a retired child gone and its replacement at one thread, so a terminated child exited within one 50 ms sample, well inside the 0.2 s grace; the peaks in the table were reached while the jobs ran. What came near the limit is the size of a warm child. Of its 25 threads, 23 come from numpy's OpenBLAS, which starts one per visible CPU when `onnxruntime` imports numpy and ignores the CPU quota; the tokenizers' rayon pool added one thread whether `RAYON_NUM_THREADS` was unset or 1, presumably because Rust sizes it from the cgroup quota, and ONNX Runtime is pinned to one thread by the session options. A pool at concurrency 2 therefore held 55 tasks of 128 on 24 CPUs, would pass the limit on a host showing about 60, and at concurrency 8 reached it on this one as the busy children loaded the model: the worker logged `OpenBLAS blas_thread_init: pthread_create failed for thread 23 of 24: Resource temporarily unavailable` and similar lines for other children. The 12 jobs were cancelled, so this run does not show what the failure does to a job; the warm-up jobs and the two jobs after the burst all succeeded.

The store therefore sets `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1`, since OpenBLAS reads the first of `OPENBLAS_NUM_THREADS`, `GOTO_NUM_THREADS` and `OMP_NUM_THREADS` that is set, and `RAYON_NUM_THREADS=1`, for a container without a CPU quota, in the environment every child starts with, overriding any value the service had for these three (`tests/python/test_jobs.py` reads them from a child's `/proc/<pid>/environ`). A warm child then holds two threads, so the pool's size no longer depends on the host's CPU count. On one CPU of quota the extra BLAS threads had nothing to run on: the latency benchmark on the pinned tree, run once on the same machine the same day, gave medians of 0.21 s for 512 documents, 0.21 s for the worst-case batch, 0.16 s for embedding and 0.57 s for redaction, none slower than the table above.

To reproduce, start the model worker with a project name and the concurrency to test, then sample from the host while a client of your own, from a one-off `run --rm --no-deps` of a python service on the `isolated` network, runs the warm-up rounds, submits 12 reranks of 512 documents, waits until two report running with progress and cancels all 12:

```sh
docker compose -p <name> -f docker/compose.yaml run -d --no-deps --name bridge-model-worker -e BRIDGE_CONCURRENCY=2 model-worker
docker exec bridge-model-worker cat /sys/fs/cgroup/pids.current
docker exec bridge-model-worker sh -c 'ls /proc/<child pid>/task | wc -l'
docker compose -p <name> -f docker/compose.yaml --profile model --profile test down
```

## Measured hook cost (informational)

Measured 2026-10-20 locally on Windows 11 Pro 10.0.26200, CPU AMD Ryzen 9 9950X 16-Core Processor, Git Bash 5.2.37 (`x86_64-pc-msys`) with GNU sed 4.9. The pre-call push guard runs before every Bash and PowerShell call with a 20-second timeout in `.claude/settings.json`. Each figure is the wall time of `sh .claude/hooks/push-guard.sh` reading a Bash tool call's JSON on stdin, from the process start to its exit 0, so it includes starting the shell on Windows but not Claude Code's own spawn. Ten runs per input after the change; on `main` at `02bfc25`, ten for the first two and three for the 200 KB body. The bodies repeat one block of ordinary Markdown (headings, a list, a table, code spans, a link, quotes) to the stated size of the escaped JSON string.

| Input | Hook | N | min s | median s | max s |
| --- | --- | --- | --- | --- | --- |
| `docker compose -f docker/compose.yaml run --rm --no-deps python-tests` | `main` | 10 | 0.05 | 0.06 | 0.08 |
| `docker compose -f docker/compose.yaml run --rm --no-deps python-tests` | changed | 10 | 0.08 | 0.09 | 0.10 |
| `gh pr create --title x --body-file - <<'EOF'`, a 20 KB body, `EOF` | `main` | 10 | 1.04 | 1.05 | 1.14 |
| `gh pr create --title x --body-file - <<'EOF'`, a 20 KB body, `EOF` | changed | 10 | 0.34 | 0.38 | 0.40 |
| `gh pr create --title x --body-file - <<'EOF'`, a 200 KB body, `EOF` | `main` | 3 | 75.57 | 75.75 | 75.98 |
| `gh pr create --title x --body-file - <<'EOF'`, a 200 KB body, `EOF` | changed | 10 | 1.59 | 1.60 | 1.64 |

The compose command has no word the hook guards, so it exits at its first check; alternating the two hooks 20 times on it gave medians of 0.085 and 0.089 s, the same within noise. On `main` the cost grew with the square of the body: Git Bash's C locale is UTF-8, where each bash pattern cut and each sed substitution walks the whole text, and the hook cut one character, one quote and one heredoc line at a time. A 200 KB body ran past the timeout almost four times over. The hook now uses a single-byte locale under MSYS and Cygwin, as C is on Linux, splits the JSON string at quotes once, reads a heredoc's body line by line with `read`, and takes single characters with cuts that stop at the first try. A long command outside a heredoc, such as a 200 KB double-quoted `--body`, is still walked a character at a time and was not measured here.

## LLM evaluation (informational)

Run 2026-09-27 on Windows 11 with Docker Desktop and Claude Code 2.1.278, through `scripts/mcp_evaluate.py` as [the MCP guide](mcp.md#evaluating-with-an-llm) describes: the demo worker behind the compose `mcp` service, the 27-chunk fixture corpus, and one session per question per model.

| Model, as the CLI reported it | Correct | Tool calls | Median seconds per question | Cost the CLI estimated |
| --- | --- | --- | --- | --- |
| claude-sonnet-5 | 10/10 | 10 | 16.5 | $0.10 |
| claude-opus-5 | 10/10 | 23 | 26.4 | $0.54 |
| claude-haiku-4-5-20251001 | 10/10 | 10 | 20.5 | $0.11 |

Every model used two searches for the two-step question, and none of the 30 sessions had a failed tool call. Opus searched more, checking candidates against the distractors. Sonnet and Haiku made one call for each other question, except the one about the redaction backend, which they answered from the tool descriptions without a call. The seconds are wall time per session, mostly the `mcp` container starting and installing its wheels.

An earlier run the same day, on the five-chunk corpus this one replaced, also scored ten out of ten on each model. There, Sonnet and Haiku answered the two-step question with one search, because every search returned the whole corpus; that is why the corpus grew.

The scores show that these models can use the tools on this corpus, not that the evaluation separates them. One session per question gives no success rate, the demo worker matches words rather than meaning, and the questions stay close to the corpus's wording.

The semantic evaluation, `tests/mcp/evaluation-semantic.xml`, ran on 2026-09-30 with Claude Code 2.1.284, one session per question, four at a time:

| Model, as the CLI reported it | Backend | Correct | Tool calls | Note |
| --- | --- | --- | --- | --- |
| claude-haiku-4-5-20251001 | onnx | 10/10 | 55 | one question took 44 searches |
| claude-sonnet-5-5 | onnx | 10/10 | 12 | one search for five of the six search questions; one call to a tool it did not have, refused |
| claude-haiku-4-5-20251001 | lexical | 6/10 | 54 | every search answer, one after 40 searches; none of the other four |

On the demo worker, Haiku found all six search answers by rewording its queries toward the notes' words, so the agent supplied the meaning the backend lacks; it missed the rerank, the similarity ranking, the redaction and the backend question, which word matching cannot answer. The first Sonnet run on the models, started right after Haiku's, scored 5/10: every search it lost ended in HTTP 429, because the model worker counts finished jobs against its capacity of 128 for 300 seconds and Haiku's run had filled it. The tools then passed that refusal to the agent rather than waiting it out, and the row above is a second run after the worker was restarted. Since then a tool waits out a full worker within its own call, and the MCP workers keep finished jobs for 30 seconds rather than 300: the same sequence, Haiku then Sonnet on the models with no restart between them, scored 10/10 for each. That run made 22 calls in all, too few to fill the worker, so it shows the sequence works; `tests/mcp/test_capacity.py` shows the wait itself.

Before deploying, test the actual application and framework combination, measure latency under sustained load on the deployment's own hardware and workloads (the load section above is one development machine), exercise overload and restarts, validate tenant isolation with your own tokens, and decide whether results kept across a restart without replay of unfinished work is acceptable. The supported tier is the one [the operations guide](operations.md) describes; this is not a replacement for a durable job system.
