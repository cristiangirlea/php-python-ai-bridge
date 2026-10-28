# PHP–Python AI Bridge

A framework-independent PHP client and a small Python task service. Submit document reranking from PHP, poll progress, receive typed results, and cancel work without keeping the original HTTP request open.

Includes a FrankenPHP worker-mode example, deterministic fault tests, and an optional real ONNX reranker. No hosted AI account is required.

Since 0.4.0 it is supported as an internal service for applications that own their host and network: one or more worker instances, each answering only the jobs it took, on a private network behind the operator's TLS and the application's own authentication, with the job table kept across a restart by `BRIDGE_STATE_DIR`. It is not for exposing the worker publicly, nor for throughput beyond the measured figures (one ONNX child on one CPU served about 3.5 jobs/s of the rerank, embed and redact mix, and an instance admits at most `BRIDGE_CAPACITY` jobs per `BRIDGE_RETENTION_SECONDS`) without more instances; [the operations guide](docs/operations.md) has the topologies and the sizing.

## What is implemented

- PHP 8.2–8.5 client using cURL, with validated job objects and typed rerank, embedding and redaction results. Rerank accepts up to 512 candidate documents and an optional `top_k` so a large set narrows to a small result; the ONNX backend scores them 32 pairs per forward pass.
- Python 3.12 service with an explicit task allowlist and a pool of warm child processes, one job per child at a time.
- Text embedding: up to 32 texts per job return unit-length 384-dimensional vectors for an index the caller owns.
- Redaction: checksum and pattern rules for card numbers, IBANs, emails, IPv4 and IPv6 addresses and phone numbers on every backend, plus named-entity masking with the int8 cased and uncased `bert-base-NER` models on the ONNX profile. Assistive, not a compliance control.
- Bounded concurrency, queue/result capacity, retention, request sizes and execution deadlines.
- Cancellation, progress polling, crash detection and generic errors that omit internal exception text.
- `GET /metrics` on the worker: aggregate job counts, queue depth, pool readiness and duration histograms in the Prometheus text format, behind the service token and carrying no job content; counters reset on restart ([the protocol](docs/protocol.md#metrics)).
- Optional structured logging with `BRIDGE_LOG=json`: best effort, one JSON line on stderr per request and per finished job, plus start, drain and stop, with a fixed schema that never holds inputs, results, headers, query strings, tokens or exception text; off by default ([the protocol](docs/protocol.md#logging)).
- A FrankenPHP example that submits work and returns a job ID immediately.
- A stdio MCP server with one tool per task, a second client of the same protocol beside the PHP client, plus an index builder over the protocol and a `bridge_search` tool that searches what it built, returning offsets and snippets rather than vectors, a compose service that puts the pinned ONNX models behind it, and a ten-question evaluation for LLM hosts with a runner for the Claude Code CLI; see [the MCP guide](docs/mcp.md).
- CPU inference with `cross-encoder/ms-marco-TinyBERT-L2-v2`, `sentence-transformers/all-MiniLM-L6-v2` and int8 `dslim/bert-base-NER` and `dslim/bert-base-NER-uncased` through ONNX Runtime, using pinned model files and wheel hashes.

The default backends are **`lexical-demo-not-a-model`** for reranking and **`hashing-bow-not-a-model`** for embedding, deterministic word overlap and feature hashing for testing the integration, and **`rules-only-not-a-model`** for redaction, which runs the real checksum and pattern rules but no model. None of them is described as AI. Enable the optional model profile below for real inference.

## Try it with Docker

Requirements: Docker Engine/Desktop with Linux containers and Docker Compose. The provided images and model wheel lock target **Linux x86_64 / Python 3.12**; the [published worker images](#run-the-published-image) and the lock also cover arm64, which the tests do not run. No local PHP or Python installation is needed.

One Composer package supports PHP 8.2, 8.3, 8.4 and 8.5: the upstream-supported PHP branches as of September 2026. The default demo uses PHP 8.5; CI exercises all four branches. See [support policy and verification limits](docs/testing.md).

Set a random, disposable service token in your shell:

```sh
export BRIDGE_TOKEN="$(openssl rand -hex 32)"
```

PowerShell 7.2+ alternative:

```powershell
$env:BRIDGE_TOKEN = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

From the repository root:

```sh
docker compose -f docker/compose.yaml config
docker compose -f docker/compose.yaml up -d worker frankenphp
docker compose -f docker/compose.yaml run --rm --no-deps integration
docker compose -f docker/compose.yaml run --rm --no-deps php-example
```

The CLI example prints sorted document indexes and scores. The integration test sends concurrent HTTP requests through the **actual long-lived FrankenPHP worker**, checks distinct results, and verifies that later malformed requests cannot reuse earlier input.

No host ports are published. Example/test clients connect over a Docker-internal network. The example HTTP routes have **no end-user authentication** and must not be exposed publicly.

### Run the published image

Every release tag publishes the worker as an image, so a deployment needs no checkout: `ghcr.io/cristiangirlea/php-python-ai-bridge-worker` (the demo backends, standard library only) and `ghcr.io/cristiangirlea/php-python-ai-bridge-worker-onnx` (the hash-locked model wheels, no models), each tagged with the version, for linux/amd64 and linux/arm64. A stable release (`v1.2.0`) also moves `latest`; a prerelease (`v1.2.0-rc.1`) gets its version tag only. Both run as UID 65532 and listen on port 8090. Pin the version, not `latest`, and attach your application to the same private network:

```sh
docker pull ghcr.io/cristiangirlea/php-python-ai-bridge-worker:latest
docker network create --internal bridge-private
docker run -d --name worker --network bridge-private -e BRIDGE_TOKEN -e BRIDGE_STATE_DIR=/state \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 512m \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,uid=65532,gid=65532 \
  --tmpfs /state:rw,noexec,nosuid,size=64m,uid=65532,gid=65532,mode=0700 \
  ghcr.io/cristiangirlea/php-python-ai-bridge-worker:latest
```

`-e BRIDGE_TOKEN` passes the token from your shell without writing it on the command line. The ONNX image never holds models: `fetch` downloads the pinned, hash-checked files into a volume once, and the worker reads them from there without network:

```sh
docker volume create bridge-models
docker run --rm -v bridge-models:/cache ghcr.io/cristiangirlea/php-python-ai-bridge-worker-onnx:latest fetch
docker run -d --name worker --network bridge-private -e BRIDGE_TOKEN -v bridge-models:/cache:ro \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 --memory 1536m \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,uid=65532,gid=65532 \
  ghcr.io/cristiangirlea/php-python-ai-bridge-worker-onnx:latest
```

Compose's `release` profile runs the same images under the limits above: `BRIDGE_IMAGE_TAG=<version> docker compose -f docker/compose.yaml --profile release up -d worker-image` (or `model-worker-image`, which reads `.cache/models` from the `fetcher`). Each image carries build provenance and an SBOM; `gh attestation verify oci://ghcr.io/cristiangirlea/php-python-ai-bridge-worker:<version> --owner cristiangirlea` checks it. The arm64 images are built under emulation and not run by the tests.

### Run the real model

Acquire dependencies and model data in the bounded, network-enabled fetcher:

```sh
docker compose -f docker/compose.yaml run --rm --no-deps fetcher
```

This downloads wheels and about 330 MB of model and tokenizer data into ignored `.cache/`, one directory per model under `models/`; the fetcher refuses any single artifact over 128 MiB. Fetch again after an upgrade that adds a model: an ONNX worker refuses to start while a model directory lacks its files, and names it. A cache created before the embed task used a flat layout that the fetcher does not delete: remove `.cache/models` and fetch again after upgrading. Wheels are checked against `requirements-model.lock`; the model revision and SHA-256 hashes are fixed in `scripts/fetch_model.py`. On Linux, ensure `.cache/` exists and is writable by container UID 65532 before acquisition. For a disposable local cache, `mkdir -p .cache && chmod 777 .cache` is sufficient; never apply that permission to the repository or another directory.

Then run **without internet access**:

```sh
docker compose -f docker/compose.yaml up -d model-worker model-frankenphp
docker compose -f docker/compose.yaml run --rm --no-deps model-test
docker compose -f docker/compose.yaml run --rm --no-deps model-integration
```

`model-test` calls the real model from the PHP client. `model-integration` verifies the complete HTTP → FrankenPHP worker → PHP client → Python model path. These are smoke tests, **not** a model-quality or throughput benchmark; measured warm-job medians, and the cost of a worker's first job, are in [the testing guide](docs/testing.md#measured-latency-informational), throughput and latency percentiles under concurrent load beside them in [its load section](docs/testing.md#measured-throughput-and-latency-under-load-informational), and quality on public data, reranking on ArguAna and NER on WNUT-17, in [its quality sections](docs/testing.md#measured-reranking-quality-on-arguana-informational). Model scores are relevance logits, not probabilities.

Stop this project's containers when finished:

```sh
docker compose -f docker/compose.yaml --profile test --profile model down
```

The downloaded cache remains for reuse; it is not included in Git.

## Use from PHP

See the [integration guide](docs/integration.md) for Composer setup, the Laravel service provider and the Symfony bundle that configure the client from `BRIDGE_URL` and `BRIDGE_TOKEN`, application job ownership and failure handling.
The client sends through `ext-curl` by default; an application with a PSR-18 client such as Symfony HttpClient or Guzzle can pass `PhpAiBridge\Psr18Transport` instead, configured as the [integration guide](docs/integration.md#through-a-psr-18-http-client) shows.

This source tree is a Composer library with PSR-4 autoloading under `PhpAiBridge\`. The latest tagged release is `v0.4.0` ([changelog](CHANGELOG.md)): install it with `composer require cristiangirlea/php-python-ai-bridge:^0.4` once the package is on Packagist, or from a VCS repository pinned to the tag until then, as the integration guide shows. `examples/bootstrap.php` is a repository-only autoloader for dependency-free examples, not an application installation method.

```php
use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

$client = new Client('http://worker:8090', getenv('BRIDGE_TOKEN'));
$documents = ['Saturn has rings.', 'Paris is the capital of France.'];
$job = $client->submitRerank('What is the capital of France?', $documents);

// In an HTTP application, save the job ID with its owner and return 202 now.
// A subsequent request can call $client->get($job->id).

// CLI/background code may wait synchronously:
$completed = $client->wait($job->id, waitTimeoutMs: 30000);
$result = RerankResult::fromJob($completed, count($documents));
echo $documents[$result->rankings[0]['index']];
```

`submitEmbed($texts)` works the same way and `EmbedResult::fromJob($job, count($texts))` yields one unit-length vector per text; the service stores nothing, so persist vectors in an index your application owns. `submitRedact($text)` returns a job whose `RedactResult::fromJob($job, $text)` holds the masked text and the spans found, each tagged with the rule or model that found it. `wait()` returns terminal jobs, including failures; `RerankResult::fromJob()` refuses anything except a successful rerank result. Use `cancel($id)` explicitly to stop remote work. A local wait timeout does **not** cancel the job.

## Two things people use it for

**Scrub text before it leaves your systems.** A support ticket or a document is redacted locally,
then the masked text goes to a hosted model. Nothing personal leaves the network, and the spans
tell you what was hidden and by which rule or model.

```php
$job = $client->submitRedact($ticket, ['PER', 'ORG', 'LOC']);
$result = RedactResult::fromJob($client->wait($job->id), $ticket);
$answer = $yourLlmClient->ask($result->text);   // the masked text, never the original
```

Redaction is a pre-filter, not a compliance control: on public social-media text it hides about
half of the named entities at the default threshold, and nearly all card numbers, IBANs, emails and
IP addresses ([measured](docs/testing.md#measured-ner-quality-on-wnut-17-informational)).

**Reorder search results by meaning.** Keep the search you have (a database, Meilisearch, Elastic),
take its top results, and let the cross-encoder order them for the query. It is the cheapest visible
quality change a search box can get; measure on your own data first, since on some corpora BM25 alone
is better ([measured](docs/testing.md#measured-reranking-quality-on-arguana-informational)).

```php
$hits = $search->query($q, limit: 100);            // your existing search
$job = $client->submitRerank($q, array_column($hits, 'text'), topK: 10);
$top = RerankResult::fromJob($client->wait($job->id), count($hits), topK: 10);
```

## Support

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). If you want help putting the bridge into your application, sizing it or
reviewing a redaction setup, open an issue describing the context, or sponsor the project through the
Sponsor button.

## Test layers

```sh
# No network, including no dependency downloads:
docker compose -f docker/compose.yaml run --rm --no-deps python-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests

# Internal-only HTTP peers, including intentionally broken peers:
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile tokens-worker
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration

# Failure injection on dedicated no-model peers, and alert-rule syntax and behavior checks:
sh scripts/failure_drills.sh
docker compose -p bridge-drills -f docker/compose.yaml --profile drills run --rm --no-deps promtool
docker compose -p bridge-drills -f docker/compose.yaml --profile drills down -v

# MCP server, after acquiring its hash-pinned wheels once (network):
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps mcp-tests

# mypy --strict and ruff over the Python code, after acquiring the pinned wheels once (network);
# py-analysis also installs the MCP server's wheels from mcp-fetcher, so it runs after that:
docker compose -f docker/compose.yaml run --rm --no-deps py-dev-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps py-analysis

# PHP syntax validation inside the container:
docker compose -f docker/compose.yaml run --rm --no-deps php-tests sh -ec \
  'find src examples tests/php -name "*.php" -print0 | xargs -0 -n1 php -l'

# Consumer install, offline: composer validate --strict, then a throwaway project
# installs the git archive from an artifact repository (or a path repository in a worktree),
# checks what shipped in vendor/, and autoloads the client:
docker compose -f docker/compose.yaml run --rm --no-deps composer-install

# PHPStan at level max, after installing the locked version once (network):
docker compose -f docker/compose.yaml run --rm --no-deps php-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps php-analysis
```

Coverage includes invalid contracts, duplicate/non-finite JSON, authentication, oversized bodies, queue deadlines, queued/running cancellation, hard process crashes, retention, malformed/oversized HTTP responses, redirect refusal, and reused-worker request isolation. Fault tasks are enabled only on dedicated test and drill peers with `BRIDGE_TEST_TASKS=1`. The [operations guide](docs/operations.md#drills) describes each drill and its explicit skip.

CI runs deterministic tests for pushes to main and for pull requests across PHP 8.2–8.5, treating PHP warnings and deprecations as test failures. Every job runs on GitHub's hosted runners (`ubuntu-latest`), because the repository is public: GitHub does not meter Actions minutes for public repositories, and a fork's pull request runs its own code on a disposable runner of GitHub's, not on a machine of the maintainer's; see [the runner notes](docs/testing.md#ci-runners) and [security](docs/security.md). The stable `deterministic` check succeeds only if every PHP matrix job and the MCP server job, which runs the offline MCP suite, mypy and ruff, the consumer install and PHPStan, and the worker-image and failure-drill jobs all succeed. The drill job also checks and tests the Prometheus rules with digest-pinned promtool. A separate workflow performs model acquisition and the real-model smoke tests on the default PHP 8.5 image: weekly, on pull requests that touch the model path, and on demand. It is not a required check, it does not use paid APIs or GPUs, and it also prints an informational latency table: its jobs must succeed, but its times never gate the run.

## Important limits

- **Durable job table only when `BRIDGE_STATE_DIR` is set:** the worker then keeps its job table (never inputs) in SQLite, so finished results and idempotency keys survive a restart until retention ends and unfinished jobs become `failed` with `service_restarted`; unset, a restart loses everything. Completed results expire after 300 seconds by default; 404 can mean expired, lost or unknown. An optional `idempotency_key` makes resubmitting after an uncertain POST safe while the job is retained. There is still no replay of unfinished jobs, durable delivery or automatic retry.
- **Trusted services only:** a bearer token reaches only the jobs it submitted; give each application its own (`BRIDGE_TOKENS`, [the protocol](docs/protocol.md#tokens)), since they still share one worker's capacity. Your application must authenticate users, associate job IDs with their owners, and authorize reads/cancellation. Random IDs are not authorization. Report a vulnerability privately as [SECURITY.md](SECURITY.md) says.
- **Private HTTP transport:** deploy behind an authenticated TLS boundary for cross-host use; the [integration guide](docs/integration.md#cross-host-use-behind-tls) has a tested example with Caddy in front of the worker. The sample Python HTTP server is not an internet-facing production server.
- **Process isolation is not a sandbox for arbitrary code:** task code is trusted and registered in source. Clients cannot upload Python, import modules or select a shell command. The container limits the entire service, not each job separately.
- **One job per warm child, no sharing across jobs:** the worker keeps `BRIDGE_CONCURRENCY` child processes alive and each loads its backend's models as it starts and keeps them, so a job waits for loading only while no child has finished starting; a child is replaced after a cancellation, a deadline or a crash, and after `BRIDGE_WORKER_MAX_JOBS` jobs (default 1000). Within one rerank job the ONNX backend scores documents 32 pairs per forward pass; there is no batching across jobs, no sharing of a loaded model between children, and no claim of GPU support, multi-host scaling or stateful agents.
- **Bounded connections and a short drain:** the worker serves at most 32 connections at once and answers one more with 503 `service_busy` and `Retry-After: 1`; a request line over 8 KiB or a header block over 16 KiB gets 431. On SIGTERM it refuses new jobs with 503 `draining`, fails queued ones as `service_restarted` and lets running ones finish for up to `BRIDGE_DRAIN_SECONDS` (default 30) before it stops; a second signal stops it at once. `GET /readyz` says whether a child is idle with its models loaded, which `/healthz` does not. See [the protocol](docs/protocol.md#drain-and-readiness).
- **Cancellation cannot undo effects:** registered future tasks must manage their own external side effects. A transport failure during POST has an uncertain submission outcome; the client never retries POST automatically.
- **No token streaming:** progress is polled by job ID. The MCP server forwards that progress to a host that asks for it; there is no SSE, framework plugin or chat SDK.

See [the integration guide](docs/integration.md), [test evidence and limits](docs/testing.md), [the protocol](docs/protocol.md), [security boundaries](docs/security.md), [operations](docs/operations.md), and [contributing](CONTRIBUTING.md).

## License

Code: [MIT](LICENSE). The optional [TinyBERT reranker](https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L2-v2) and [MiniLM embedding model](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) are Apache-2.0 at their pinned revisions; the cased and uncased BERT NER models, [dslim/bert-base-NER](https://huggingface.co/dslim/bert-base-NER) and [dslim/bert-base-NER-uncased](https://huggingface.co/dslim/bert-base-NER-uncased), are MIT and are fetched as the int8 ONNX conversions published by Xenova ([cased](https://huggingface.co/Xenova/bert-base-NER), [uncased](https://huggingface.co/Xenova/bert-base-NER-uncased)), pinned by revision and hash. Model weights and dependency wheels are downloaded separately and retain their own licenses.
