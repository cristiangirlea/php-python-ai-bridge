# Outside security review

The scans in [SECURITY.md](../SECURITY.md) and the project's own tests do not replace a review by someone who did not write the code. This page is the brief for that reviewer: what to look at, what is already known, how to run it, and what to hand back.

## Scope

In order of priority:

1. **The worker's HTTP handling** (`worker/ai_bridge/server.py`): request-line and header parsing, `Content-Length` and body reading, the read budget, the connection limit, the lingering close and the busy drain, and what each refusal reveals. [The protocol](protocol.md) is the specification.
2. **Token handling**: `BRIDGE_TOKEN`, `BRIDGE_TOKENS` and `BRIDGE_TOKENS_FILE` parsing, the constant-time comparison, per-owner job isolation (another owner's job must be indistinguishable from an unknown one), idempotency keys per owner, isolation across a restart from `BRIDGE_STATE_DIR` (restored owners and keys, and a table from before owners going to `default`), the `BRIDGE_TOKENS_FILE` checks (a regular file, its mode, its size), and that no token reaches a child process, a log line, a metric or the job table.
3. **Admission, limits and input validation** (`worker/ai_bridge/jobs.py`, `worker/ai_bridge/tasks.py`): capacity, per-owner capacity, result retention, the task allowlist and input limits, the redaction patterns' cost on hostile input, and whether one client can starve the others or exhaust memory.
4. **The PHP client** (`src/`): how it treats a hostile or broken worker, and whether it can leak the token.

The MCP server, the examples and the demo backends are out of scope unless a finding in them reaches the worker or the client.

## The threat model

- The worker runs on a private network or loopback, behind the application's own authentication, and across hosts behind a TLS proxy ([security boundaries](security.md)). An attacker reaching the worker directly from an untrusted network is outside the model.
- In the model: a client holding a valid token that tries to read, cancel or learn about another token's jobs; a client or a compromised application that sends malformed, oversized or slow requests; a hostile worker answering the PHP client; anything that reads logs, metrics or the job table on disk.
- Task inputs are untrusted data; task code is trusted.

## Known limits

These are documented choices, not findings: the worker is `http.server`, not a hardened front; rate limiting belongs in the proxy; redaction is assistive ([security boundaries](security.md)); a child's freed memory is not wiped.

## Running it

Everything builds and runs inside `docker/compose.yaml` with no local PHP or Python; the README's "Try it with Docker" starts a worker, and `AGENTS.md` lists every command of the deterministic CI gate. The fuzzing and hostile-peer tests in [testing](testing.md) are a starting point. Use a disposable `BRIDGE_TOKEN`.

## What to hand back

- Each finding with its severity, the affected version or commit, the steps or input that reproduce it, and what an attacker gains. Report anything exploitable privately, as [SECURITY.md](../SECURITY.md) says, before it is written up anywhere public.
- What was reviewed and how (reading, fuzzing, live testing), and what was not.
- Who reviewed it, so the [production-grade tracking issue](https://github.com/cristiangirlea/php-python-ai-bridge/issues/112) can name the review.
