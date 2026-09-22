# Protocol v1

Every route requires `Authorization: Bearer <service token>`. JSON requests require a single `Content-Length`, `Content-Type: application/json`, a maximum of 262144 bytes, and unique object keys. Chunked request bodies are unsupported. All routes close the HTTP connection after a response.

| Route | Result |
| --- | --- |
| `GET /healthz` | 200 with protocol version and configured backend; process liveness only, not model readiness |
| `POST /v1/jobs` | 202 with a queued job |
| `GET /v1/jobs/{id}` | 200 with a snapshot, or 404 |
| `POST /v1/jobs/{id}/cancel` | Send `{}`; returns the terminal/current snapshot, or 404 |

Submit:

```json
{"task":"rerank","input":{"query":"capital of France","documents":["Paris is the capital of France.","Saturn has rings."]},"timeout_ms":30000}
```

Only `rerank` is enabled normally. Query and each document must contain 1–8192 characters with non-whitespace content; 1–512 documents are accepted, and the query plus all documents may total at most 200000 characters. The 262144 byte request limit binds first for multi-byte text, and the PHP client counts that budget in bytes rather than characters. An optional integer `top_k` from 1 to the document count limits how many rankings are returned; omitting it returns every document. The deadline is an integer from 100 to 300000 milliseconds and includes queue time, process startup and execution. Missing `timeout_ms` means 30000.

A job contains `id`, `task`, `status`, `timeout_ms`, `result`, `error`, and `progress`. IDs are 32 lowercase hex characters. Result/error/progress are null until applicable. Input documents are not echoed in status responses.

States: `queued` → `running` → `succeeded` or `failed`. A queued/running job may also become `timed_out` or `cancelled`. Terminal states do not change. Cancelling an already completed job returns its unchanged state. Progress contains integer `completed` and `total`; it may be null before execution.

Successful reranking returns `{"model":"...","rankings":[{"index":0,"score":1.0}]}`. Rankings are sorted by descending score, with original order breaking ties; each original document index occurs at most once, and exactly `top_k` rankings are returned (every document when `top_k` is omitted). Progress reports at most about 64 intermediate steps regardless of document count and always ends at the total. The deterministic backend identifies itself as `lexical-demo-not-a-model`. ONNX scores are not calibrated probabilities.

Failures use generic error objects with `code` and `message`. Task errors include `task_failed`, `worker_crashed`, `worker_start_failed` and `deadline_exceeded`. Internal exception text is not sent to clients. HTTP contract failures return 400, missing auth 401, missing routes/jobs 404, oversized bodies 413 and capacity exhaustion 429.

The default capacity is 128 total queued, running and retained jobs, concurrency is 2, and terminal retention is 300 seconds. A full result cache rejects submissions until entries expire; it does not silently evict a result early. Configure these with `BRIDGE_CAPACITY`, `BRIDGE_CONCURRENCY`, and `BRIDGE_RETENTION_SECONDS`.

The PHP client uses a fresh cURL handle per call, refuses redirects, ignores proxy environment variables, caps response bytes, and verifies that GET/cancel responses match the requested ID. Configure its request timeout separately from job execution and local waiting deadlines. There are no transparent submission retries.

Adding a task requires trusted source changes to `worker/ai_bridge/tasks.py`: an explicit name, input validation, bounded output and execution logic. Add a PHP result type where useful, plus successful, malformed-input and lifecycle tests. Task imports, file paths and command strings must not come from request data.
