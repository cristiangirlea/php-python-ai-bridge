"""scripts/mcp_evaluate.py runs evaluation.xml through the Claude Code CLI; these check what it can check offline.

No test starts `claude`: the command it would run, the environment and configuration it hands over, and how a
recorded stream of events is scored are all tested as data.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("mcp_evaluate", REPO / "scripts" / "mcp_evaluate.py")
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)

TOKEN = "test-only-bridge-token-never-use-in-production"
HASHING, MINILM = "hashing-bow-not-a-model", "sentence-transformers/all-MiniLM-L6-v2"
TOOLS = [f"mcp__bridge__{name}" for name in
         ("bridge_embed_similarity", "bridge_health", "bridge_redact", "bridge_rerank", "bridge_search")]


def stream(*events):
    return [json.dumps(event) for event in events]


def init(tools=TOOLS, status="connected"):
    return {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "tools": tools,
            "mcp_servers": [{"name": "bridge", "status": status}]}


def call(name, arguments):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": arguments}]}}


def outcome(text):
    return {"type": "result", "result": text, "num_turns": 2, "total_cost_usd": 0.01, "is_error": False}


class CommandTests(unittest.TestCase):
    def test_the_agent_gets_only_the_bridge_server_and_no_built_in_tools(self):
        argv = evaluate.command("What colour?", "sonnet", Path("/tmp/mcp.json"))
        self.assertEqual(argv[:3], ["claude", "-p", "What colour?"])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(Path("/tmp/mcp.json")))
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__bridge")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], evaluate.PROMPT)

    def test_user_settings_and_permission_bypasses_stay_out(self):
        argv = evaluate.command("Q?", "haiku", Path("mcp.json"))
        # Only local settings load, and the runner starts claude in an empty directory, so none do: no hooks,
        # plugins or permission rules from the user's own configuration reach the evaluated agent.
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "local")
        self.assertIn("--no-session-persistence", argv)
        for flag in ("--dangerously-skip-permissions", "--permission-mode", "--bare"):
            self.assertNotIn(flag, argv)

    def test_the_prompt_asks_for_a_tagged_answer_and_tool_feedback(self):
        for tag in ("<response>", "<feedback>", "NOT_FOUND"):
            self.assertIn(tag, evaluate.PROMPT)


class ConfigurationTests(unittest.TestCase):
    def test_the_server_is_the_documented_launcher_and_the_config_holds_no_secret(self):
        config = evaluate.server_config(REPO)
        server = config["mcpServers"]["bridge"]
        self.assertEqual(server["command"], "sh")
        self.assertEqual(Path(server["args"][0]), REPO / "scripts" / "mcp_stdio.sh")
        self.assertNotIn("\\", server["args"][0])
        self.assertNotIn(TOKEN, json.dumps(config))
        self.assertNotIn("env", server)

    def test_each_backend_has_its_own_launcher_naming_a_fixed_service(self):
        # A launcher that took its service from the environment could be pointed at a network-enabled one.
        for backend, launcher, service in [("lexical", "mcp_stdio.sh", "mcp"), ("onnx", "mcp_stdio_onnx.sh", "mcp-model")]:
            with self.subTest(backend=backend):
                server = evaluate.server_config(REPO, backend)["mcpServers"]["bridge"]
                self.assertEqual(Path(server["args"][0]), REPO / "scripts" / launcher)
                lines = [line for line in (REPO / "scripts" / launcher).read_text(encoding="utf-8").splitlines()
                         if line and not line.startswith("#")]
                self.assertEqual(lines, ['exec docker compose -f "$(dirname "$0")/../docker/compose.yaml" run --rm -i -T '
                                         + service])

    def test_an_onnx_server_gets_longer_to_start(self):
        # Its worker installs the model wheels before it answers, and compose waits for it to be healthy.
        # Longer than compose's health budget for the model worker (a 10 s start period and 60 checks 5 s apart).
        self.assertGreaterEqual(int(evaluate.child_env({"BRIDGE_TOKEN": TOKEN}, Path("/data"), "onnx")["MCP_TIMEOUT"]),
                                600000)
        self.assertEqual(evaluate.child_env({"BRIDGE_TOKEN": TOKEN}, Path("/data"))["MCP_TIMEOUT"], "180000")

    def test_the_child_environment_carries_the_token_and_drops_the_nesting_marker(self):
        environ = {"BRIDGE_TOKEN": TOKEN, "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin"}
        env = evaluate.child_env(environ, Path("fixtures/eval"))
        self.assertEqual(env["BRIDGE_TOKEN"], TOKEN)
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertTrue(Path(env["BRIDGE_MCP_DATA"]).is_absolute())
        self.assertNotIn("\\", env["BRIDGE_MCP_DATA"])
        self.assertGreaterEqual(int(env["MCP_TIMEOUT"]), 60000)
        self.assertEqual(environ["CLAUDECODE"], "1", "the caller's environment is copied, not changed")

    def test_an_operator_timeout_is_kept(self):
        env = evaluate.child_env({"BRIDGE_TOKEN": TOKEN, "MCP_TIMEOUT": "240000"}, Path("/data"))
        self.assertEqual(env["MCP_TIMEOUT"], "240000")

    def test_a_missing_token_is_refused(self):
        with self.assertRaisesRegex(ValueError, "BRIDGE_TOKEN"):
            evaluate.child_env({"PATH": "/bin"}, Path("/data"))

    def test_an_answer_can_differ_by_backend(self):
        # The first answer is the demo worker's, which is also what the MCP builder's harness reads.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "e.xml"
            path.write_text('<evaluation><qa_pair><question>Q?</question><answer>rules</answer>'
                            '<answer backend="onnx">model</answer></qa_pair></evaluation>', encoding="utf-8")
            self.assertEqual(evaluate.load_pairs(path), [("Q?", "rules")])
            self.assertEqual(evaluate.load_pairs(path, "lexical"), [("Q?", "rules")])
            self.assertEqual(evaluate.load_pairs(path, "onnx"), [("Q?", "model")])
        repository = REPO / "tests" / "mcp" / "evaluation.xml"
        demo, models = evaluate.load_pairs(repository), evaluate.load_pairs(repository, "onnx")
        self.assertEqual([answer for _, answer in demo].count("rules"), 1)
        self.assertEqual([a for (_, a), (_, b) in zip(demo, models) if a != b], ["rules"])
        self.assertIn(("model"), [answer for _, answer in models])

    def test_the_builders_share_one_command(self):
        self.assertIn("run --rm mcp-index /data/notes --out /data/index", evaluate.build_command("lexical"))
        self.assertIn("run --rm mcp-model-index /data/notes --out /data/index", evaluate.build_command("onnx"))
        self.assertEqual(evaluate.build_command("lexical").replace("mcp-index", "mcp-model-index"),
                         evaluate.build_command("onnx"))

    def test_the_questions_are_read_from_the_evaluation_file(self):
        pairs = evaluate.load_pairs(REPO / "tests" / "mcp" / "evaluation.xml")
        self.assertEqual(len(pairs), 10)
        self.assertTrue(all(question and answer == answer.strip() for question, answer in pairs))


class ScoringTests(unittest.TestCase):
    def test_a_correct_tagged_answer_scores_and_the_calls_are_kept(self):
        record = evaluate.read_events(stream(
            init(),
            call("mcp__bridge__bridge_search", {"query": "glass", "index_path": "index"}),
            outcome("<feedback>Clear.</feedback>\n<response>amber</response>"),
        ), "amber")
        self.assertEqual(record["score"], 1)
        self.assertEqual(record["response"], "amber")
        self.assertEqual(record["feedback"], "Clear.")
        self.assertEqual(record["calls"], [{"name": "bridge_search", "input": {"query": "glass", "index_path": "index"}}])
        self.assertEqual(record["model"], "claude-sonnet-5")
        self.assertEqual(record["problems"], [])

    def test_matching_is_exact_on_the_last_response_tag(self):
        text = "<response>green</response> then, on reflection, <response> amber </response>"
        self.assertEqual(evaluate.read_events(stream(init(), outcome(text)), "amber")["score"], 1)
        self.assertEqual(evaluate.read_events(stream(init(), outcome("<response>Amber</response>")), "amber")["score"], 0)
        untagged = evaluate.read_events(stream(init(), outcome("amber")), "amber")
        self.assertEqual((untagged["score"], untagged["response"]), (0, None))

    def test_a_run_that_could_see_other_tools_does_not_count(self):
        record = evaluate.read_events(stream(init(TOOLS + ["Read"]), outcome("<response>amber</response>")), "amber")
        self.assertEqual(record["score"], 0)
        self.assertIn("Read", " ".join(record["problems"]))

    def test_a_server_that_did_not_connect_does_not_count(self):
        record = evaluate.read_events(stream(init([], "failed"), outcome("<response>amber</response>")), "amber")
        self.assertEqual(record["score"], 0)
        self.assertIn("failed", " ".join(record["problems"]))

    def test_a_missing_init_or_result_is_a_problem(self):
        record = evaluate.read_events([], "amber")
        self.assertEqual(record["score"], 0)
        self.assertEqual(len(record["problems"]), 2)

    def test_unexpected_event_shapes_are_problems_not_crashes(self):
        odd = [{"type": "system", "subtype": "init", "tools": "bridge", "mcp_servers": ["bridge"]},
               {"type": "assistant", "message": "thinking"},
               {"type": "assistant", "message": {"content": "text"}},
               {"type": "user", "message": {"content": ["plain"]}},
               outcome("<response>amber</response>")]
        record = evaluate.read_events(stream(*odd), "amber")
        self.assertEqual(record["score"], 0)
        self.assertFalse(record["connected"])
        self.assertIn("the bridge server was missing", record["problems"])

    def test_the_record_says_whether_the_server_connected(self):
        self.assertTrue(evaluate.read_events(stream(init(), outcome("x")), "x")["connected"])
        self.assertFalse(evaluate.read_events(stream(init([], "failed"), outcome("x")), "x")["connected"])
        self.assertFalse(evaluate.read_events([], "x")["connected"])

    def test_tool_errors_are_recorded_and_noise_is_ignored(self):
        failed = {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "index_path must stay under the root"}]}}
        record = evaluate.read_events(["not json", *stream(init(), failed, outcome("<response>5</response>"))], "5")
        self.assertEqual(record["tool_errors"], ["index_path must stay under the root"])
        self.assertEqual(record["score"], 1)


class AskTests(unittest.TestCase):
    def test_a_timeout_keeps_what_was_streamed_and_does_not_count(self):
        streamed = "\n".join(stream(init(), outcome("<response>amber</response>"))).encode()
        timeout = subprocess.TimeoutExpired(["claude"], 60, output=streamed)
        with mock.patch.object(evaluate.subprocess, "run", side_effect=timeout):
            record = evaluate.ask(["claude"], "amber", {}, Path("."), 60)
        self.assertEqual(record["score"], 0)
        self.assertEqual(record["model"], "claude-sonnet-5")
        self.assertIn("timed out after 60 s", record["problems"])

    def test_a_timeout_before_any_output_does_not_count(self):
        with mock.patch.object(evaluate.subprocess, "run", side_effect=subprocess.TimeoutExpired(["claude"], 60)):
            record = evaluate.ask(["claude"], "amber", {}, Path("."), 60)
        self.assertEqual(record["score"], 0)
        self.assertIn("timed out after 60 s", record["problems"])

    def test_a_failed_exit_does_not_count_and_names_the_error(self):
        finished = subprocess.CompletedProcess(["claude"], 1, "\n".join(stream(init(), outcome("<response>amber</response>"))),
                                               "starting\nError: not logged in\n")
        with mock.patch.object(evaluate.subprocess, "run", return_value=finished):
            record = evaluate.ask(["claude"], "amber", {}, Path("."), 60)
        self.assertEqual(record["score"], 0)
        self.assertIn("claude exited with 1: Error: not logged in", record["problems"])


def fake_record(expected, answer=None, connected=True):
    problems = [] if connected else ["the bridge server was failed"]
    answer = expected if answer is None else answer
    return {"expected": expected, "model": "claude-sonnet-5", "calls": [], "tool_errors": [], "problems": problems,
            "result": f"<response>{answer}</response>", "turns": 1, "cost_usd": 0.0, "response": answer,
            "feedback": None, "connected": connected, "score": int(connected and answer == expected), "seconds": 0.1}


class RunTests(unittest.TestCase):
    """main() end to end with the sessions faked: what reaches --out, stderr and the exit status."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        (self.data / "index").mkdir(parents=True)
        self.index_model(HASHING)
        self.out = Path(self.temp.name) / "results.json"
        self.environ = {"BRIDGE_TOKEN": TOKEN, "BRIDGE_MCP_DATA": str(self.data), "PATH": os.environ.get("PATH", "")}
        self.calls, self.launchers = [], []

    def tearDown(self):
        self.temp.cleanup()

    def index_model(self, model):
        (self.data / "index" / "index.json").write_text(json.dumps({"format": "bridge-index/1", "model": model}),
                                                        encoding="utf-8")

    def run_main(self, fake, *arguments, environ=None):
        def ask(argv, expected, env, cwd, timeout):
            self.calls.append(argv[argv.index("-p") + 1])
            config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
            self.launchers.append(config["mcpServers"]["bridge"]["args"][0])
            return fake(len(self.calls), expected)

        stdout, stderr = io.StringIO(), io.StringIO()
        # The version probe runs a claude that does not exist here; the runner must survive that.
        with mock.patch.dict(os.environ, environ or self.environ, clear=True), \
                mock.patch.object(evaluate.shutil, "which", return_value="/nonexistent/claude"), \
                mock.patch.object(evaluate, "ask", side_effect=ask), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = evaluate.main(["--model", "sonnet", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_every_answer_right_exits_zero_and_writes_the_record(self):
        code, stdout, _ = self.run_main(lambda n, expected: fake_record(expected), "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn("10/10 correct", stdout)
        written = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(len(written["runs"]["sonnet"]), 10)

    def test_a_wrong_answer_exits_one_and_still_writes_the_record(self):
        code, _, _ = self.run_main(lambda n, expected: fake_record(expected, "wrong" if n == 3 else None),
                                   "--out", str(self.out))
        self.assertEqual(code, 1)
        self.assertEqual(sum(r["score"] for r in json.loads(self.out.read_text(encoding="utf-8"))["runs"]["sonnet"]), 9)

    def test_an_out_path_in_a_missing_directory_is_refused_before_any_session(self):
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected),
                                        "--out", str(Path(self.temp.name) / "missing" / "results.json"))
        self.assertEqual(code, 2)
        self.assertIn("--out", stderr)
        self.assertEqual(self.calls, [])

    def test_a_server_that_does_not_connect_stops_the_run_after_one_session(self):
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected, connected=False))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("mcp-fetcher", stderr)

    def test_an_onnx_server_that_does_not_connect_points_at_its_worker(self):
        self.index_model(MINILM)
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected, connected=False), "--backend", "onnx")
        self.assertEqual(code, 2)
        self.assertIn("mcp-model-worker", stderr)

    def test_an_evaluation_that_searches_no_index_needs_none(self):
        (self.data / "index" / "index.json").unlink()
        evaluation = Path(self.temp.name) / "no-index.xml"
        evaluation.write_text("<evaluation><qa_pair><question>Rank tasks.txt for the gauge.</question><answer>5</answer>"
                              "</qa_pair></evaluation>", encoding="utf-8")
        code, _, _ = self.run_main(lambda n, expected: fake_record(expected), "--evaluation", str(evaluation))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 1)

    def test_the_first_session_runs_alone_before_the_rest_start(self):
        events, lock = [], threading.Lock()

        def fake(n, expected):
            with lock:
                events.append(("start", n))
            time.sleep(0.05 if n == 1 else 0)
            with lock:
                events.append(("end", n))
            return fake_record(expected)

        code, _, _ = self.run_main(fake, "--jobs", "4")
        self.assertEqual(code, 0)
        self.assertEqual(events[:2], [("start", 1), ("end", 1)])

    def test_an_index_built_by_another_model_is_refused_with_the_matching_build_command(self):
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected), "--backend", "onnx")
        self.assertEqual(code, 2)
        self.assertIn(HASHING, stderr)
        self.assertIn("mcp-model-index /data/notes --out /data/index", stderr)
        self.assertEqual(self.calls, [])
        self.index_model(MINILM)
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected))
        self.assertEqual(code, 2)
        self.assertIn("run --rm mcp-index /data/notes --out /data/index", stderr)

    def test_an_unreadable_index_is_refused(self):
        (self.data / "index" / "index.json").write_text("not json", encoding="utf-8")
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected))
        self.assertEqual(code, 2)
        self.assertIn("index.json", stderr)

    def test_each_backend_starts_its_own_launcher(self):
        code, _, _ = self.run_main(lambda n, expected: fake_record(expected), "--only", "1")
        self.assertEqual(code, 0)
        self.index_model(MINILM)
        code, _, _ = self.run_main(lambda n, expected: fake_record(expected), "--backend", "onnx", "--only", "1")
        self.assertEqual(code, 0)
        self.assertEqual([Path(launcher).name for launcher in self.launchers], ["mcp_stdio.sh", "mcp_stdio_onnx.sh"])

    def test_another_evaluation_file_can_be_run(self):
        evaluation = Path(self.temp.name) / "other.xml"
        evaluation.write_text("<evaluation><qa_pair><question>First?</question><answer>1</answer></qa_pair>"
                              "<qa_pair><question>Second?</question><answer>2</answer></qa_pair></evaluation>",
                              encoding="utf-8")
        code, stdout, _ = self.run_main(lambda n, expected: fake_record(expected), "--evaluation", str(evaluation))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["First?", "Second?"])
        self.assertIn("2/2 correct", stdout)

    def test_an_api_key_in_the_environment_is_announced(self):
        code, _, stderr = self.run_main(lambda n, expected: fake_record(expected), "--only", "1",
                                        environ={**self.environ, "ANTHROPIC_API_KEY": "sk-test-not-real"})
        self.assertEqual(code, 0)
        self.assertIn("ANTHROPIC_API_KEY", stderr)
        self.assertNotIn("sk-test-not-real", stderr)


class RefusalTests(unittest.TestCase):
    def run_main(self, environ, which="/usr/bin/claude"):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(evaluate.shutil, "which", return_value=which), \
                contextlib.redirect_stderr(stderr):
            code = evaluate.main(["--model", "sonnet"])
        return code, stderr.getvalue()

    def test_it_refuses_without_a_token(self):
        code, stderr = self.run_main({"PATH": "/bin"})
        self.assertEqual(code, 2)
        self.assertIn("BRIDGE_TOKEN", stderr)

    def test_it_refuses_without_the_claude_cli(self):
        code, stderr = self.run_main({"BRIDGE_TOKEN": TOKEN}, which=None)
        self.assertEqual(code, 2)
        self.assertIn("claude", stderr)

    def test_it_refuses_without_a_built_index_and_names_the_build_command(self):
        with tempfile.TemporaryDirectory() as empty:
            code, stderr = self.run_main({"BRIDGE_TOKEN": TOKEN, "BRIDGE_MCP_DATA": empty})
        self.assertEqual(code, 2)
        self.assertIn("mcp-index /data/notes --out /data/index", stderr)
        self.assertNotIn(TOKEN, stderr)


if __name__ == "__main__":
    unittest.main()
