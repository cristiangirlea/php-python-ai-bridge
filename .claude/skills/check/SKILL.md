---
name: check
description: Run what CI's deterministic gate runs (compose python, PHP, syntax, integration and MCP suites, failure drills and promtool) before a PR
---
Run the steps in order from the repository root and stop at the first failure. Report each step's
result in one line; on a failure, show the error text and fix it before re-running.

`BRIDGE_TOKEN` must be set to a disposable random string of 32+ characters first; every compose command
refuses to start without it, and the `config` steps print it, so never run them with a real token in the
shell. The MCP step needs `.cache/wheels` from one network-enabled `mcp-fetcher`
run; it is then offline. On Linux, `mkdir -p .cache && chmod 777 .cache` first so container UID 65532
can write there.

The failure-drill driver uses only project `bridge-drills` and the `drills` profile, cleans up its own peers,
and runs no models. It reports seven verdicts and an explicit start-crash-loop skip; a skip is documented
coverage, not a pass. `promtool` checks the six alert rules and tests their behavior in a digest-pinned Prometheus image, offline.

```bash
docker compose -f docker/compose.yaml --profile test config
docker compose -f docker/compose.yaml run --rm --no-deps python-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests sh -ec 'find src examples tests/php -name "*.php" -print0 | xargs -0 -n1 php -l'
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile tokens-worker
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration
docker compose -f docker/compose.yaml --profile test down

docker compose -p bridge-drills -f docker/compose.yaml --profile drills config -q
sh scripts/failure_drills.sh
docker compose -p bridge-drills -f docker/compose.yaml --profile drills run --rm --no-deps promtool
docker compose -p bridge-drills -f docker/compose.yaml --profile drills down -v
docker compose -f docker/compose.yaml --profile mcp --profile mcp-fetch --profile mcp-model --profile php-dev --profile py-dev --profile php-frameworks --profile tls config
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps mcp-tests
docker compose -f docker/compose.yaml run --rm --no-deps py-dev-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps py-analysis
docker compose -f docker/compose.yaml run --rm --no-deps composer-install
docker compose -f docker/compose.yaml run --rm --no-deps php-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps php-frameworks-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps php-analysis
docker compose -f docker/compose.yaml run --rm --no-deps php-psr18-tests
docker compose -f docker/compose.yaml up -d worker
docker compose -f docker/compose.yaml run --rm --no-deps laravel-tests
docker compose -f docker/compose.yaml run --rm --no-deps symfony-tests
docker compose -f docker/compose.yaml up -d tls-proxy
docker compose -f docker/compose.yaml run --rm --no-deps tls-check
docker compose -f docker/compose.yaml --profile mcp --profile mcp-fetch --profile php-dev --profile py-dev --profile php-frameworks --profile tls down
```

The `composer-install` step is the consumer-install check: `composer validate --strict` on the library, then a
throwaway consumer project in tmpfs installs its `git archive` zip from an `artifact` repository (or from a path
repository in a git worktree whose git directory is outside the mount) with Packagist disabled, checks what shipped in
`vendor/`, and autoloads `PhpAiBridge\Client`; the archive is built from the committed `HEAD`, so an uncommitted change
is not what it tests, and it needs no network and no cache.

`py-dev-fetcher` downloads the mypy and ruff wheels `requirements-dev.lock` pins into `.cache/wheels`, with network,
and `py-analysis` installs them and the MCP server's wheels from `mcp-fetcher` into a tmpfs and runs, offline,
`ruff check` over `worker`, `mcp_server`, `scripts` and `tests` and `mypy --strict` over `worker` and `mcp_server`,
both configured in `pyproject.toml`. Fix a finding with annotations or a small guard, never a behaviour change.

`php-fetcher` installs the PHPStan and the PSR-7/17/18 interfaces that `composer.lock` pins into
`.cache/php-vendor`, with network, and `php-analysis` runs PHPStan offline at level max over `src`, `examples`
and `tests/php` with `phpstan.neon`; `phpstan-baseline.neon` holds the findings left on purpose, each with its
reason. `php-psr18-tests` reruns `tests/php/run.php` with that vendor directory on the include path, so the
PSR-18 transport checks that `php-tests` skips (with one printed line) run too, offline.

`php-frameworks-fetcher` installs the Laravel and Symfony versions `tests/php/frameworks/composer.lock` pins into
`.cache/php-frameworks-vendor`, with network, the library linked from the checkout; PHPStan loads that autoloader, so
it runs before `php-analysis`. `laravel-tests` boots a Laravel application that discovers the provider and publishes
its configuration, and `symfony-tests` a bare Symfony kernel with the bundle; both resolve the client and rerank
against `worker`, offline. If the Laravel test says the lock no longer carries the library's `composer.json`, lock
it again as `docs/testing.md` says.

`tls-proxy` is Caddy in front of `worker` on `:8443` with `tls internal`, and `tls-check` runs `tests/php/tls.php`
twice with the system CA store emptied by a tmpfs, so the client trusts only the example root: once with
`curl.cainfo` set to that root from the `tls-ca` volume (the store is empty, a rerank over HTTPS, 401 for a wrong token
and for no `Authorization` header, Caddy's 400 for plain HTTP on the TLS port, nothing listening on port 80), once
without it, where the client must fail as `transport_error`. `down -v` removes the volume and with it the example CA.

The `python-tests` step also runs both hooks' tests (`tests/python/test_repo_rules_hook.py` and
`tests/python/test_push_guard_hook.py`, which runs `.claude/hooks/push-guard.test.sh`); the containers
mount `/tmp` noexec, so the guard's killed-git case is skipped there, with its skip line printed in the
`python-tests` output, and runs only on a host.

This runs the PHP steps on compose's default image, PHP 8.5. CI runs them on 8.2, 8.3, 8.4 and 8.5
and first checks that the image's PHP minor is the one the matrix expects; to reproduce another branch,
set `BRIDGE_PHP_IMAGE` to its pinned image from `.github/workflows/tests.yml` and rerun the PHP and
integration steps. Report the other three as "left to CI". If the integration step fails, read
`docker compose -f docker/compose.yaml logs --tail 100` before `down`.

The real-model smoke tests (`model-smoke.yml`) are not part of this: they download about 630 MB of models plus pinned wheels and run
only for a change on that workflow's `paths:` list, through `fetcher`, `model-test` and
`model-integration` as README describes.

This skill does not merge: merging is through the PR, under AGENTS.md's conditions.
