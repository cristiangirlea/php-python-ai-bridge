---
paths:
  - "mcp_server/**"
  - "tests/mcp/**"
  - "scripts/mcp_*"
  - "requirements-mcp.*"
---
# The MCP server

- The tool list is a literal in `tools.py`, never read from the worker: test-only tasks are never tools,
  whatever `BRIDGE_TEST_TASKS` says, and the suite runs with it set to prove that.
- `BACKENDS` names what the worker reports, per backend and task, and must equal
  `ai_bridge.tasks.MODEL_NAMES` for the same `BRIDGE_RERANK_MODEL` (`tests/mcp/test_models.py`);
  descriptions list all selectable rerankers and results identify the worker's choice. This package cannot import the worker.
  Every timing claim in a tool description is a measured median from `docs/testing.md` (a test checks),
  every path parameter says where it resolves, and every position in a result says what it counts.
- Paths resolve under `BRIDGE_MCP_ROOT` and must stay inside it after following symlinks; the server
  writes nothing, and the index builder is the only writer. A worker's free text and the token are never
  reflected; only its fixed error code is.
- A call times out after `BRIDGE_MCP_TIMEOUT_MS` and then cancels the job; a full worker is waited out
  within the call's own budget. The MCP server never shares a worker with an application.
- Dependencies are `requirements-mcp.txt` with a hash lock; `mcp-fetcher` downloads the wheels once and
  `mcp-tests` installs them offline into a tmpfs. Pin changes are reviewed, regenerated deliberately and
  tested before they land.
