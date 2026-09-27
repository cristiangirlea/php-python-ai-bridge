"""Run tests/mcp/evaluation.xml with an LLM through the Claude Code CLI, one headless session per question.

The evaluated agent gets this repository's stdio MCP server and nothing else: built-in tools are disabled, other
MCP servers and the user's own settings are not loaded, and a run whose agent could see any other tool does not
count. Answers are scored by exact match on the last <response> tag, the format of the MCP builder's harness.
It uses the CLI's own login, so no API key is needed; each question spends that account's usage.

    BRIDGE_TOKEN=... python scripts/mcp_evaluate.py --model sonnet [--model haiku] [--jobs 4] [--out FILE]

Build the fixture index first (docs/mcp.md). The report goes to stdout as Markdown, progress to stderr.
Exit status: 0 when every answer is right, 1 when any is wrong or a run did not count, 2 on refusal.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVALUATION = REPO / "tests" / "mcp" / "evaluation.xml"
FIXTURES = REPO / "tests" / "mcp" / "fixtures" / "eval"
SERVER = "bridge"
PREFIX = f"mcp__{SERVER}__"
BUILD = ("docker compose -f docker/compose.yaml run --rm mcp-index /data/notes --out /data/index "
         "--chunk-chars 300 --overlap-chars 60")
# Set by a Claude Code session for its children; a nested `claude` started with them believes it is inside one.
NESTING = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

PROMPT = """You are answering one question with the tools you have been given. Use them rather than guessing.

End your reply with two blocks, in this order:
<feedback>What helped or hindered you in the tools' names, descriptions, parameters and results, and what would
have made the task easier. Be specific.</feedback>
<response>Only the answer, in exactly the form the question asks for: a number as digits, a name as it is written
in the source. If you could not find it, NOT_FOUND.</response>"""


def load_pairs(path: Path) -> list:
    root = ElementTree.parse(path).getroot()
    return [(pair.findtext("question", "").strip(), pair.findtext("answer", "").strip())
            for pair in root.iter("qa_pair")]


def server_config(repo: Path = REPO) -> dict:
    # No env block: the server inherits the environment claude runs in (see child_env), so the token is never
    # written to the configuration file.
    return {"mcpServers": {SERVER: {"type": "stdio", "command": "sh",
                                    "args": [(repo / "scripts" / "mcp_stdio.sh").as_posix()]}}}


def child_env(environ, data: Path) -> dict:
    if not environ.get("BRIDGE_TOKEN"):
        raise ValueError("BRIDGE_TOKEN is not set; the MCP server needs the worker's token")
    env = {key: value for key, value in environ.items() if key not in NESTING}
    env["BRIDGE_MCP_DATA"] = Path(data).resolve().as_posix()  # compose mounts it, and Docker wants it absolute
    env["MSYS_NO_PATHCONV"] = "1"  # Git Bash on Windows would otherwise rewrite the container's /data paths
    env.setdefault("MCP_TIMEOUT", "180000")  # the mcp container installs its wheels into a tmpfs on every start
    return env


def command(question: str, model: str, config: Path, executable: str = "claude") -> list:
    return [executable, "-p", question, "--model", model, "--system-prompt", PROMPT,
            "--tools", "", "--mcp-config", str(config), "--strict-mcp-config", "--allowedTools", f"mcp__{SERVER}",
            "--setting-sources", "local", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]


def _last(text, tag):
    found = re.findall(rf"<{tag}>(.*?)</{tag}>", text or "", re.DOTALL)
    return found[-1].strip() if found else None


def _text(content) -> str:
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)[:300]


def read_events(lines, expected: str) -> dict:
    """Score one session from its stream-json lines; anything that is not a JSON object is ignored."""
    record = {"expected": expected, "model": None, "calls": [], "tool_errors": [], "problems": [],
              "result": None, "turns": None, "cost_usd": None}
    init = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind, message = event.get("type"), event.get("message") or {}
        if kind == "system" and event.get("subtype") == "init":
            init = event
            record["model"] = event.get("model")
        elif kind == "assistant":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    record["calls"].append({"name": str(block.get("name", "")).removeprefix(PREFIX),
                                            "input": block.get("input")})
        elif kind == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    record["tool_errors"].append(_text(block.get("content")))
        elif kind == "result":
            record.update(result=event.get("result"), turns=event.get("num_turns"),
                          cost_usd=event.get("total_cost_usd"))
            if event.get("is_error"):
                record["problems"].append(f"the CLI reported {event.get('subtype') or 'an error'}")
    if init is None:
        record["problems"].append("the CLI never reported its tools")
    else:
        others = sorted(str(tool) for tool in init.get("tools") or [] if not str(tool).startswith(PREFIX))
        if others:
            record["problems"].append("the agent could also use " + ", ".join(others))
        status = {server.get("name"): server.get("status") for server in init.get("mcp_servers") or []}
        if status.get(SERVER) != "connected":
            record["problems"].append(f"the {SERVER} server was {status.get(SERVER, 'missing')}")
    if record["result"] is None:
        record["problems"].append("the CLI returned no result")
    record["response"] = _last(record["result"], "response")
    record["feedback"] = _last(record["result"], "feedback")
    record["score"] = int(not record["problems"] and record["response"] == expected)
    return record


def ask(argv: list, expected: str, env: dict, cwd: Path, timeout: int) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
        stdout, failure = completed.stdout, None
        if completed.returncode:
            lines = completed.stderr.strip().splitlines()
            failure = f"claude exited with {completed.returncode}" + (f": {lines[-1][:200]}" if lines else "")
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else error.stdout or ""
        failure = f"timed out after {timeout} s"
    record = read_events(stdout.splitlines(), expected)
    if failure:
        record["problems"].append(failure)
        record["score"] = 0
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def refuse(message: str) -> int:
    print(f"mcp_evaluate: {message}", file=sys.stderr)
    return 2


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def report(model: str, records: list) -> str:
    seen = sorted({record["model"] for record in records if record["model"]})
    lines = [f"### {model} ({', '.join(seen) or 'model not reported'}): "
             f"{sum(record['score'] for record in records)}/{len(records)} correct", "",
             "| # | Expected | Answer | Tool calls | Seconds | Problems |", "| --- | --- | --- | --- | --- | --- |"]
    for record in records:
        calls = ", ".join(call["name"].removeprefix("bridge_") for call in record["calls"]) or "none"
        lines.append(f"| {record['n']} | {_cell(record['expected'])} | {_cell(record['response'])} | {calls} | "
                     f"{record['seconds']} | {_cell('; '.join(record['problems']) or '')} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the MCP evaluation through the Claude Code CLI.")
    parser.add_argument("--model", action="append", required=True,
                        help="a model name or alias the CLI accepts; repeat to evaluate several")
    parser.add_argument("--jobs", type=int, default=4, help="questions run at once, default 4")
    parser.add_argument("--only", default="", help="comma-separated question numbers, counting from 1")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per question, default 600")
    parser.add_argument("--out", type=Path, help="also write every session's record as JSON")
    arguments = parser.parse_args(argv)
    pairs = load_pairs(EVALUATION)
    try:
        wanted = sorted({int(number) for number in arguments.only.split(",") if number.strip()}) or \
            list(range(1, len(pairs) + 1))
    except ValueError:
        return refuse("--only takes question numbers separated by commas")
    if not 1 <= arguments.jobs <= 16 or arguments.timeout < 60 or not set(wanted) <= set(range(1, len(pairs) + 1)):
        return refuse(f"--jobs must be 1-16, --timeout at least 60, and questions 1-{len(pairs)}")
    data = Path(os.environ.get("BRIDGE_MCP_DATA") or FIXTURES).resolve()
    try:
        env = child_env(os.environ, data)
    except ValueError as error:
        return refuse(str(error))
    executable = shutil.which("claude")
    if not executable:
        return refuse("the Claude Code CLI (claude) is not on PATH")
    if not (data / "index" / "index.json").is_file():
        return refuse(f"no index at {(data / 'index').as_posix()}; build it first, from the repository root, "
                      f"with BRIDGE_MCP_DATA set to that data directory:\n  {BUILD}")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, env=env).stdout.strip()
    runs, correct, total = {}, 0, 0
    with tempfile.TemporaryDirectory(prefix="mcp-evaluate-") as scratch:
        config, cwd = Path(scratch) / "mcp.json", Path(scratch) / "cwd"
        config.write_text(json.dumps(server_config()), encoding="utf-8")
        cwd.mkdir()  # empty: no project files, no CLAUDE.md, no local settings
        for model in arguments.model:
            def one(number, model=model):
                question, answer = pairs[number - 1]
                record = ask(command(question, model, config, executable), answer, env, cwd, arguments.timeout)
                print(f"{model} question {number}: {'right' if record['score'] else 'wrong'}", file=sys.stderr)
                return {"n": number, "question": question, **record}

            with concurrent.futures.ThreadPoolExecutor(arguments.jobs) as pool:
                records = list(pool.map(one, wanted))
            runs[model] = records
            correct, total = correct + sum(record["score"] for record in records), total + len(records)
            print(report(model, records) + "\n")
    print(f"{datetime.date.today().isoformat()}, {version or 'claude version unknown'}: {correct}/{total} correct.")
    if arguments.out:
        arguments.out.write_text(json.dumps({"claude": version, "date": datetime.date.today().isoformat(),
                                             "runs": runs}, indent=2), encoding="utf-8")
    return 0 if correct == total else 1


if __name__ == "__main__":
    sys.exit(main())
