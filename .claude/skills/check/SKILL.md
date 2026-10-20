---
name: check
description: Run what CI's deterministic gate runs (compose python, PHP, syntax, integration and MCP suites) before a PR
---
Run the steps in order from the repository root and stop at the first failure. Report each step's
result in one line; on a failure, show the error text and fix it before re-running.

`BRIDGE_TOKEN` must be set to a disposable random string of 32+ characters first; every compose command
refuses to start without it, and the `config` steps print it, so never run them with a real token in the
shell. The MCP step needs `.cache/wheels` from one network-enabled `mcp-fetcher`
run; it is then offline. On Linux, `mkdir -p .cache && chmod 777 .cache` first so container UID 65532
can write there.

```bash
docker compose -f docker/compose.yaml --profile test config
docker compose -f docker/compose.yaml run --rm --no-deps python-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests sh -ec 'find src examples tests/php -name "*.php" -print0 | xargs -0 -n1 php -l'
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration
docker compose -f docker/compose.yaml --profile test down
docker compose -f docker/compose.yaml --profile mcp --profile mcp-fetch --profile mcp-model --profile php-dev config
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps mcp-tests
docker compose -f docker/compose.yaml run --rm --no-deps composer-install
docker compose -f docker/compose.yaml run --rm --no-deps php-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps php-analysis
docker compose -f docker/compose.yaml run --rm --no-deps php-psr18-tests
docker compose -f docker/compose.yaml --profile mcp --profile mcp-fetch --profile php-dev down
```

The `composer-install` step is the consumer-install check: `composer validate --strict` on the library, then a
throwaway consumer project in tmpfs installs its `git archive` zip from an `artifact` repository (or from a path
repository in a git worktree whose git directory is outside the mount) with Packagist disabled, checks what shipped in
`vendor/`, and autoloads `PhpAiBridge\Client`; the archive is built from the committed `HEAD`, so an uncommitted change
is not what it tests, and it needs no network and no cache.

`php-fetcher` installs the PHPStan and the PSR-7/17/18 interfaces that `composer.lock` pins into
`.cache/php-vendor`, with network, and `php-analysis` runs PHPStan offline at level max over `src`, `examples`
and `tests/php` with `phpstan.neon`; `phpstan-baseline.neon` holds the findings left on purpose, each with its
reason. `php-psr18-tests` reruns `tests/php/run.php` with that vendor directory on the include path, so the
PSR-18 transport checks that `php-tests` skips (with one printed line) run too, offline.

The `python-tests` step also runs both hooks' tests (`tests/python/test_repo_rules_hook.py` and
`tests/python/test_push_guard_hook.py`, which runs `.claude/hooks/push-guard.test.sh`); the containers
mount `/tmp` noexec, so the guard's killed-git case is skipped there, with its skip line printed in the
`python-tests` output, and runs only on a host.

This runs the PHP steps on compose's default image, PHP 8.5. CI runs them on 8.2, 8.3, 8.4 and 8.5
and first checks that the image's PHP minor is the one the matrix expects; to reproduce another branch,
set `BRIDGE_PHP_IMAGE` to its pinned image from `.github/workflows/tests.yml` and rerun the PHP and
integration steps. Report the other three as "left to CI". If the integration step fails, read
`docker compose -f docker/compose.yaml logs --tail 100` before `down`.

The real-model smoke tests (`model-smoke.yml`) are not part of this: they download about 330 MB and run
only for a change on that workflow's `paths:` list, through `fetcher`, `model-test` and
`model-integration` as README describes.

This skill does not merge: merging is through the PR, under AGENTS.md's conditions.
