# MCP server

`mcp_server/bridge_mcp` is a stdio [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI agent one tool per task the worker allows. It is a second, independent client of [the protocol](protocol.md), sitting beside the PHP client rather than replacing it: both submit a job, poll it and read the result. The asynchronous protocol stays the primary interface; an MCP tool call is synchronous from the agent's side, so each tool submits, polls to completion and returns the finished result.

## Why the tools look the way they do

A tool's input is the agent's output, the most expensive tokens there are, and anything a cloud-hosted agent passes to a tool has already left the machine. Two consequences shape every tool:

- **Bulk input goes by reference.** `bridge_rerank` takes `documents_path`, a file under a directory the operator configured, and returns only the best `top_k` with a short snippet each. Pasting hundreds of documents into a call would cost more than reading them; a file costs nothing to name. Documents by value are capped at 64.
- **Small results.** `bridge_embed_similarity` returns a cosine matrix and the most similar pairs, never vectors; an agent cannot use a vector, and an index belongs in a script talking to the HTTP protocol directly. That script is `python -m bridge_mcp.index build`, run by the operator; `bridge_search` reads what it wrote and returns offsets and snippets, never vectors.
- **Honest scope for redaction.** `bridge_redact` protects whatever the agent forwards the text to next. The text is already in the conversation when the tool is called; the tool does not un-send it. Redacting before an agent ever sees the data is the PHP client's job.

## Tools

Every description states what the backend actually is, in the worker's own words, because the server reads `/healthz` at start-up and refuses to start if the worker does not answer. On the default worker the descriptions say `lexical-demo-not-a-model`, `hashing-bow-not-a-model` and `rules-only-not-a-model`; on the ONNX profile they name the pinned models. The test-only tasks are never tools, whatever `BRIDGE_TEST_TASKS` is set to: the tool list is a literal in `tools.py`, not read from the worker, and the test suite runs with test tasks enabled to prove it.

| Tool | Input | Result | Worker time, median of five cold jobs on one CPU, on two runners |
| --- | --- | --- | --- |
| `bridge_health` | none | `backend`, `protocol`, the models by task | instant |
| `bridge_rerank` | `query`, then `documents` (≤64) or `documents_path` (≤512 in a file), `top_k` | `model`, `considered`, `results[]` of `index`, `score`, `snippet` | 0.4 s on the demo backend; with the ONNX model 0.7-0.9 s for 32 documents and 1.0-1.2 s for 512 |
| `bridge_embed_similarity` | `texts` (2–32) | `model`, `matrix`, `pairs[]` of `a`, `b`, `similarity` | 0.4 s on the demo backend; 1.1-1.3 s for 32 texts with the ONNX model |
| `bridge_redact` | `text`, `entities` (subset of PER, ORG, LOC), `min_score` | `model`, masked `text`, `spans[]` with `source` | 0.4 s on the demo backend; 1.6-2.4 s for 2000 characters with the ONNX model |
| `bridge_search` | `query`, `index_path` (an index directory under the root), `top_k` (1-50), `rerank` (default true) | `embed_model`, `rerank_model`, `chunks`, `considered`, `results[]` of `source`, `chunk`, `start`, `end`, `similarity`, `rerank_score`, `snippet` | one embed job, plus one rerank job unless `rerank` is false |

The times are the worker's, from [the measured table](testing.md#measured-latency-informational), and the ranges are the difference between two runners' CPUs; the model loads afresh for every call, and the MCP server adds its polling interval and HTTP overhead on top. A `documents_path` file is either a JSON array of strings or one document per non-empty line. A file over 4 MiB is refused before it is parsed, but the limits that bind are the worker's: at most 512 documents, and the query plus all documents within 200000 characters (the encoded request within 262144 bytes), so a usable file is roughly 200 KB of text. Relative paths resolve under the root; absolute paths must resolve inside it after following symlinks. Snippets are the first 160 characters of a document with whitespace collapsed; they are content from the agent's own files and enter its context like any other file it reads.

Progress: the worker reports progress per job, and the server forwards each change as an MCP progress notification when the host supplied a progress token, and nothing otherwise. Time limit: a call waits `BRIDGE_MCP_TIMEOUT_MS` (default 60000) and then **cancels** the job, because no later request will come back for it; the PHP client's `wait()` deliberately does not cancel, for the opposite reason. The worker's own deadline is set two seconds later as a backstop.

Errors come back as tool errors the agent can act on (capacity reached, out of contract, worker unreachable, token rejected, cancelled). The worker's fixed error code is the only thing reflected from a failure; free text from the worker never is, and the token never appears anywhere.

## Building and searching an index

`bridge_search` answers questions about a corpus larger than the agent should read, but it does not build anything: the operator builds an index first, over the same HTTP protocol the PHP client uses, and the agent only names it.

```sh
docker compose -f docker/compose.yaml run --rm mcp-index /data/docs --out /data/artifacts/docs
```

`mcp-index` runs `python -m bridge_mcp.index build` against `mcp-worker`. It is the only service that can write to `/data`, and it needs no wheels: the builder is standard library only. The agent then calls `bridge_search` with `index_path` set to `artifacts/docs` (`artifacts/` is git-ignored when `/data` is the checkout). On Linux, the output directory must be writable by container UID 65532.

- **Inputs.** Files named on the command line, and every `.txt` or `.md` file under each directory named (`--suffix` changes the list), read as UTF-8 with their own line endings, so offsets are code points of the file as stored. A file over 16 MiB, a non-UTF-8 file or an index over 50000 chunks is refused, not skipped, and so is an `index.json` over 64 MiB when it is loaded: parsing costs several times its size, and the `mcp` container has 512 MiB. Source paths are stored relative to the index directory, so rebuild an index rather than moving it.
- **Chunking.** Character windows of `--chunk-chars` (default 1000, 200-4000), cut at the last whitespace in the second half of the window, each starting on a word and overlapping the previous one by about `--overlap-chars` (default 200). An unbroken token longer than a window is cut hard, still with overlap. There is no tokenizer on the client: dense text such as code, URLs or non-Latin scripts can exceed the embedding model's 256 word pieces in 1000 characters, and the model then silently embeds only the start of the chunk; use `--chunk-chars 500` for such corpora.
- **Format.** `index.json` holds the format id `bridge-index/1`, the embedding model and backend, dimensions, the chunking, the source paths relative to the index directory, and every chunk's source, offsets and text. `vectors.f32` holds count x dimensions little-endian float32 values, row-major, and `index.json` records its SHA-256. Both are written to temporary files and nothing is replaced unless both were written, but the two renames are separate: a search that loads between them sees a checksum mismatch and retries once after 0.2 seconds. A loader refuses any other format, a count, size or checksum that does not match, a chunk out of range, a vector that is not unit length, and a vectors file that is not a bare name beside `index.json` or resolves outside the root.
- **Building.** Chunks are embedded 32 per `embed` job with the worker's own model, each job waiting up to `--timeout-s` (default 60, 1-300); a worker at capacity is waited out, and a worker whose model changes mid-build refuses the build and leaves nothing behind. A build of 10000 chunks is about 313 jobs, and a worker retains finished jobs against its capacity for 300 seconds, so a dedicated worker with a higher `BRIDGE_CAPACITY` or a lower `BRIDGE_RETENTION_SECONDS` builds faster.
- **Searching.** An index built with a different embedding model than the worker now uses is refused before any job is spent: vectors from different models are not comparable. Otherwise the query is embedded with one job and every chunk is scored by dot product (the vectors are unit length, so this is cosine similarity), by brute force in pure Python: there is no approximate index and no incremental rebuild. Unless `rerank` is false, the best max(4 x `top_k`, 20) chunks are sent to the worker's `rerank` task, trimmed from the bottom to fit its 200000-character budget and the 262144-byte request limit, which multi-byte text reaches first, and the results are ordered by that score; the agent pays for none of their text. Each result names its source relative to the root, the chunk's offsets, and a 160-character snippet around the longest query word of three or more letters that the chunk contains as a whole word, or its start. Read the source between the offsets for the whole chunk.
- **Caching.** The server keeps loaded indexes up to 128 MiB of index files, always at least the latest, keyed by path and reloaded when either file is replaced or its modification time or size changes. Every rebuild replaces both files, so every rebuild is noticed; only an in-place rewrite by another tool that keeps the inode, time and size is not. Loading runs outside the cache's lock, so a search of a cached index never waits for another index to load.

## Evaluating with an LLM

The tests prove the tools keep their contract; whether an agent uses them well is a separate question, which `tests/mcp/evaluation.xml` asks. It holds ten questions with single, string-comparable answers over an invented corpus in `tests/mcp/fixtures/eval`, so no model can answer from memory: facts found by searching an index, a rerank by file reference, redaction labels and counts, a similarity ranking, a two-step search, and whether the redaction tool runs a model at all. The notes split into 27 chunks, so a default search considers 20 and returns 5 of them, and they carry distractors: a second delivery of fittings, the lantern's old panes, another keeper's repair, other years, several firms' yards. The answers assume the demo worker the compose `mcp` service uses, whose search matches words rather than meaning, so the questions stay close to the corpus's wording; an evaluation of semantic retrieval would need an ONNX-backed worker behind the MCP server, which compose does not provide yet.

CI does not run an LLM. `tests/mcp/test_evaluation.py` proves every answer is reachable through the tools by making the calls a capable agent would make, and that no question contains its own answer. It also checks that a default search sees only part of the corpus, and that none of the first steps agents actually sent on the two-step question already shows its answer.

To run it with an LLM, build the fixture index once, from the repository root with an absolute path to the fixtures, then run the questions with the Claude Code CLI:

```sh
export BRIDGE_MCP_DATA="$PWD/tests/mcp/fixtures/eval"
docker compose -f docker/compose.yaml run --rm mcp-index /data/notes --out /data/index --chunk-chars 300 --overlap-chars 60
python scripts/mcp_evaluate.py --model sonnet --model haiku --out results.json
```

`scripts/mcp_evaluate.py` runs each question in its own headless `claude -p` session with the CLI's own login, so it needs no API key and spends that account's usage. The agent gets this server through `scripts/mcp_stdio.sh` and nothing else: built-in tools are off, other MCP servers and your own settings are not loaded, and a session whose agent could see another tool does not count. `BRIDGE_TOKEN` must be set; it reaches the server through the environment and is never written to the configuration file. Answers are scored by exact match on the last `<response>` tag. The report goes to stdout as Markdown, with every session's tool calls and the agent's feedback on the tools in `--out`; the exit status is 0 only when every answer is right. It needs `claude`, `sh` and `docker` on `PATH`; on Windows, `sh` is Git's.

The harness in Anthropic's MCP builder skill runs the same file through the API instead. It needs `anthropic`, `mcp` and an API key, and takes the same index:

```sh
python /path/to/mcp-builder/scripts/evaluation.py tests/mcp/evaluation.xml -t stdio -m <current model> \
  -c sh -a scripts/mcp_stdio.sh -e BRIDGE_TOKEN="$BRIDGE_TOKEN" BRIDGE_MCP_DATA="$BRIDGE_MCP_DATA"
```

`scripts/mcp_stdio.sh` wraps the compose command, because the harness would read compose's own flags as its options; the evaluation file comes first because `-e` takes every argument after it. Pass `-m`: the harness's default model is old.

On Linux the fixture directory must be writable by container UID 65532 for the build step, or the builder refuses with "cannot write". Under Git Bash on Windows, give `BRIDGE_MCP_DATA` as `E:/...` rather than `$PWD`'s `/e/...`, and set `MSYS_NO_PATHCONV=1` for the build step so the container paths `/data/...` are not rewritten into Windows paths; the runner sets it for the server itself. The fixture index is git-ignored. The latest recorded run is in [the testing notes](testing.md#llm-evaluation-informational).

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

Compose starts a dedicated `mcp-worker` alongside it, never the demo `worker` or one serving an application, and the server waits up to `BRIDGE_MCP_STARTUP_S` (default 10) for it to answer before giving up. The service installs its hash-pinned wheels into a tmpfs on every start, so the first response takes a few seconds longer than later ones. `run --rm` removes only the `mcp` container when the host ends the session: `mcp-worker` keeps running, with up to 300 seconds of retained results in memory, and later sessions reuse it. Stop it with `docker compose -f docker/compose.yaml --profile mcp down`. `/data` inside the container is the checkout by default; set `BRIDGE_MCP_DATA` to the **absolute** path of another directory to mount it there read-only (a relative value resolves against `docker/`, not your shell). A host that launches MCP servers from a JSON configuration would use, with the token supplied from its environment:

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
- **An agent can now submit jobs, and an agent can be prompt-injected.** It cannot read the token, but it can fill the worker's capacity or submit junk. Point the MCP server at a **dedicated worker**, never the one serving a production PHP application; the compose `mcp` service does this with `mcp-worker`. Capacity, concurrency and retention are per worker.

Paths are confined to `BRIDGE_MCP_ROOT`, including an index's vectors file after symlinks are resolved; the server never writes files, and only the operator-run `mcp-index` builder writes, into the directory it is told to. Results contain indexes, scores, snippets of the agent's own files, masked text and spans; the original documents are not echoed by the worker.

## Testing

`tests/mcp/` drives the tools through the SDK's in-memory client against a real in-process worker with test tasks enabled, and the protocol client against the same worker: the exact tool list, backend names in every description, by-value and by-reference reranking, root confinement, the similarity matrix, redaction, forwarded progress, cancellation on a local deadline, and errors that carry the worker's code but never the token. The index builder and `bridge_search` are tested the same way: chunking, the format and every corruption it refuses, building against the worker, the model gate, confinement of index and vectors paths, cache reloads, and the builder's command line. CI acquires the hash-pinned wheels once and runs the suite offline:

```sh
docker compose -f docker/compose.yaml run --rm --no-deps mcp-fetcher
docker compose -f docker/compose.yaml run --rm --no-deps mcp-tests
```

Dependencies are pinned in `requirements-mcp.lock` with SHA-256 hashes for CPython 3.12 on Linux x86_64, like the model wheels. The SDK hard-depends on its HTTP transport stack (uvicorn, starlette, sse-starlette, cryptography, pyjwt), so those are installed and pinned too although nothing exercises them over stdio; they are part of the supply-chain surface all the same. The MCP SDK is the one third-party dependency of this repository's own code, and it stays out of `worker/`, which remains standard library only.
