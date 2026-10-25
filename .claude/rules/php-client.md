---
paths:
  - "src/**"
  - "examples/**"
  - "tests/php/**"
---
# The PHP client

- Every file starts with `declare(strict_types=1)`; four-space indentation; PHP `>=8.2 <8.6` with
  `ext-curl` and `ext-json` only. No `ext-mbstring`: `Client::codePoints()` counts code points as bytes
  minus UTF-8 continuation bytes, and new text limits go through it.
- Warnings are failures. `tests/php/run.php` promotes every warning and deprecation to an exception, so a
  call deprecated on PHP 8.5 (as `curl_close()` was) fails the suite: release handles by dropping them.
  CI runs the suite on 8.2, 8.3, 8.4 and 8.5; to reproduce one locally, set `BRIDGE_PHP_IMAGE` to that
  job's pinned image from `.github/workflows/tests.yml` and rerun the same compose commands.
- Tests are `check()` and `rejects()` calls: `tests/php/run.php` runs offline (contract checks and one
  transport error), and `tests/php/integration.php` loads it and then drives the client against the real
  `worker`, `fault-worker`, `tokens-worker` (two tokens) and `hostile` peers over HTTP. The path through FrankenPHP is
  `tests/integration.py`. A new behaviour gets a success case, invalid-input cases and, where the worker
  is involved, an integration case. Syntax is checked with `php -l` over `src`, `examples` and `tests/php`.
- The client never retries a `POST` on its own (a transport failure leaves the submission uncertain), and
  `wait()` never cancels; the MCP server cancels on its own deadline on purpose. Keep both as
  `docs/protocol.md` describes, and change the doc with the contract.
- The example routes have no end-user authentication and publish no ports; don't add either to the examples.
