# PHP support and verification

## One package, supported PHP branches

As of 2026-09-17, PHP's upstream-supported branches are **8.2, 8.3, 8.4 and 8.5**. Security-only maintenance counts as supported. See [PHP's official schedule](https://www.php.net/supported-versions.php).

| PHP | Upstream maintenance on that date | Security support ends |
| --- | --- | --- |
| 8.2 | Security fixes only | 2026-12-31 |
| 8.3 | Security fixes only | 2027-12-31 |
| 8.4 | Active | 2028-12-31 |
| 8.5 | Active; latest stable minor | 2029-12-31 |

All four use the same source and Composer package. The current requirement is `>=8.2 <8.6`, with `ext-curl` and `ext-json`. It deliberately excludes future unverified minor versions as well as EOL PHP branches. The default Docker environment uses PHP 8.5. The Python service and HTTP protocol do not need separate versions for each PHP minor.

This is a maintained release policy, not an automatic date-dependent Composer constraint. On a new stable PHP release, review its migration guide, add a pinned runtime to CI, fix incompatibilities, then widen the constraint in a release. When a branch reaches EOL, remove it from the supported matrix and update the minimum in a documented release. Existing published versions remain immutable. Refresh pinned images for security/patch updates; a test against one patch is not a test against every historical patch of that minor.

Framework support is separate: a Symfony/Laravel application must satisfy both its framework's PHP requirements and this package's requirements. Manual framework configuration examples are documented, but dedicated adapters and framework-version test matrices are not implemented.

## Reproduce the checks

Use the commands in [README](../README.md#test-layers). All execution is inside bounded Linux amd64 containers, with no external test network. HTTP peers share only a private Docker network. Model dependency acquisition is a separate network-enabled phase.

To select another PHP version, set `BRIDGE_PHP_IMAGE` to its exact pinned image from [the CI matrix](../.github/workflows/tests.yml), then run the same Compose commands. For example:

```sh
export BRIDGE_PHP_IMAGE='dunglas/frankenphp:1.12.7-php8.2-bookworm@sha256:15646d56650cc5a6c68275ac934eab8f8648a5e776690035f9278a93e0577088'
docker compose -f docker/compose.yaml --profile test config
docker compose -f docker/compose.yaml run --rm --no-deps php-tests
docker compose -f docker/compose.yaml up -d worker frankenphp fault-worker hostile
docker compose -f docker/compose.yaml run --rm --no-deps php-integration
docker compose -f docker/compose.yaml run --rm --no-deps integration
```

PowerShell uses `$env:BRIDGE_PHP_IMAGE = '...'` instead of `export`. Set a disposable `BRIDGE_TOKEN` as described in the README first. Changing the image and rerunning `up` recreates this project's PHP service; do this only in a disposable test environment, not a live application.

CI checks the actual PHP minor before testing so a misconfigured image cannot silently test the default version four times. The stable required check `deterministic` depends on all four matrix jobs and on the offline MCP server job, and rejects any of them failed, cancelled or skipped. PHP contract/HTTP tests promote warnings and deprecations to exceptions.

## What the tests establish

PHP branch support was verified locally on 2026-09-17 on PHP 8.2.33, 8.3.33, 8.4.25 and 8.5.10 with the pinned FrankenPHP 1.12.7 images. For every push and pull request, CI runs the Python worker, PHP contract/HTTP, FrankenPHP and syntax layers below on each of the four branches, and the MCP server layer once in its own job, which does not depend on PHP. The optional ONNX layer runs only on PHP 8.5, weekly, on pull requests that touch the model path, and on demand. The counts below were observed locally on 2026-09-27 on the default PHP 8.5.10 image; a local pass does not imply a hosted CI run, so consult the PR's Actions checks for remote results.

| Layer | Scope and counting |
| --- | --- |
| Python worker | 120 tests: lifecycle, validation and HTTP, process crashes, cancellation, timeouts, retention, allocation/exit races, redaction rules including IPv6, lower-case IBANs and email addresses in any script and their speed on hostile input, NER aggregation, name joining, the second title-cased reading, the uncased model's reading of the whole text and the merge driven by stubs, the fetcher's pins and the model directories an ONNX worker checks for before it starts, batched cross-encoder scoring, the NER measurement's sample and scoring, progress bounds, and the model-name table every result reports from |
| MCP server | 119 tests, offline with hash-pinned wheels: the protocol client against an in-process worker, every tool through the SDK's in-memory client with test tasks enabled, the entry point as a subprocess, result validators on synthetic data, the index builder (chunking, the format and each corruption it refuses, building against the worker, caching, its command line) `bridge_search` (the model gate, path confinement, reloads, rerank on and off), the reachability of every answer in the LLM evaluation, that a default search sees only part of its corpus and that its two-step question takes two steps, that the demo worker reaches none of the semantic evaluation's answers, that a tool waits out a full worker, before each of a search's jobs and within one budget, and says so when it stays full, the evaluation runner's command, isolation checks, scoring and per-backend launcher and index check without starting `claude`, a check that the server's model names match the worker's, that every path parameter says where it resolves and every position in a result says what it counts, and a check that every timing claim in a tool description is a measured median |
| PHP contracts + HTTP | 134 checks per PHP version; this **includes** the 101 contract checks, not 134 + 101 |
| FrankenPHP | 12 jobs submitted by six concurrent test threads through a reused PHP worker, a 512-document `top_k` request, embedding and redaction round trips, and invalid-input, missing-job and multibyte boundary checks |
| PHP syntax | All 12 PHP source/example/test files |
| Optional ONNX | Through both the PHP CLI and FrankenPHP HTTP: 2 rerank cases, a 70-document rerank across three batches with its relevant document in the second, a parity check that three documents score within 1e-3 alone and inside that set, 1 embedding and 3 redaction cases: a name, an email address and a place; a sentence whose lower-case person, place and organisation only the uncased model finds, which also checks its label order; and clean text, which must produce no spans; and the ONNX-backed MCP server end to end, every tool through compose's `mcp-model` service over stdio, including a search over an index the embedding model built, with a query worded unlike the note it must find, and the calls for every question in the semantic evaluation, whose answers the models must all reach; smoke checks, not quality datasets |

Test durations reported by the runners are suite durations, not inference latency or a reproducible performance benchmark.

The optional real-model smoke tests run on the default PHP 8.5 image with cached dependencies and no internet access during inference. They were not rerun on every PHP minor. On 2026-09-17, three PHP documentation snippets passed syntax validation and the fenced JSON examples parsed successfully; that check has not been repeated since, and it is not a framework boot test.

The default backend is deterministic word overlap (`lexical-demo-not-a-model`). Passing its tests proves plumbing/lifecycle behavior, not AI quality. The embedding and redaction demos are `hashing-bow-not-a-model` and `rules-only-not-a-model`: the first proves the contract only, while the second runs the real checksum and pattern rules with no model. The optional ONNX checks use the four pinned, hash-verified models (the TinyBERT cross-encoder, MiniLM embeddings and the int8 conversions of the cased and uncased BERT NER models) with local inference. The model workflow runs weekly, on pull requests that touch the model path, and on demand; it is not a required check.

The original prototype was not entirely developed with TDD. Review corrections used observed red-green regression tests. PHP 8.5 testing similarly reproduced a deprecated `curl_close()` call under the strict error handler before replacing it with object release. Passing tests alone do not prove a test-first history or complete correctness.

## What is not established

- No measured line/branch coverage percentage, formal security audit or production certification.
- No full Symfony kernel or Laravel application/Octane integration test, including configuration caching and multi-user authorization.
- No fresh consumer Composer-install smoke test or Packagist release yet; repository examples use a small local autoloader.
- No sustained load/soak benchmark, throughput claim, p95/p99 latency, warm-model latency, measured memory-per-job limit or CPU cost per inference; the only timing figures are the cold-job medians below.
- No representative ranking-quality evaluation (for example NDCG/MRR), multilingual accuracy evaluation, NER quality on real data or GPU benchmark; the NER figures below come from 48 invented sentences by the same author as the development set used to tune the redact task.
- No repeated or semantic LLM evaluation. The one recorded run below is a single session per question per model on the demo worker, so it shows that the tools can be used, not a success rate or retrieval quality.
- No verified native Windows/macOS service execution or ARM runtime; Docker tests target Linux amd64/Python 3.12.
- No durable delivery, restart recovery, automatic retry, idempotency or multi-instance result routing. Jobs and results live in memory.

Configured resources are **limits, not measurements**: ordinary services, including the MCP server's own `mcp-worker`, get one CPU and 512 MiB each; the model worker gets one CPU and 1536 MiB. Normal task concurrency defaults to two, total capacity to 128 and terminal retention to 300 seconds; the MCP workers keep finished jobs for 30 seconds, because their one client reads each result as its job ends. The model profile sets concurrency to one, loads a model per job, and scores rerank documents 32 pairs per forward pass. None of these imply 128 simultaneous inference jobs or a specific requests-per-second rate.

## Measured latency (informational)

Measured 2026-09-27 UTC by the model smoke workflow on pull request #8 at branch head `c3c2706` (GitHub's pull-request merge ref `53e1f6f`), runner GitHub-hosted ubuntu-latest, CPU AMD EPYC 9V74 80-Core Processor. Limits: model-worker cpus=1, mem_limit=1536m, BRIDGE_CONCURRENCY=1; worker cpus=1, mem_limit=512m. N=5 sequential jobs per row; wall time from `POST /v1/jobs` to the first `GET` showing a terminal state, polled every 0.05 s, so figures carry up to 0.05 s of polling.

| Backend | Case | N | min s | p50 s | max s | Note |
| --- | --- | --- | --- | --- | --- | --- |
| onnx | rerank 32 documents of ~40 words | 5 | 0.70 | 0.74 | 0.75 | cold; 1 batch of 32 |
| onnx | rerank 512 documents of ~40 words | 5 | 1.00 | 1.00 | 1.01 | cold; 16 batches of 32 |
| onnx | rerank 32 documents of 6000 characters | 5 | 1.05 | 1.10 | 1.10 | cold; 1 full batch of 32 at the 512-token limit, the worst case |
| onnx | embed 32 texts of ~40 words | 5 | 1.10 | 1.10 | 1.11 | cold; 1 forward pass |
| onnx | redact 2000 characters, PER+ORG+LOC | 5 | 1.59 | 1.60 | 1.61 | cold; rules plus windows of 512 |
| lexical | rerank 32 documents of ~40 words | 5 | 0.36 | 0.36 | 0.48 | cold process, no model |
| lexical | rerank 512 documents of ~40 words | 5 | 0.36 | 0.37 | 0.37 | cold process, no model |
| lexical | rerank 32 documents of 6000 characters | 5 | 0.37 | 0.37 | 0.37 | cold process, no model |
| lexical | embed 32 texts of ~40 words | 5 | 0.37 | 0.37 | 0.37 | cold process, no model |
| lexical | redact 2000 characters, PER+ORG+LOC | 5 | 0.36 | 0.36 | 0.36 | cold process, no model |

The CPU a runner gets matters more than run-to-run noise. An earlier run on the same pull request, at `41393c0` on an AMD EPYC 7763, measured ONNX medians 15-50% higher: 0.85 s for 32 documents, 1.20 s for 512, 1.30 s for embedding and 2.40 s for redaction, and 0.41-0.43 s on the lexical rows. Its memory row used 24 documents of 8000 characters, not the worst case above. The ranges quoted in the MCP tool descriptions span both runs, except redaction's, which the next paragraph replaces.

Every job is cold by design: a fresh process imports its dependencies and, on the ONNX backend, loads the model before any work. The lexical rows are therefore the cost of that process alone, about 0.4 s, and most of each ONNX row is start-up and model loading rather than scoring: 512 documents take 0.26 s more than 32, and a full batch at the 512-token limit fits the model worker's memory limit. There is no measurement from before batched reranking, because the benchmark was added with it, so these figures do not state a speed-up. They are worker time only; the PHP client, FrankenPHP and the MCP server add their own polling and HTTP overhead. The benchmark asserts that every job succeeds with progress ending on the total, and a failure fails the smoke run; it asserts nothing about speed.

Since pull request #19 every redact job also loads and runs the uncased NER model. That pull request's model workflow (run 36902794112 at branch head `e49e388`, on an AMD EPYC 7763) measured a median of 3.40 s for the redact row, against 2.40 s in the earlier EPYC 7763 run above, while its rerank and embed rows came out 5-8% slower than that run's (0.90, 1.30 and 1.40 s): the second model costs about a second per cold job. A local run on an AMD Ryzen 9 9950X measured 2.76 s, and the range the redact tool quotes spans these two. 8192 lower-case characters, which every reading covers, took 5.8 s locally.

Every model smoke run prints a fresh table to its job summary. To reproduce locally after acquiring the models:

```sh
docker compose -f docker/compose.yaml up -d worker model-worker
docker compose -f docker/compose.yaml run --rm --no-deps -T model-bench
docker compose -f docker/compose.yaml --profile model down
```

## Measured NER precision and recall (informational)

`scripts/ner_measure.py` runs every sentence of `tests/fixtures/ner-sample.json` through the ONNX worker's redact task with PER, ORG and LOC, at the task's default threshold and a lower one, each its own job, and compares the models' spans with the sample's labels; rule spans are not counted. The model workflow prints a fresh table to its job summary on every run, and fails only if a job fails, never on a score. The table below is from that workflow, run 36902794112 on pull request #19 at branch head `e49e388`; the commit in its header is GitHub's merge ref. The sample is invented and small, so these figures describe these pinned models on these three registers, not a quality guarantee.

Measured 2026-10-01 UTC, commit `d5b3edc039b6370b51f5fe455785b88ef2aa5e29`, runner: GitHub-hosted ubuntu-latest, CPU: AMD EPYC 7763 64-Core Processor.
Model `Xenova/bert-base-NER:int8+Xenova/bert-base-NER-uncased:int8`; `tests/fixtures/ner-sample.json`, 48 invented sentences with 104 labelled entities; one redact job per sentence and threshold, entities PER, ORG and LOC. Exact: same label and boundaries. Overlapping: same label and a shared character, one gold span per predicted span. Masked: a shared character under any label, the share of entities a redaction hides.

| Threshold | Register or label | Gold | Predicted | Precision, exact | Recall, exact | Precision, overlapping | Recall, overlapping | Masked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.85 | news | 38 | 38 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.85 | informal | 30 | 25 | 0.72 | 0.60 | 0.84 | 0.70 | 0.80 |
| 0.85 | records | 36 | 36 | 0.94 | 0.94 | 0.94 | 0.94 | 1.00 |
| 0.85 | PER | 42 | 37 | 0.92 | 0.81 | 1.00 | 0.88 | 0.88 |
| 0.85 | ORG | 24 | 24 | 0.88 | 0.88 | 0.88 | 0.88 | 0.88 |
| 0.85 | LOC | 38 | 38 | 0.92 | 0.92 | 0.92 | 0.92 | 0.92 |
| 0.5 | news | 38 | 38 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 0.5 | informal | 30 | 33 | 0.67 | 0.73 | 0.73 | 0.80 | 1.00 |
| 0.5 | records | 36 | 36 | 0.94 | 0.94 | 0.94 | 0.94 | 1.00 |
| 0.5 | PER | 42 | 38 | 0.95 | 0.86 | 1.00 | 0.90 | 0.90 |
| 0.5 | ORG | 24 | 30 | 0.73 | 0.92 | 0.73 | 0.92 | 0.92 |
| 0.5 | LOC | 38 | 39 | 0.92 | 0.95 | 0.92 | 0.95 | 0.95 |

Since pull request #18, the redact task reads a sentence written all in lower case or all in capitals a second time, from a title-cased copy with the same offsets, and likewise a surname in capitals before a comma and a given name (`WIERZBICKI, Tomasz`), the records convention. It joins pieces of one name that touch or meet at a hyphen, full stop or apostrophe before the threshold, scored by the most confident piece, so the initial of `A. Okonkwo` carries the surname; and of two readings of the same span it keeps the more confident. Since pull request #19, the uncased `dslim/bert-base-NER-uncased` reads the whole text once more, ignoring case, beside the cased model, which keeps what capitals tell in news and records: alone, the uncased model masked only 0.54 of the development set's records against 0.77 for the cased readings. Those choices were made on a separate development set, `tests/fixtures/ner-dev.json`, which before the uncased model was measured gained lower-case names among capitals, news sentences and sentences of common words that are also names (`pay the bill`, `a rose`, `Will the courier`); the uncased model invented no name in any of them. This sample stayed unseen until each final measurement; both sets are small and were written by the same author, so the figures describe these sentences, not informal text in general.

Against pull request #18's run on the same CPU type, exact recall at 0.85 rose from 0.47 to 0.60 in informal messages and from 0.92 to 0.94 in records, and news stayed at 1.00; the share of entities masked under any label, which is what a redaction hides, rose from 0.63 to 0.80 in informal messages and from 0.97 to 1.00 in records, and at 0.5 it is 1.00 in all three. Exact precision held at 0.94 in records and moved from 0.74 to 0.72 in informal messages. What still leaks at 0.85: a name after a sign-off (`Regards, olumide`), a handle (`@nadia`), some lower-case places and first names (`porto`, `galway`, `fatima`) and a capitalised company in an informal note (`Halvern`). A reading can still take a filler word into a name (`lol tomasz`) or give a name the wrong label, which still hides it, and the uncased model tags a domain name such as `example.com` as an organisation.

int8 inference is not bit-identical across CPUs: a local run on an AMD Ryzen 9 9950X has differed from GitHub's EPYC runners by a span or two, so read such differences as noise. The redact tool's description quotes these figures; re-measure and update both together. To reproduce locally after acquiring the models:

```sh
docker compose -f docker/compose.yaml up -d model-worker
docker compose -f docker/compose.yaml run --rm --no-deps -T model-ner
NER_SAMPLE=tests/fixtures/ner-dev.json docker compose -f docker/compose.yaml run --rm --no-deps -T model-ner
```

## LLM evaluation (informational)

Run 2026-09-27 on Windows 11 with Docker Desktop and Claude Code 2.1.278, through `scripts/mcp_evaluate.py` as [the MCP guide](mcp.md#evaluating-with-an-llm) describes: the demo worker behind the compose `mcp` service, the 27-chunk fixture corpus, and one session per question per model.

| Model, as the CLI reported it | Correct | Tool calls | Median seconds per question | Cost the CLI estimated |
| --- | --- | --- | --- | --- |
| claude-sonnet-5 | 10/10 | 10 | 16.5 | $0.10 |
| claude-opus-5 | 10/10 | 23 | 26.4 | $0.54 |
| claude-haiku-4-5-20251001 | 10/10 | 10 | 20.5 | $0.11 |

Every model used two searches for the two-step question, and none of the 30 sessions had a failed tool call. Opus searched more, checking candidates against the distractors. Sonnet and Haiku made one call for each other question, except the one about the redaction backend, which they answered from the tool descriptions without a call. The seconds are wall time per session, mostly the `mcp` container starting and installing its wheels.

An earlier run the same day, on the five-chunk corpus this one replaced, also scored ten out of ten on each model. There, Sonnet and Haiku answered the two-step question with one search, because every search returned the whole corpus; that is why the corpus grew.

The scores show that these models can use the tools on this corpus, not that the evaluation separates them. One session per question gives no success rate, the demo worker matches words rather than meaning, and the questions stay close to the corpus's wording.

The semantic evaluation, `tests/mcp/evaluation-semantic.xml`, ran on 2026-09-30 with Claude Code 2.1.284, one session per question, four at a time:

| Model, as the CLI reported it | Backend | Correct | Tool calls | Note |
| --- | --- | --- | --- | --- |
| claude-haiku-4-5-20251001 | onnx | 10/10 | 55 | one question took 44 searches |
| claude-sonnet-5-5 | onnx | 10/10 | 12 | one search for five of the six search questions; one call to a tool it did not have, refused |
| claude-haiku-4-5-20251001 | lexical | 6/10 | 54 | every search answer, one after 40 searches; none of the other four |

On the demo worker, Haiku found all six search answers by rewording its queries toward the notes' words, so the agent supplied the meaning the backend lacks; it missed the rerank, the similarity ranking, the redaction and the backend question, which word matching cannot answer. The first Sonnet run on the models, started right after Haiku's, scored 5/10: every search it lost ended in HTTP 429, because the model worker counts finished jobs against its capacity of 128 for 300 seconds and Haiku's run had filled it. The tools then passed that refusal to the agent rather than waiting it out, and the row above is a second run after the worker was restarted. Since then a tool waits out a full worker within its own call, and the MCP workers keep finished jobs for 30 seconds rather than 300: the same sequence, Haiku then Sonnet on the models with no restart between them, scored 10/10 for each. That run made 22 calls in all, too few to fill the worker, so it shows the sequence works; `tests/mcp/test_capacity.py` shows the wait itself.

Before calling this production-ready, test the actual application/framework combinations, measure cold and warm latency separately on documented hardware and workloads, exercise overload/restarts, validate tenant isolation, and decide whether the in-memory lifecycle is acceptable. The current result is an experimental, testable integration—not a replacement for a durable job system.
