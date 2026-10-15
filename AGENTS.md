# PHP-Python AI Bridge

A typed PHP client for a small Python service that runs bounded AI tasks (rerank, embed, redact), one
process per job, with a FrankenPHP worker-mode example and a stdio MCP server as a second client of the
same HTTP protocol. Experimental: jobs live in memory, nothing is durable, no production claim is made.
README has the overview; `docs/protocol.md`, `docs/integration.md`, `docs/mcp.md`, `docs/security.md`
and `docs/testing.md` are the guides. Everything builds, tests and downloads inside `docker/compose.yaml`.

## Layout

| Path | Holds |
|---|---|
| `src/` | The PHP client (`PhpAiBridge\`): `Client`, `Job`, the typed results, `BridgeException`. PHP `>=8.2 <8.6`, `ext-curl` and `ext-json` only. |
| `worker/ai_bridge/` | The Python 3.12 service: `server.py` (HTTP), `jobs.py` (store, limits, retention), `tasks.py` (allowlist, validation, backends, `MODEL_NAMES`). |
| `mcp_server/bridge_mcp/` | The stdio MCP server: `tools.py` (the tool list and `BACKENDS`), `protocol.py` (its client of the worker), `index.py` (the builder behind `bridge_search`). |
| `examples/` | Local autoloader, the CLI example, the FrankenPHP route and Caddyfile. |
| `scripts/` | Model fetcher, benchmark, NER measurement, MCP stdio launchers, the LLM evaluation runner. |
| `tests/php/` | `run.php` (offline contract checks), `integration.php` (the client against the real worker and broken peers over HTTP), `model.php` (ONNX smoke). |
| `tests/python/` | The worker's unittest suite, including the workflows' runners and both hooks (the pre-call push guard's own cases are in `.claude/hooks/push-guard.test.sh`). |
| `tests/mcp/` | The MCP server's suite, offline against hash-pinned wheels; its fixtures and LLM evaluations. |
| `tests/*.py` | Cross-boundary tests: `integration.py` (through the FrankenPHP worker), `model_integration.py`, `mcp_model_probe.py`, and `hostile_server.py`, a broken peer. |
| `docker/compose.yaml` | Every service, profile and limit. |
| `.github/workflows/` | `tests.yml`, the deterministic gate, and `model-smoke.yml`, the real models (not required). |

## Build and test

Docker with Linux containers and Compose; no local PHP or Python. Every compose command needs
`BRIDGE_TOKEN` in the environment (CI uses `test-only-bridge-token-never-use-in-production`; locally any
random string of 32+ characters). This is what `.github/workflows/tests.yml` runs, in order; `/check` runs it:

```bash
docker compose -f docker/compose.yaml --profile test config
docker compose -f docker/compose.yaml run --rm --no-deps python-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests
docker compose -f docker/compose.yaml run --rm --no-deps php-tests sh -ec 'find src examples tests/php -name "*.php" -print0 | xargs -0 -n1 php -l'
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration
docker compose -f docker/compose.yaml --profile test down
docker compose -f docker/compose.yaml --profile mcp --profile mcp-fetch --profile mcp-model config
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher   # network once: wheels into .cache/
docker compose -f docker/compose.yaml run --rm --no-deps mcp-tests
docker compose -f docker/compose.yaml --profile mcp --profile mcp-fetch down
```

CI runs the PHP steps on each of PHP 8.2, 8.3, 8.4 and 8.5 with `BRIDGE_PHP_IMAGE` set to the matrix's
pinned image, after first checking that the image really is `EXPECTED_PHP`; the stable `deterministic`
check requires every matrix job and the MCP job. Locally the default image is PHP 8.5; `docs/testing.md`
says how to pick another, what each layer establishes, and where CI runs. `model-smoke.yml` downloads
about 330 MB of models and wheels and is not a required check: run it only for a change on its `paths:` list.

## Rules of this repository

`CONTRIBUTING.md` is the rule book and is not repeated here. In short: PHP is `declare(strict_types=1)`,
four spaces, no `ext-mbstring`, and a warning or deprecation fails its tests; the worker path is standard
library only, with model dependencies in `requirements-model.*` and the MCP server's in
`requirements-mcp.*`; every behaviour needs a success case, invalid-input cases and an integration test
where the PHP/Python boundary is involved; a bug fix is a red-green regression test first; the PR body
records which tests actually ran. Never commit `.env`, model weights, caches or credentials; never run
downloaded model code, and never mount credentials into test containers. Keep the Composer PHP range, the CI image matrix,
compose's default image and `docs/testing.md` consistent. Public documentation changes with the contract,
and the figures the MCP tool descriptions quote come from `docs/testing.md`'s measured tables: re-measure
and update both together. Path-scoped detail is in `.claude/rules/`.

## Working rules

- **A branch delivers what it is for, nothing else.** One purpose per branch (one issue; a short name such
  as `fix/reject-empty-query`) and one PR, whose description lists the files it touches and why. Larger
  work is a feature branch: each piece is a branch whose PR targets the feature branch under every
  condition below, and the feature branch reaches `main` through one PR reviewed for how the pieces fit.
  A problem found on the way is fixed here only if this branch caused it or it stops the change meeting
  its acceptance criteria; otherwise it gets an issue and a branch of its own. No refactors, renames or
  extras the issue did not ask for.
- **Everything is tested before a merge**: `/check` locally, tests for every behaviour the change adds or
  alters, and the `deterministic` check green on GitHub. Only then may an agent merge its PR, and only
  through the PR (`gh pr merge <n> --merge --delete-branch --match-head-commit <sha>`), when an
  independent review ran and its blocking findings are fixed, a security read of the diff has nothing
  open, and the head being merged is the commit all of that ran on. Merge `main` in only for a conflict,
  or once before those final checks and review. What could not be tested is said in the PR and waits for
  the owner. Never `--auto` or `--admin`, never push to `main`: hand over the command (the pre-call
  hook `.claude/hooks/push-guard.sh` refuses these in the spellings it can read and an unreadable command
  that mentions them; script files and other programs are not read: the ruleset on `main` is the guard).
- **Reviews are text, commits are changes.** A finding is blocking when it is a defect this branch brings
  in, a missing test for what it changes, a broken house rule or a security hole; fix it with the smallest
  change. Everything else (problems already on `main`, improvements, style) becomes an issue linked from
  the PR. Findings and the reason for a fix go into the review on the PR, never into a commit per round
  or per finding: before a commit exists, everything found goes into that one commit; after a commit, a
  new finding is one more review entry and one more commit for all of it. Unrelated changes still get
  their own commits.
- Commit messages as `CONTRIBUTING.md` says (`feat: add ...`, `fix: reject ...`, `test: cover ...`), one
  logical change per commit, no emoji and no AI attribution anywhere (no `Co-Authored-By`, no "Generated
  with", in commits, PR text, comments or docs). Commits are `Cristian Girlea <cristiangirlea@gmail.com>`:
  check `git config user.email` first.
- Tests first, seen failing. Files are LF (`.gitattributes`). Secrets are never read, printed or
  committed. Small diffs: don't restyle what you didn't change. Not sure? Ask, with one proposal.
