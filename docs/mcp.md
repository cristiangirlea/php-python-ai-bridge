# MCP server

`mcp_server/bridge_mcp` is a stdio [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI agent one tool per task the worker allows. It is a second, independent client of [the protocol](protocol.md), sitting beside the PHP client rather than replacing it: both submit a job, poll it and read the result. The asynchronous protocol stays the primary interface; an MCP tool call is synchronous from the agent's side, so each tool submits, polls to completion and returns the finished result.

## Why the tools look the way they do

A tool's input is the agent's output, the most expensive tokens there are, and anything a cloud-hosted agent passes to a tool has already left the machine. Two consequences shape every tool:

- **Bulk input goes by reference.** `bridge_rerank` takes `documents_path`, a file under a directory the operator configured, and returns only the best `top_k` with a short snippet each. Pasting hundreds of documents into a call would cost more than reading them; a file costs nothing to name. Documents by value are capped at 64.
- **Small results.** `bridge_embed_similarity` returns a cosine matrix and the most similar pairs, never vectors; an agent cannot use a vector, and an index belongs in a script talking to the HTTP protocol directly.
- **Honest scope for redaction.** `bridge_redact` protects whatever the agent forwards the text to next. The text is already in the conversation when the tool is called; the tool does not un-send it. Redacting before an agent ever sees the data is the PHP client's job.

## Tools

Every description states what the backend actually is, in the worker's own words, because the server reads `/healthz` at start-up and refuses to start if the worker does not answer. On the default worker the descriptions say `lexical-demo-not-a-model`, `hashing-bow-not-a-model` and `rules-only-not-a-model`; on the ONNX profile they name the pinned models. The test-only tasks are never tools, whatever `BRIDGE_TEST_TASKS` is set to: the tool list is a literal in `tools.py`, not read from the worker, and the test suite runs with test tasks enabled to prove it.

| Tool | Input | Result | Typical time |
| --- | --- | --- | --- |
| `bridge_health` | none | `backend`, `protocol`, the models by task | instant |
| `bridge_rerank` | `query`, then `documents` (≤64) or `documents_path` (≤512 in a file), `top_k` | `model`, `considered`, `results[]` of `index`, `score`, `snippet` | under a second on the demo backend; one to ten seconds with the ONNX model, which loads afresh per call |
| `bridge_embed_similarity` | `texts` (2–32) | `model`, `matrix`, `pairs[]` of `a`, `b`, `similarity` | as above |
| `bridge_redact` | `text`, `entities` (subset of PER, ORG, LOC), `min_score` | `model`, masked `text`, `spans[]` with `source` | as above |

A `documents_path` file is either a JSON array of strings or one document per non-empty line, at most 4 MiB. Relative paths resolve under the root; absolute paths must resolve inside it after following symlinks. Snippets are the first 160 characters of a document with whitespace collapsed; they are content from the agent's own files and enter its context like any other file it reads.

Progress: the worker reports progress per job, and the server forwards each change as an MCP progress notification when the host supplied a progress token, and nothing otherwise. Time limit: a call waits `BRIDGE_MCP_TIMEOUT_MS` (default 60000) and then **cancels** the job, because no later request will come back for it; the PHP client's `wait()` deliberately does not cancel, for the opposite reason. The worker's own deadline is set two seconds later as a backstop.

Errors come back as tool errors the agent can act on (capacity reached, out of contract, worker unreachable, token rejected, cancelled). The worker's fixed error code is the only thing reflected from a failure; free text from the worker never is, and the token never appears anywhere.

## Configuration

| Variable | Meaning |
| --- | --- |
| `BRIDGE_TOKEN` | The worker's service token, required. Read from the environment only; never returned. |
| `BRIDGE_URL` | The worker origin, default `http://127.0.0.1:8090`. |
| `BRIDGE_MCP_ROOT` | The one directory whose files `documents_path` may name. Unset means paths are refused. |
| `BRIDGE_MCP_TIMEOUT_MS` | Per-call wait before cancelling, 100–300000, default 60000. |
| `BRIDGE_MCP_STARTUP_S` | How long to wait for the worker to answer at start-up before exiting, default 10. Only a refused connection is waited out; a rejected token fails at once. |

The root is server configuration rather than MCP roots on purpose: the 2026-07-28 specification deprecates roots and sampling in favour of explicit parameters, and a host that does not declare the capability fails the call. An operator-set directory gives the same confinement without depending on the host.

## Running it

The worker publishes no host ports, so the server runs inside the same Docker network and the host's agent talks to it over stdio:

```sh
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher   # once: hash-pinned wheels into .cache/
docker compose -f docker/compose.yaml run --rm -i -T mcp
```

Compose starts the `worker` alongside it, and the server waits up to `BRIDGE_MCP_STARTUP_S` (default 10) for it to answer before giving up. `/data` inside the container is the checkout by default; set `BRIDGE_MCP_DATA` to the **absolute** path of another directory to mount it there read-only (a relative value resolves against `docker/`, not your shell). A host that launches MCP servers from a JSON configuration would use, with the token supplied from its environment:

```json
{"mcpServers": {"bridge": {
  "command": "docker",
  "args": ["compose", "-f", "/path/to/php-python-ai-bridge/docker/compose.yaml", "run", "--rm", "-i", "-T", "mcp"],
  "env": {"BRIDGE_TOKEN": "...", "BRIDGE_MCP_DATA": "/path/to/candidates"}
}}}
```

The server writes only JSON-RPC to stdout; everything else goes to stderr.

## Trust model

Nothing about the worker's trust model changes: a private network, one bearer token, never internet-facing. Two things are new and must be understood:

- **Over stdio there is no authentication.** The host process launches the server and inherits its trust; the server holds `BRIDGE_TOKEN` from its environment. Running this server over HTTP or SSE would be a new, unsolved authorisation surface and is out of scope; the bearer token does not cover it.
- **An agent can now submit jobs, and an agent can be prompt-injected.** It cannot read the token, but it can fill the worker's capacity or submit junk. Point the MCP server at a **dedicated worker**, never the one serving a production PHP application. Capacity, concurrency and retention are per worker.

Paths are confined to `BRIDGE_MCP_ROOT`; the server never writes files. Results contain indexes, scores, snippets of the agent's own files, masked text and spans; the original documents are not echoed by the worker.

## Testing

`tests/mcp/` drives the tools through the SDK's in-memory client against a real in-process worker with test tasks enabled, and the protocol client against the same worker: the exact tool list, backend names in every description, by-value and by-reference reranking, root confinement, the similarity matrix, redaction, forwarded progress, cancellation on a local deadline, and errors that carry the worker's code but never the token. CI acquires the hash-pinned wheels once and runs the suite offline:

```sh
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps mcp-tests
```

Dependencies are pinned in `requirements-mcp.lock` with SHA-256 hashes for CPython 3.12 on Linux x86_64, like the model wheels. The SDK hard-depends on its HTTP transport stack (uvicorn, starlette, sse-starlette, cryptography, pyjwt), so those are installed and pinned too although nothing exercises them over stdio; they are part of the supply-chain surface all the same. The MCP SDK is the one third-party dependency of this repository's own code, and it stays out of `worker/`, which remains standard library only.
