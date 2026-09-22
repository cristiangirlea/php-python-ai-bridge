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

`rerank`, `embed` and `redact` are enabled normally. For `rerank`, query and each document must contain 1–8192 characters with non-whitespace content; 1–512 documents are accepted, and the query plus all documents may total at most 200000 characters. The 262144 byte limit still applies to the encoded request, so multi-byte text and escaped quotes can reach it before the character budget; the PHP client counts the budget in bytes and sends raw UTF-8 to keep the two close. An optional integer `top_k` from 1 to the document count limits how many rankings are returned; omitting it returns every document. The deadline is an integer from 100 to 300000 milliseconds and includes queue time, process startup and execution. Missing `timeout_ms` means 30000.

A job contains `id`, `task`, `status`, `timeout_ms`, `result`, `error`, and `progress`. IDs are 32 lowercase hex characters. Result/error/progress are null until applicable. Input documents are not echoed in status responses.

States: `queued` → `running` → `succeeded` or `failed`. A queued/running job may also become `timed_out` or `cancelled`. Terminal states do not change. Cancelling an already completed job returns its unchanged state. Progress contains integer `completed` and `total`; it may be null before execution.

Successful reranking returns `{"model":"...","rankings":[{"index":0,"score":1.0}]}`. Rankings are sorted by descending score, with original order breaking ties; each original document index occurs at most once, and exactly `top_k` rankings are returned (every document when `top_k` is omitted). Progress is reported at most 66 times per job regardless of document count and always ends at the total. The deterministic backend identifies itself as `lexical-demo-not-a-model`. ONNX scores are not calibrated probabilities.

Submit `{"task":"embed","input":{"texts":["..."]}}` with 1–32 texts of 1–8192 characters each, totalling at most 200000 characters. A successful result is `{"model":"...","dimensions":384,"vectors":[[...]]}` with one unit-length vector per text in input order; components are rounded to six decimals, so the dot product of two vectors is their cosine similarity. The service returns vectors and stores nothing; the caller owns any index built from them. The deterministic backend identifies itself as `hashing-bow-not-a-model`: a signed feature hash of word tokens that is stable across processes and ignores word order. The ONNX backend is `sentence-transformers/all-MiniLM-L6-v2` with mean pooling; it truncates input beyond 256 word pieces and is trained on English text.

Submit `{"task":"redact","input":{"text":"...","entities":["PER"],"min_score":0.85}}` with 1–8192 characters of valid Unicode text. `entities` is an optional list of distinct labels among `PER`, `ORG` and `LOC` (default `["PER"]`; `MISC` is never offered) and `min_score` an optional number from 0 to 1 (default 0.85); both shape only the model pass. A successful result is `{"model":"...","text":"...","spans":[{"start":0,"end":5,"label":"EMAIL","source":"rule:email","score":1.0}]}`: the text with each span replaced by `[LABEL]`, and the spans sorted and disjoint, with offsets counted in Unicode code points of the original text. Deterministic rules run on every backend and are tagged `rule:`: `CARD` (any 13–19 digit window of a digit run that passes Luhn, longest first), `IBAN` (mod 97, trimming trailing groups the pattern swallowed), `EMAIL` and `IPV4` at score 1.0, and `PHONE` at 0.8 because it is a pattern, not a proof: 7–15 digits with a leading plus or separators, or at least ten bare digits, never a date shape. Known pattern false positives remain: a dotted quadruple of small numbers such as a version string matches `IPV4`, and separated digit groups such as amounts can match `PHONE`; filter by `source` and `score` where that matters. On the default backend, or when `entities` is empty, the model name is `rules-only-not-a-model` and no model runs. The ONNX backend adds spans tagged `model:PER`, `model:ORG` or `model:LOC` carrying the model's mean word confidence, from `Xenova/bert-base-NER:int8`, the int8 ONNX conversion of `dslim/bert-base-NER` (CoNLL-2003, English news). Overlapping candidates are merged into one span carrying the leftmost, then longest, then strongest label, so no part of any candidate stays visible. Redaction is assistive: rules cannot see unstructured personal data, and the model misses names unlike its news training data, so it is not a compliance control.

Failures use generic error objects with `code` and `message`. Task errors include `task_failed`, `worker_crashed`, `worker_start_failed` and `deadline_exceeded`. Internal exception text is not sent to clients. HTTP contract failures return 400, missing auth 401, missing routes/jobs 404, oversized bodies 413 and capacity exhaustion 429.

The default capacity is 128 total queued, running and retained jobs, concurrency is 2, and terminal retention is 300 seconds. A full result cache rejects submissions until entries expire; it does not silently evict a result early. Configure these with `BRIDGE_CAPACITY`, `BRIDGE_CONCURRENCY`, and `BRIDGE_RETENTION_SECONDS`.

The PHP client uses a fresh cURL handle per call, refuses redirects, ignores proxy environment variables, caps response bytes, and verifies that GET/cancel responses match the requested ID. Configure its request timeout separately from job execution and local waiting deadlines. There are no transparent submission retries.

Adding a task requires trusted source changes to `worker/ai_bridge/tasks.py`: an explicit name, input validation, bounded output and execution logic. Add a PHP result type where useful, plus successful, malformed-input and lifecycle tests. Task imports, file paths and command strings must not come from request data.
