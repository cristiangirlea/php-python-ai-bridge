---
paths:
  - "worker/**"
  - "tests/python/**"
  - "scripts/fetch_model.py"
---
# The Python worker

- The worker path is standard library only. `numpy`, `onnxruntime` and `tokenizers` are imported inside
  the ONNX functions of `tasks.py`, never at module top, so the demo backend and every test run without
  them; the post-edit hook reports a top-level import of a model dependency. Model dependencies belong
  in `requirements-model.txt` and its hash lock, downloaded only by the `fetcher` service; the model
  worker installs them offline from `.cache/wheels` into a tmpfs when it starts.
- Tasks are an allowlist in `tasks.py`, each with `validate()` before `execute()` and each run in its own
  process. Clients get fixed error codes, never exception text, and the token is never logged or returned.
  Test-only tasks exist only with `BRIDGE_TEST_TASKS=1` (compose's `fault-worker`).
- `MODEL_NAMES` is what every result reports as `model` and what the MCP server's `BACKENDS` table must
  equal (`tests/mcp/test_models.py`); change both together.
- Limits live in three places: concurrency, capacity, the per-owner cap and retention are `Settings` in
  `jobs.py` (defaults from the environment in `server.main`), the body size and the token limits are
  `MAX_BODY`, `MAX_TOKENS`, `MAX_TOKENS_FILE` and `TOKEN_NAME` in `server.py`, and the field, count and
  character limits are the `MAX_*` constants in `tasks.py`; the deadline range is checked in `JobStore.submit`. `docs/protocol.md`
  documents them; change the doc with the limit.
- Tests run offline with `python -m unittest discover -s tests/python` inside `python-tests`
  (`network_mode: none`). A change to the model path belongs on `model-smoke.yml`'s `paths:` list, so
  the real-model smoke tests run for it; keep that list current when a file moves.
