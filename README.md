# PHP–Python AI Bridge

A framework-independent PHP client and a small Python task service. Submit document reranking from PHP, poll progress, receive typed results, and cancel work without keeping the original HTTP request open.

Includes a FrankenPHP worker-mode example, deterministic fault tests, and an optional real ONNX reranker. No hosted AI account is required. This is an **experimental prototype**, not a production job queue.

## What is implemented

- PHP 8.2–8.5 client using cURL, with validated job and reranking result objects.
- Python 3.12 service with an explicit task allowlist and a separate process per task.
- Bounded concurrency, queue/result capacity, retention, request sizes and execution deadlines.
- Cancellation, progress polling, crash detection and generic errors that omit internal exception text.
- A FrankenPHP example that submits work and returns a job ID immediately.
- CPU inference with `cross-encoder/ms-marco-TinyBERT-L2-v2` through ONNX Runtime, using pinned model files and wheel hashes.

The default backend is **`lexical-demo-not-a-model`**: deterministic word overlap for testing the integration. It is deliberately not described as AI. Enable the optional model profile below for real inference.

## Try it with Docker

Requirements: Docker Engine/Desktop with Linux containers and Docker Compose. The provided images and model wheel lock target **Linux x86_64 / Python 3.12**; other architectures are not verified. No local PHP or Python installation is needed.

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

### Run the real model

Acquire dependencies and model data in the bounded, network-enabled fetcher:

```sh
docker compose -f docker/compose.yaml run --rm --no-deps fetcher
```

This downloads wheels and about 18 MB of model/tokenizer data into ignored `.cache/`. Wheels are checked against `requirements-model.lock`; the model revision and SHA-256 hashes are fixed in `scripts/fetch_model.py`. On Linux, ensure `.cache/` exists and is writable by container UID 65532 before acquisition. For a disposable local cache, `mkdir -p .cache && chmod 777 .cache` is sufficient; never apply that permission to the repository or another directory.

Then run **without internet access**:

```sh
docker compose -f docker/compose.yaml up -d model-worker model-frankenphp
docker compose -f docker/compose.yaml run --rm --no-deps model-test
docker compose -f docker/compose.yaml run --rm --no-deps model-integration
```

`model-test` calls the real model from the PHP client. `model-integration` verifies the complete HTTP → FrankenPHP worker → PHP client → Python model path. These are smoke tests, **not** a model-quality or throughput benchmark. Model scores are relevance logits, not probabilities.

Stop this project's containers when finished:

```sh
docker compose -f docker/compose.yaml --profile test --profile model down
```

The downloaded cache remains for reuse; it is not included in Git.

## Use from PHP

See the [integration guide](docs/integration.md) for Composer evaluation setup, manual Symfony/Laravel configuration, application job ownership and failure handling. Dedicated framework adapters are not implemented yet.

This source tree is a Composer library with PSR-4 autoloading under `PhpAiBridge\`. There is no tagged release or Packagist publication yet; use a Composer path/VCS repository during evaluation. `examples/bootstrap.php` is a repository-only autoloader for dependency-free examples, not an application installation method.

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

`wait()` returns terminal jobs, including failures; `RerankResult::fromJob()` refuses anything except a successful rerank result. Use `cancel($id)` explicitly to stop remote work. A local wait timeout does **not** cancel the job.

## Test layers

```sh
# No network, including no dependency downloads:
docker compose -f docker/compose.yaml run --rm --no-deps python-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests

# Internal-only HTTP peers, including intentionally broken peers:
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration

# PHP syntax validation inside the container:
docker compose -f docker/compose.yaml run --rm --no-deps php-tests sh -ec \
  'find src examples tests/php -name "*.php" -print0 | xargs -0 -n1 php -l'
```

Coverage includes invalid contracts, duplicate/non-finite JSON, authentication, oversized bodies, queue deadlines, queued/running cancellation, hard process crashes, retention, malformed/oversized HTTP responses, redirect refusal, and reused-worker request isolation. Fault tasks are enabled only in the test service with `BRIDGE_TEST_TASKS=1`.

CI runs deterministic tests for pushes and pull requests across PHP 8.2–8.5, treating PHP warnings and deprecations as test failures. The stable `deterministic` check succeeds only if every PHP matrix job succeeds. A separate, manually dispatched workflow performs model acquisition and the real-model smoke tests on the default PHP 8.5 image; it does not use paid APIs or GPUs.

## Important limits

- **In-memory only:** a service restart loses jobs/results. Completed results expire after 300 seconds by default; 404 can mean expired, lost or unknown. There is no durable delivery, automatic retry or idempotency key support.
- **Trusted services only:** one bearer token authorizes all jobs. Your application must authenticate users, associate job IDs with their owners, and authorize reads/cancellation. Random IDs are not authorization.
- **Private HTTP transport:** deploy behind an authenticated TLS boundary for cross-host use. The sample Python HTTP server is not an internet-facing production server.
- **Process isolation is not a sandbox for arbitrary code:** task code is trusted and registered in source. Clients cannot upload Python, import modules or select a shell command. The container limits the entire service, not each job separately.
- **Cold model per job:** this prototype favors simple crash/cancellation containment over model pooling and throughput. It does not claim GPU support, automatic batching, multi-host scaling or stateful agents.
- **Cancellation cannot undo effects:** registered future tasks must manage their own external side effects. A transport failure during POST has an uncertain submission outcome; the client never retries POST automatically.
- **No token streaming:** progress is polled by job ID. There is no SSE, MCP server, framework plugin or chat SDK.

See [the integration guide](docs/integration.md), [test evidence and limits](docs/testing.md), [the protocol](docs/protocol.md), [security boundaries](docs/security.md), and [contributing](CONTRIBUTING.md).

## License

Code: [MIT](LICENSE). The optional [TinyBERT reranker](https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L2-v2) is Apache-2.0 at the pinned revision. Model weights and dependency wheels are downloaded separately and retain their own licenses.
