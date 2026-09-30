"""End-to-end check of the ONNX-backed MCP server: the compose `mcp-model` service over stdio, started the way an
MCP host starts it. Asserts correctness only, never speed; a smoke check, not a quality benchmark.

It also makes the calls for every question in tests/mcp/evaluation-semantic.xml and checks that each answer is
reachable with the models; the offline tests check that the demo worker reaches none of them.

Needs BRIDGE_TOKEN, the fetched models and wheels (the fetcher and mcp-fetcher services), and BRIDGE_MCP_DATA set
to the absolute path of a directory holding tests/mcp/fixtures/eval/notes as notes/, its tasks.txt, and an index
that mcp-model-index built from the notes as index/. Everything but the result goes to stderr.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "mcp_stdio_onnx.sh"
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tests" / "mcp"))
sys.path.insert(0, str(REPO / "scripts"))
from ai_bridge.tasks import MODEL_NAMES  # noqa: E402  (standard library only at import)
from model_cases import REDACT_TEXT, RERANK_CASES, SIMILAR_TEXTS  # noqa: E402
from mcp_evaluate import load_pairs  # noqa: E402  (the runner's rule for which answer a backend expects)
from semantic_reach import SEMANTIC_REACH  # noqa: E402

SEMANTIC = REPO / "tests" / "mcp" / "evaluation-semantic.xml"

TOOLS = ["bridge_embed_similarity", "bridge_health", "bridge_redact", "bridge_rerank", "bridge_search"]
TIMEOUT_S = 600  # the model worker installs its wheels and the first calls load each model cold

CALLS = {
    "health": ("bridge_health", {}),
    "rerank": ("bridge_rerank", {"query": RERANK_CASES[0][0], "documents": RERANK_CASES[0][1]}),
    "similarity": ("bridge_embed_similarity", {"texts": SIMILAR_TEXTS}),
    "redact": ("bridge_redact", {"text": REDACT_TEXT, "entities": ["PER", "LOC"]}),
    # Worded unlike the note ("replaced with amber glass"), so word overlap alone would not rank it first.
    "search": ("bridge_search", {"query": "What colour is the glazing up in the lamp housing since it was restored?",
                                 "index_path": "index", "top_k": 3}),
}


def exchange(messages, wanted):
    """Send every message, then read replies until each wanted id has one; kill the server on timeout."""
    process = subprocess.Popen(["sh", str(LAUNCHER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                               encoding="utf-8")
    expired = threading.Event()
    timer = threading.Timer(TIMEOUT_S, lambda: (expired.set(), process.kill()))
    timer.start()
    replies = {}
    try:
        process.stdin.write("".join(json.dumps(message) + "\n" for message in messages))
        process.stdin.flush()
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(f"not JSON-RPC, ignored: {line.rstrip()[:200]}", file=sys.stderr)
                continue
            replies[message.get("id")] = message
            if wanted <= replies.keys():
                break
    finally:
        timer.cancel()
        process.stdin.close()
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:  # a server that ignores the end of its input must not hide the result
            process.kill()
            code = process.wait()
    missing = wanted - replies.keys()
    assert not missing, (f"no reply to {sorted(missing)} within {TIMEOUT_S} s" if expired.is_set()
                         else f"the server exited with {code} before replying to {sorted(missing)}")
    return replies


def main():
    messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcp-model-probe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ]
    names = list(CALLS)
    messages += [{"jsonrpc": "2.0", "id": 2 + i, "method": "tools/call",
                  "params": {"name": CALLS[name][0], "arguments": CALLS[name][1]}} for i, name in enumerate(names)]
    # Then every semantic question's calls, in order; each entry's answer is read from its last call.
    semantic_calls = [call for calls, _, _ in SEMANTIC_REACH for call in calls]
    first = 2 + len(names)
    messages += [{"jsonrpc": "2.0", "id": first + i, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}} for i, (tool, arguments) in enumerate(semantic_calls)]
    replies = exchange(messages, set(range(first + len(semantic_calls))))
    for number, reply in replies.items():
        assert "error" not in reply, (number, reply["error"])
    results = {}
    for i, name in enumerate(names):
        result = replies[2 + i]["result"]
        assert not result.get("isError"), (name, result.get("content"))
        results[name] = result["structuredContent"]

    tools = replies[1]["result"]["tools"]
    assert sorted(tool["name"] for tool in tools) == TOOLS, [tool["name"] for tool in tools]
    described = " ".join(tool.get("description", "") for tool in tools)
    for model in MODEL_NAMES["onnx"].values():
        assert model.split(":")[0] in described, f"{model} is not named in the tool descriptions"
    assert "not-a-model" not in described

    assert results["health"]["backend"] == "onnx", results["health"]
    assert results["health"]["models"] == MODEL_NAMES["onnx"], results["health"]["models"]
    assert results["rerank"]["model"] == MODEL_NAMES["onnx"]["rerank"]
    assert results["rerank"]["results"][0]["index"] == RERANK_CASES[0][2], results["rerank"]["results"]
    assert results["similarity"]["model"] == MODEL_NAMES["onnx"]["embed"]
    top = results["similarity"]["pairs"][0]
    assert (top["a"], top["b"]) == (0, 1), results["similarity"]["pairs"]
    sources = {span["source"] for span in results["redact"]["spans"]}
    assert {"rule:email", "model:PER", "model:LOC"} <= sources, results["redact"]["spans"]
    assert results["search"]["embed_model"] == MODEL_NAMES["onnx"]["embed"], results["search"]
    assert results["search"]["rerank_model"] == MODEL_NAMES["onnx"]["rerank"], results["search"]
    shown = " ".join(hit["snippet"] for hit in results["search"]["results"])
    assert "amber" in shown, results["search"]["results"]
    answers = [answer for _, answer in load_pairs(SEMANTIC, "onnx")]
    assert len(answers) == len(SEMANTIC_REACH), (len(answers), len(SEMANTIC_REACH))
    number = first
    for position, ((calls, read, _), answer) in enumerate(zip(SEMANTIC_REACH, answers), start=1):
        number += len(calls)
        result = replies[number - 1]["result"]
        assert not result.get("isError"), (position, result.get("content"))
        got = read(result["structuredContent"])
        assert got == answer, f"semantic question {position}: expected {answer!r}, reachable {got!r}"
    print("mcp-model probe: the ONNX-backed MCP server answered every check and every semantic question")


if __name__ == "__main__":
    main()
