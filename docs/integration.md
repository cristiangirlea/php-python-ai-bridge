# Integrating the bridge

This guide covers the existing framework-independent client. Dedicated Symfony bundles and Laravel packages do not exist yet. The manual framework examples below are configuration recipes, not framework integration tests or a promise of production readiness.

## What runs where

```text
Browser -> your authenticated PHP application -> private Python service
                   |                                 |
                   | return 202 + application job ID  | queue -> child process
                   |                                 |          -> result
Browser -> your application's status endpoint ------> GET job
```

Composer installs and autoloads the PHP client. It does not install Python, download a model or start a service. Run the Python service separately using the reviewed [Docker setup](../README.md#try-it-with-docker); configure its private address and service token in your PHP application. FrankenPHP is an optional PHP runtime, not a requirement of the client.

Two normal tasks are implemented. Reranking: given a query and up to 512 candidate documents, it returns document indexes sorted by relevance, optionally narrowed to the `top_k` best. Embedding: up to 32 texts return one unit-length vector each, for an index your application owns. It is not a chat/completions API. The default backends are deterministic demos, not AI; the optional ONNX profile runs the real models offline after a separate download step.

## Install through Composer during evaluation

There is no tagged release or Packagist package yet. The implementation is on `main`. In your application's root `composer.json`, add this repository and requirement, preserving existing entries:

```json
{
  "repositories": [
    {
      "type": "vcs",
      "url": "https://github.com/cristiangirlea/php-python-ai-bridge"
    }
  ],
  "require": {
    "cristiangirlea/php-python-ai-bridge": "dev-main"
  }
}
```

Run `composer update cristiangirlea/php-python-ai-bridge --no-scripts --no-plugins` inside your application's dependency-acquisition container. Review and commit the resulting `composer.lock` to pin the evaluated revision. This command deliberately skips application hooks/plugins; perform any reviewed framework setup separately in your isolated application environment. No global `minimum-stability: dev` change is necessary for this explicit development requirement. See [Composer's VCS repository documentation](https://getcomposer.org/doc/05-repositories.md#vcs).

After a release is published to Packagist, the custom repository/branch constraint can be replaced by a released version. That publication has not happened yet. A clean Composer installation is a separate release gate; the dependency-free examples in this repository do not prove it.

The client needs PHP 8.2–8.5 and `ext-curl`/`ext-json`. See [support and verification](testing.md) for the tested matrix and limitations. The selected framework may require a newer PHP version than this library.

## Configure the private service

Set these in your deployment's secret/configuration mechanism; never commit a real token:

```dotenv
BRIDGE_URL=http://worker:8090
BRIDGE_TOKEN=<replace-with-a-random-service-token>
BRIDGE_REQUEST_TIMEOUT_MS=2000
```

The placeholder is not a usable token. Generate at least 32 non-whitespace ASCII characters, for example using the commands in the README, and give the same token to PHP and Python. `worker` resolves only for containers attached to the Compose network; it is not a hostname on your laptop. Do not point the client at the FrankenPHP demo's port 8080: the client talks directly to Python on port 8090.

The supplied Compose file publishes no host ports. To evaluate your own app, attach its container to the same private network (the default project creates `php-python-ai-bridge_isolated`; a custom Compose project name changes that prefix). For cross-host deployment, use an authenticated TLS boundary. Do not expose the demo or Python HTTP server directly to the internet. Configure a trusted origin, never a URL supplied by an end user.

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

## Symfony: manual service registration

Add this service to your existing `config/services.yaml`:

```yaml
services:
    PhpAiBridge\Client:
        arguments:
            $baseUrl: '%env(BRIDGE_URL)%'
            $token: '%env(BRIDGE_TOKEN)%'
            $requestTimeoutMs: '%env(int:BRIDGE_REQUEST_TIMEOUT_MS)%'
```

With normal application autowiring enabled, constructor-inject `PhpAiBridge\Client` into an application service or controller. No bundle or Flex recipe is needed for this manual setup. Keep secrets in deployment environment variables or Symfony's secret management; do not put a token in committed YAML. See [Symfony service configuration](https://symfony.com/doc/current/service_container.html) and [environment processors](https://symfony.com/doc/current/configuration/env_var_processors.html).

## Laravel: manual container binding

Create `config/ai_bridge.php` in your application:

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

Constructor-inject `PhpAiBridge\Client` into your service/controller. This is not an auto-discovered package provider or facade. Read `env()` only in configuration files so Laravel configuration caching works; never expose cached configuration containing secrets. See [Laravel service providers](https://laravel.com/docs/13.x/providers) and [configuration caching](https://laravel.com/docs/13.x/configuration#configuration-caching). These snippets use manual container APIs, but no Laravel version matrix has been executed yet.

## Application HTTP lifecycle and ownership

Implement the following in your application, for either framework:

1. Authenticate the user, validate the query/documents, enforce quotas and CSRF protection where applicable.
2. Call `submitRerank()`. Persist an association between an application-owned job ID, the current user, the returned bridge ID and the original document ordering. Keep sensitive documents according to your application's retention policy.
3. Return HTTP 202 promptly. A queued response means accepted, not completed. Define recovery for a database failure after submission; remote submission and your database write are not atomic.
4. On a later status request, verify ownership **before** calling `get()`. Handle `queued`/`running`, `succeeded`, `failed`, `timed_out` and `cancelled`. For success, validate using `RerankResult::fromJob()` with the original document count; indexes refer to the original order.
5. Authorize cancellation the same way before calling `cancel()`. A job may complete before cancellation arrives; the returned snapshot is authoritative.

The service token authorizes every job. Do not send it to a browser or rely on random job IDs for authorization. Do not blindly publish the example routes as your production API. Capture required results before retention expires; the worker is in-memory and restart loses all jobs/results. A 404 cannot distinguish expiry, restart loss and an unknown job.

PHP objects reused by a long-lived worker must not retain per-user inputs, authentication state or results. The supplied FrankenPHP test checks the standalone demo's request isolation; it is not a Symfony Runtime or Laravel Octane test.

## Timeouts and failures

| Setting/outcome | Meaning | Application handling |
| --- | --- | --- |
| Client `requestTimeoutMs` | Maximum duration of one HTTP call; default 2000 ms | Keep separate from the job deadline |
| Submission `timeoutMs` | Queue + process startup + execution; default 30000 ms | Choose a bound appropriate to the task |
| `waitTimeoutMs` / `wait_timeout` | Local polling budget exhausted | Remote job may still run; inspect it or explicitly cancel |
| `transport_error` during submission | Response unknown; a job may have been created | Do not automatically retry: no idempotency keys exist |
| `http_error`, status 401 | Service credentials rejected | Fix server-side configuration, do not forward service secrets |
| `http_error`, status 400/413 | Invalid or oversized request | Reject/correct input |
| `http_error`, status 429 | Total queued/running/retained capacity exhausted | Apply backpressure; completed retained jobs also occupy capacity |
| `invalid_response` | Peer violated the client contract | Treat as an integration failure, not a valid task result |
| Terminal `failed` or `timed_out` | Task did not succeed | Report a safe failure; retries require an application policy |

The current default limits are two simultaneous task processes, 128 total job slots and 300 seconds of terminal retention. These are settings, not measured throughput or a production SLA. The model profile uses one task process at a time and loads the model anew per job.

## Before shipping a framework integration

Run a fresh Composer installation, framework container boot/configuration-cache tests, authenticated submit/status/cancel tests, cross-user ownership rejection and reused-worker isolation tests in your target PHP/framework versions. Test the real model if enabled. See [verification status and missing measurements](testing.md). Do not add Messenger or Laravel queue retries without addressing uncertain submission outcomes and duplicate work.
