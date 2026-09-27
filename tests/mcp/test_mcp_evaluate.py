"""scripts/mcp_evaluate.py runs evaluation.xml through the Claude Code CLI; these check what it can check offline.

No test starts `claude`: the command it would run, the environment and configuration it hands over, and how a
recorded stream of events is scored are all tested as data.
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("mcp_evaluate", REPO / "scripts" / "mcp_evaluate.py")
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)

TOKEN = "test-only-bridge-token-never-use-in-production"
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

    def test_the_child_environment_carries_the_token_and_drops_the_nesting_marker(self):
        environ = {"BRIDGE_TOKEN": TOKEN, "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin"}
        env = evaluate.child_env(environ, Path("fixtures/eval"))
        self.assertEqual(env["BRIDGE_TOKEN"], TOKEN)
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertTrue(Path(env["BRIDGE_MCP_DATA"]).is_absolute())
        self.assertNotIn("\\", env["BRIDGE_MCP_DATA"])
        self.assertEqual(env["MSYS_NO_PATHCONV"], "1")
        self.assertGreaterEqual(int(env["MCP_TIMEOUT"]), 60000)
        self.assertEqual(environ["CLAUDECODE"], "1", "the caller's environment is copied, not changed")

    def test_an_operator_timeout_is_kept(self):
        env = evaluate.child_env({"BRIDGE_TOKEN": TOKEN, "MCP_TIMEOUT": "240000"}, Path("/data"))
        self.assertEqual(env["MCP_TIMEOUT"], "240000")

    def test_a_missing_token_is_refused(self):
        with self.assertRaisesRegex(ValueError, "BRIDGE_TOKEN"):
            evaluate.child_env({"PATH": "/bin"}, Path("/data"))

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

    def test_tool_errors_are_recorded_and_noise_is_ignored(self):
        failed = {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "index_path must stay under the root"}]}}
        record = evaluate.read_events(["not json", *stream(init(), failed, outcome("<response>5</response>"))], "5")
        self.assertEqual(record["tool_errors"], ["index_path must stay under the root"])
        self.assertEqual(record["score"], 1)


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
