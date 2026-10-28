# Integrating the bridge

This guide covers the framework-independent client and the two optional integrations shipped in the same package: a Laravel service provider and a Symfony bundle, each of which builds `PhpAiBridge\Client` from a URL and a token. Neither framework is a dependency of the library; install the bridge into an application that already has one. The manual recipes kept below each integration are configuration examples that no test runs, and nothing here is a promise of production readiness.

## What runs where

```text
Browser -> your authenticated PHP application -> private Python service
                   |                                 |
                   | return 202 + application job ID  | queue -> child process
                   |                                 |          -> result
Browser -> your application's status endpoint ------> GET job
```

Composer installs and autoloads the PHP client. It does not install Python, download a model or start a service. Run the Python service separately using the reviewed [Docker setup](../README.md#try-it-with-docker); configure its private address and service token in your PHP application. FrankenPHP is an optional PHP runtime, not a requirement of the client.

Three normal tasks are implemented. Reranking: given a query and up to 512 candidate documents, it returns document indexes sorted by relevance, optionally narrowed to the `top_k` best. Embedding: up to 32 texts return one unit-length vector each, for an index your application owns. Redaction: one text returns a masked copy and the spans found, from checksum and pattern rules on every backend and a named-entity model on the ONNX profile. It is not a chat/completions API. The default backends are deterministic demos, not AI; the optional ONNX profile runs the real models offline after a separate download step.

## Install through Composer

The latest tagged release is `v0.4.0`; see [the changelog](../CHANGELOG.md). The package is on Packagist; run this inside your application's dependency-acquisition container:

```sh
composer require cristiangirlea/php-python-ai-bridge:^0.4 --no-scripts --no-plugins
```

To pin an unreleased commit instead, add this repository and requirement to your application's root `composer.json`, preserving existing entries and replacing the example hash with the commit you want:

```json
{
  "repositories": [
    {
      "type": "vcs",
      "url": "https://github.com/cristiangirlea/php-python-ai-bridge"
    }
  ],
  "require": {
    "cristiangirlea/php-python-ai-bridge": "dev-main#<commit-hash>"
  }
}
```

Then run `composer update cristiangirlea/php-python-ai-bridge --no-scripts --no-plugins`. Either way, review and commit the resulting `composer.lock` to pin the installed revision. These commands deliberately skip application hooks/plugins; perform any reviewed framework setup separately in your isolated application environment. A tagged version needs no `minimum-stability` change; the `dev-main` requirement permits this development version explicitly. See [Composer's VCS repository documentation](https://getcomposer.org/doc/05-repositories.md#vcs).

A clean Composer installation of the library is checked on every push to `main` and every pull request: CI's `composer-install` service validates `composer.json` strictly, then installs the library's `git archive` zip into a throwaway consumer project from an `artifact` repository (or from a path repository in a git worktree whose git directory is outside the mount), offline, checks what shipped in `vendor/`, and autoloads the client; see [testing](testing.md#what-the-tests-establish). That proves the package installs and autoloads, not that any framework boots with it.

The client needs PHP 8.2–8.5 and `ext-curl`/`ext-json`. See [support and verification](testing.md) for the tested matrix and limitations. The selected framework may require a newer PHP version than this library.

## Configure the private service

Set these in your deployment's secret/configuration mechanism; never commit a real token:

```dotenv
BRIDGE_URL=http://worker:8090
BRIDGE_TOKEN=<replace-with-a-random-service-token>
BRIDGE_REQUEST_TIMEOUT_MS=2000
```

The placeholder is not a usable token. Generate at least 32 non-whitespace ASCII characters, for example using the commands in the README, and give the same token to PHP and Python. `worker` resolves only for containers attached to the Compose network; it is not a hostname on your laptop. Do not point the client at the FrankenPHP demo's port 8080: the client talks directly to Python on port 8090.

The supplied Compose file publishes no host ports. To evaluate your own app, attach its container to the same private network (the default project creates `php-python-ai-bridge_isolated`; a custom Compose project name changes that prefix). To deploy the worker without a checkout, run its [published image](../README.md#run-the-published-image) on a private network your application joins. For cross-host deployment, use an authenticated TLS boundary, as [Cross-host use behind TLS](#cross-host-use-behind-tls) shows. Do not expose the demo or Python HTTP server directly to the internet. Configure a trusted origin, never a URL supplied by an end user.

## Cross-host use behind TLS

When the application and the worker run on different hosts, put a TLS proxy in front of the worker on the worker's host and point the client at it; the plain-HTTP hop is then only the one inside that host:

```text
application host                         worker host
PHP client --- HTTPS + Bearer token ---> TLS proxy :8443 --- HTTP ---> worker :8090
```

The proxy terminates TLS and passes the request through, `Authorization` header included; it holds no token and checks none, and the worker checks the token as it does on a private network. Compose's `tls` profile is a worked example: `tls-proxy` runs Caddy from the pinned FrankenPHP image with [examples/tls/Caddyfile](../examples/tls/Caddyfile) (`tls internal`, so Caddy signs the site certificate with its own local CA, no admin endpoint, no HTTP listener and no redirect), and `tls-check` runs [tests/php/tls.php](../tests/php/tls.php) with the unchanged client against `https://tls-proxy:8443`, trusting only that CA's root through PHP's settings:

```sh
docker compose -f docker/compose.yaml --profile tls up -d tls-proxy worker
docker compose -f docker/compose.yaml run --rm --no-deps tls-check
docker compose -f docker/compose.yaml --profile tls down -v
```

The check proves that a rerank completes over HTTPS with the certificate verified against the example CA, that a wrong token and a request with no `Authorization` header both get the worker's 401 through the proxy, that the client trusts only the example root (the check empties the system CA store, and without the example CA the client fails as `transport_error`), and that the proxy serves no plain HTTP: `http://` to its TLS port gets Caddy's own 400 and reaches nothing, and port 80 has no listener. An `http://` URL still sends the token in clear before that 400, so configure the client with `https://` only. Both containers run on the internal network with no published port; the CA lives on a tmpfs volume that `down -v` removes, and `tls-check` mounts that whole CA directory read-only, its keys included, which only a throwaway example may do.

For production, change what the example takes for granted:

- **Certificate:** one from your CA or from ACME for the worker host's real name instead of `tls internal`, and the client's `BRIDGE_URL` naming that host.
- **Trust store:** give the client only the CA that signs the proxy's certificate (`curl.cainfo` and `openssl.cafile` in `php.ini`, or the system store if a public CA signs it), never the CA's key; never turn verification off.
- **Binding:** publish the proxy on the worker host's private interface only (for example `127.0.0.1` or a private address in a port mapping), firewall it to the application hosts, and keep the worker itself unpublished.
- **Trust in the proxy:** it sees every token and payload in clear, so run, patch and restrict it like the worker; it must not log `Authorization`.
- **Beyond the example:** mutual TLS, a client certificate checked by the proxy in addition to the token, is the next step; the example does not test it.

## Plain PHP

With Composer installed in your application, load `vendor/autoload.php` instead of this repository's example autoloader:

```php
<?php

declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

$client = new Client(
    getenv('BRIDGE_URL') ?: throw new RuntimeException('Set BRIDGE_URL'),
    getenv('BRIDGE_TOKEN') ?: throw new RuntimeException('Set BRIDGE_TOKEN'),
    requestTimeoutMs: 2000,
);
$documents = ['Saturn has rings.', 'Paris is the capital of France.'];
$job = $client->submitRerank('What is the capital of France?', $documents);

// CLI/background example only: do not block an HTTP worker polling for a result.
$done = $client->wait($job->id, waitTimeoutMs: 30000);
if ($done->status !== 'succeeded') {
    throw new RuntimeException('Reranking ended with status: ' . $done->status);
}
$result = RerankResult::fromJob($done, count($documents));
echo $documents[$result->rankings[0]['index']];
```

Handle `InvalidArgumentException` for local validation and `PhpAiBridge\BridgeException` for transport/protocol errors. Remote task failure is a terminal job status, not necessarily an exception from `get()` or `wait()`.

## Through a PSR-18 HTTP client

By default the client talks to the service through `ext-curl`. An application that already routes its HTTP through a [PSR-18](https://www.php-fig.org/psr/psr-18/) client can pass `PhpAiBridge\Psr18Transport` as the constructor's last argument instead. It takes the PSR-18 client, a PSR-17 request factory and a PSR-17 stream factory. The library does not require the PSR packages (`composer.json` only suggests `psr/http-client` and `psr/http-factory`); the HTTP client you install brings them.

Configure that client for the bridge before handing it over:

- **Redirects off.** The service never redirects. A client that follows a redirect would send the service token to wherever the `Location` header points, before the bridge sees anything. `Psr18Transport` refuses any 3xx response it receives as `invalid_response`, but it cannot see a redirect the client has already followed.
- **No proxy from the environment** for the service's private address, as the cURL path does.
- **The client's own timeout applies.** PSR-18 has no per-request timeout, so `requestTimeoutMs` and the per-poll budget `wait()` computes are not enforced by this transport. Set the client's timeout at or below `requestTimeoutMs`. `wait()` still checks its deadline between polls and reports a transport failure past that deadline as `wait_timeout`, but one poll can outlast the wait budget by up to the client's timeout.
- **The 262144-byte response cap bounds what the bridge reads**, not what the client buffers: `Psr18Transport` reads at most one byte past the cap from the body stream and refuses a larger body as `invalid_response`. A client that buffers the whole response first (as Guzzle does for `sendRequest()`) has already received it.

A client exception (`Psr\Http\Client\ClientExceptionInterface`) or a failure while reading the body becomes `BridgeException` `transport_error`, without the client's message. As with cURL, a failed submission's outcome is unknown: resubmit only with the same idempotency key (see below), never without one. Other statuses, the content type and the JSON are checked by `Client` exactly as on the cURL path. A 429's `Retry-After` is read from the PSR-7 response by the same rule as cURL's (exactly one header of 1 to 6 digits, else null), so `retryAfterSeconds` is the same through either transport. The 16 KiB response-header cap is the cURL transport's: a PSR-18 client has parsed the headers before the bridge sees them.

In both examples `$url` and `$token` are read from the environment as in the plain PHP example above. Symfony HttpClient's `Psr18Client` is the PSR-18 client and both PSR-17 factories at once. It needs a PSR-7 implementation such as `nyholm/psr7`:

```php
use PhpAiBridge\Client;
use PhpAiBridge\Psr18Transport;
use Symfony\Component\HttpClient\HttpClient;
use Symfony\Component\HttpClient\Psr18Client;

$http = new Psr18Client(HttpClient::create([
    'max_redirects' => 0,
    'no_proxy' => '*',
    'timeout' => 2.0,
    'max_duration' => 2.0,
]));
$client = new Client($url, $token, 2000, new Psr18Transport($http, $http, $http));
```

Guzzle 7's client implements PSR-18 itself, and `guzzlehttp/psr7` ships the PSR-17 factories:

```php
use GuzzleHttp\Psr7\HttpFactory;
use PhpAiBridge\Client;
use PhpAiBridge\Psr18Transport;

$http = new \GuzzleHttp\Client(['allow_redirects' => false, 'proxy' => '', 'connect_timeout' => 1.0, 'timeout' => 2.0]);
$factory = new HttpFactory();
$client = new Client($url, $token, 2000, new Psr18Transport($http, $factory, $factory));
```

The repository's tests drive `Psr18Transport` through small in-repo fakes of the PSR interfaces (see [testing](testing.md)); neither Symfony HttpClient nor Guzzle is installed or run by them, so treat these two snippets as configuration recipes.

## Symfony: the bundle

After `composer require cristiangirlea/php-python-ai-bridge`, register the bundle in `config/bundles.php` (there is no Flex recipe, so Flex does not add it):

```php
return [
    // ...
    PhpAiBridge\Symfony\PhpAiBridgeBundle::class => ['all' => true],
];
```

Set `BRIDGE_URL` and `BRIDGE_TOKEN` in the deployment environment, and `BRIDGE_REQUEST_TIMEOUT_MS` if 2000 ms does not suit you. Those are the defaults of the bundle's configuration; write `config/packages/php_ai_bridge.yaml` only to read other variables or to fix a value:

```yaml
php_ai_bridge:
    url: '%env(default::BRIDGE_URL)%'
    token: '%env(default::BRIDGE_TOKEN)%'
    request_timeout_ms: '%env(int:BRIDGE_REQUEST_TIMEOUT_MS)%'
```

The bundle registers `PhpAiBridge\Client` as a shared service under its class name, so constructor-inject it into a service or controller by type. Environment values are read when the client is first resolved: an unset or empty URL or token, a URL that is not a bare HTTP(S) origin, a token under 32 characters or a timeout outside 1 to 300000 fail with an `InvalidArgumentException` naming the `php_ai_bridge` setting. An empty URL or an out-of-range timeout written in the configuration fails the container build. No message quotes the token. Keep the token out of committed YAML; use deployment environment variables or Symfony's secret management.

`symfony-tests` (see [testing](testing.md)) boots a bare `Kernel` with only this bundle, no FrameworkBundle, with debug off: it compiles and dumps the container, resolves the client and a service autowired with it, reranks against the real worker, loads the dumped container in a second kernel and resolves the client again, and checks every invalid case above. It runs Symfony 8.0 on PHP 8.5 only. It is not a full Symfony application: no FrameworkBundle, Flex, Messenger, Symfony Runtime or FrankenPHP worker mode is tested, and no older Symfony version, though the bundle uses only `AbstractBundle`, which Symfony has had since 6.1.

### Without the bundle: manual service registration

Add this service to your existing `config/services.yaml`:

```yaml
services:
    PhpAiBridge\Client:
        arguments:
            $baseUrl: '%env(BRIDGE_URL)%'
            $token: '%env(BRIDGE_TOKEN)%'
            $requestTimeoutMs: '%env(int:BRIDGE_REQUEST_TIMEOUT_MS)%'
```

With normal application autowiring enabled, constructor-inject `PhpAiBridge\Client` as above. See [Symfony service configuration](https://symfony.com/doc/current/service_container.html) and [environment processors](https://symfony.com/doc/current/configuration/env_var_processors.html).

## Laravel: the service provider

`composer require cristiangirlea/php-python-ai-bridge` is enough: Laravel's package discovery registers `PhpAiBridge\Laravel\BridgeServiceProvider` from the package's `composer.json`. Set `BRIDGE_URL`, `BRIDGE_TOKEN` and, optionally, `BRIDGE_REQUEST_TIMEOUT_MS` (default 2000) in `.env` or the deployment environment. To change how they are read, publish the configuration:

```sh
php artisan vendor:publish --tag=ai-bridge-config
```

That writes `config/ai_bridge.php`, the only place the provider's settings call `env()`, so `php artisan config:cache` keeps working; never expose cached configuration, since it holds the token. The provider binds one `PhpAiBridge\Client` per application as a singleton: constructor-inject it by type. Settings are checked when the client is first resolved: a missing or empty URL or token, a URL that is not a bare HTTP(S) origin, a token under 32 characters, or a timeout that is not a whole number from 1 to 300000 fails with an `InvalidArgumentException` naming the setting, as in `ai_bridge.token (BRIDGE_TOKEN)`, never with the token's value.

`laravel-tests` (see [testing](testing.md)) boots a Laravel 13 `Application` without a skeleton on PHP 8.5, with the provider coming from the package manifest Laravel builds out of Composer's `installed.json`, never from a `register()` call: it runs `vendor:publish` for the tag, resolves the client twice and gets one instance, reranks against the real worker, boots again from a configuration cache written as `config:cache` writes it, with the environment changed so only the cache can be right, and checks every invalid case above. It is not a full skeleton application, and no Octane, queue worker, facade or other Laravel version is tested.

### Without discovery: manual container binding

If your application lists the package under `extra.laravel.dont-discover`, create `config/ai_bridge.php` yourself:

```php
<?php

return [
    'url' => env('BRIDGE_URL'),
    'token' => env('BRIDGE_TOKEN'),
    'request_timeout_ms' => (int) env('BRIDGE_REQUEST_TIMEOUT_MS', 2000),
];
```

Add this binding inside the `register()` method of your existing, registered `AppServiceProvider`; keep the rest of the provider unchanged:

```php
$this->app->bind(\PhpAiBridge\Client::class, static function ($app): \PhpAiBridge\Client {
    $settings = $app['config']->get('ai_bridge');

    return new \PhpAiBridge\Client(
        $settings['url'],
        $settings['token'],
        $settings['request_timeout_ms'],
    );
});
```

Read `env()` only in configuration files so configuration caching works. See [Laravel service providers](https://laravel.com/docs/13.x/providers) and [configuration caching](https://laravel.com/docs/13.x/configuration#configuration-caching).

## Application HTTP lifecycle and ownership

Implement the following in your application, for either framework:

1. Authenticate the user, validate the query/documents, enforce quotas and CSRF protection where applicable.
2. Call `submitRerank()`. Persist an association between an application-owned job ID, the current user, the returned bridge ID and the original document ordering. Keep sensitive documents according to your application's retention policy.
3. Return HTTP 202 promptly. A queued response means accepted, not completed. Define recovery for a database failure after submission; remote submission and your database write are not atomic.
4. On a later status request, verify ownership **before** calling `get()`. Handle `queued`/`running`, `succeeded`, `failed`, `timed_out` and `cancelled`. For success, validate using `RerankResult::fromJob()` with the original document count; indexes refer to the original order.
5. Authorize cancellation the same way before calling `cancel()`. A job may complete before cancellation arrives; the returned snapshot is authoritative.

A service token reaches every job submitted with it, whichever of your users it was for; give each application its own ([tokens](protocol.md#tokens)). Do not send it to a browser or rely on random job IDs for authorization. Do not blindly publish the example routes as your production API. Capture required results before retention expires. Without `BRIDGE_STATE_DIR` the worker is in-memory and a restart loses all jobs and results; with it, finished results survive a restart until retention ends, and jobs that were unfinished become `failed` with `service_restarted`. A 404 cannot distinguish expiry, restart loss and an unknown job.

When a submission's outcome is uncertain (`transport_error`, or the PHP process died before it stored the job ID), resubmit with the same idempotency key instead of a fresh request: the worker returns the job the first request created, in whatever state it is now, or creates it if the first request never arrived. Pick the key per unit of work (for example a hash of your own record ID and the task) and store it before the first POST:

```php
$key = 'rerank-' . hash('sha256', $documentSetId);
try {
    $job = $client->submitRerank($query, $documents, idempotencyKey: $key);
} catch (PhpAiBridge\BridgeException $e) {
    if ($e->errorCode !== 'transport_error') {
        throw $e;
    }
    $job = $client->submitRerank($query, $documents, idempotencyKey: $key);
}
```

With a key, `submit()` accepts any valid status in the response, since a repeat can return a running or finished job. A key only covers the job's retention: once it expires, the same key runs the task again. A job that failed with `service_restarted` is also retained, so the same key returns that failure until retention ends: resubmit it with a new key.

PHP objects reused by a long-lived worker must not retain per-user inputs, authentication state or results. The supplied FrankenPHP test checks the standalone demo's request isolation; it is not a Symfony Runtime or Laravel Octane test.

## Timeouts and failures

| Setting/outcome | Meaning | Application handling |
| --- | --- | --- |
| Client `requestTimeoutMs` | Maximum duration of one HTTP call; default 2000 ms | Keep separate from the job deadline |
| Submission `timeoutMs` | Queue + process startup + execution; default 30000 ms | Choose a bound appropriate to the task |
| `waitTimeoutMs` / `wait_timeout` | Local polling budget exhausted | Remote job may still run; inspect it or explicitly cancel |
| `transport_error` during submission | Response unknown; a job may have been created | Resubmit with the same `idempotencyKey` while the job would still be retained; never retry without one |
| `http_error`, status 401 | Service credentials rejected | Fix server-side configuration, do not forward service secrets |
| `http_error`, status 400/413 | Invalid or oversized request | Reject/correct input |
| `http_error`, status 429 | Total queued/running/retained capacity exhausted; no job was created | Apply backpressure; resubmit no sooner than `retryAfterSeconds` (below). Completed retained jobs also occupy capacity |
| `invalid_response` | Peer violated the client contract | Treat as an integration failure, not a valid task result |
| Terminal `failed` or `timed_out` | Task did not succeed | Report a safe failure; retries require an application policy |

When the worker is full, `submit()` throws a `BridgeException` with `httpStatus` 429 and, from the worker's `Retry-After` header, `retryAfterSeconds`: when capacity is expected back. The client never retries by itself. Unlike a `transport_error`, a 429 means no job exists, so scheduling the submission again is safe; how is the application's choice, for instance a delayed message on its own queue:

```php
try {
    $job = $client->submitRerank($query, $documents);
} catch (\PhpAiBridge\BridgeException $error) {
    if ($error->httpStatus !== 429) {
        throw $error;
    }
    // Null when the header was missing or malformed (a proxy answered, say): fall back to your own delay.
    $delaySeconds = $error->retryAfterSeconds ?? 5;
    $this->queue->dispatchLater(new SubmitRerank($applicationJobId), $delaySeconds);
    return new Response('', 503, ['Retry-After' => (string) $delaySeconds]);
}
```

`dispatchLater()` and `SubmitRerank` stand for your framework's delayed dispatch (Symfony Messenger's `DelayStamp`, Laravel's `->delay()`); cap the attempts, since a worker kept full by other callers can refuse again.

The current default limits are two warm task processes, 128 total job slots and 300 seconds of terminal retention. These are settings, not measured throughput or a production SLA. The model profile keeps one task process, which loads each model on its first job of that kind and keeps it.

## Before shipping a framework integration

Run a fresh Composer installation, framework container boot/configuration-cache tests, authenticated submit/status/cancel tests, cross-user ownership rejection and reused-worker isolation tests in your target PHP/framework versions. Test the real model if enabled. See [verification status and missing measurements](testing.md). Do not add Messenger or Laravel queue retries without an idempotency key per message, or uncertain submission outcomes become duplicate work.
