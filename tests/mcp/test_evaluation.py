"""The evaluation in evaluation.xml is for an LLM host; this proves every answer is reachable through the tools.

It runs the calls a capable agent would make, against the demo worker the compose `mcp` service uses, and
checks that the answer can be read from what the tools return. It says nothing about whether a given model
finds those calls on its own; that is what running the evaluation with an LLM measures.
"""

import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from mcp import Client

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from bridge_mcp import index
from bridge_mcp.protocol import Bridge
from bridge_mcp.tools import build_server
from semantic_reach import found, snippets

TOKEN = "test-only-bridge-token-never-use-in-production"
HERE = Path(__file__).parent
EVALUATION = HERE / "evaluation.xml"
FIXTURES = HERE / "fixtures" / "eval"
# The chunking the operator is told to use; snippets, and so which answers are reachable, depend on it.
CHUNK_CHARS, OVERLAP_CHARS = 300, 60
BUILD_COMMAND = f"mcp-index /data/notes --out /data/index --chunk-chars {CHUNK_CHARS} --overlap-chars {OVERLAP_CHARS}"


def start_demo(cls):
    """The demo worker in process, a copy of the fixtures with an index built the documented way, and a server."""
    cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=64))
    cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
    cls.thread.start()
    cls.bridge = Bridge("http://127.0.0.1:%d" % cls.server.server_address[1], TOKEN)
    cls.temp = tempfile.TemporaryDirectory()
    cls.root = Path(cls.temp.name) / "eval"
    shutil.copytree(FIXTURES, cls.root)
    # The same command the docs give the operator, minus the container: small chunks, so a snippet holds a fact.
    index.build(cls.bridge, [cls.root / "notes"], cls.root / "index", chars=CHUNK_CHARS, overlap=OVERLAP_CHARS)
    cls.mcp = build_server(cls.bridge, backend="lexical", root=cls.root, timeout_ms=10000)


def stop_demo(cls):
    cls.server.shutdown()
    cls.thread.join()
    cls.server.server_close()
    cls.temp.cleanup()


# One entry per question, in file order: the tool calls an agent needs, how the answer is read from the last
# call's structured result, and whether the question may contain its answer (it supplies the text the tool works
# on, or offers the answer as a choice). Earlier calls in a chain must surface what the next call relies on.
INDEX = {"index_path": "index"}
OFFICE = "Call the harbour office on +44 20 7946 0958 or write to office@gullrock.example."
REACH = [
    ([("bridge_search", {"query": "lantern room glass replaced", **INDEX})], found("amber"), False),
    ([("bridge_search", {"query": "copper fittings delivered lantern gallery", **INDEX})], found("Tollin Marine"), False),
    ([("bridge_search", {"query": "keeper repaired fog signal", **INDEX})], found("Merrow"), False),
    ([("bridge_redact", {"text": OFFICE})],
     lambda body: next((span["label"] for span in body["spans"] if span["source"] == "rule:phone"), None), True),
    ([("bridge_redact", {"text": OFFICE})], lambda body: str(len(body["spans"])), True),
    ([("bridge_rerank", {"query": "tide gauge brass datum plate", "documents_path": "tasks.txt", "top_k": 1})],
     lambda body: str(body["results"][0]["index"]), False),
    ([("bridge_embed_similarity", {"texts": ["lantern glass amber", "amber lantern glass", "fog signal repair"]})],
     lambda body: "".join("ABC"[body["pairs"][0][key]] for key in ("a", "b")), True),
    ([("bridge_health", {})], lambda body: "rules" if body["models"]["redact"].startswith("rules") else "model", True),
    ([("bridge_search", {"query": "light first lit year", **INDEX})], found("1871"), False),
    ([("bridge_search", {"query": "copper fittings delivered lantern gallery", **INDEX}),
      ("bridge_search", {"query": "Tollin Marine keeps yard", **INDEX})], found("Carrowby"), False),
]

# First steps of the two-step question: the one above, and the queries Sonnet, Opus and Haiku actually sent first
# when the evaluation ran through the Claude Code CLI on 2026-09-27. Each must name the company and none may
# already show its yard, or the question tests one search rather than two. The demo worker ignores word order,
# so queries that differ only in order are listed once.
FIRST_HOPS = [
    "copper fittings delivered lantern gallery",
    "lantern gallery metal fittings supplier",
    "lantern gallery metal fittings supplier company",
    "lantern gallery metal fittings supplied by company",
    "metal fittings lantern gallery company delivered",
    "metal fittings lantern gallery",
]
DOCS = HERE.parent.parent / "docs"


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pairs = [(pair.findtext("question"), pair.findtext("answer"))
                     for pair in ElementTree.parse(EVALUATION).getroot().iter("qa_pair")]
        start_demo(cls)

    @classmethod
    def tearDownClass(cls):
        stop_demo(cls)

    def test_the_file_holds_ten_distinct_answerable_questions(self):
        self.assertEqual(len(self.pairs), 10)
        self.assertEqual(len(REACH), len(self.pairs))
        self.assertEqual(len({question for question, _ in self.pairs}), 10)
        for question, answer in self.pairs:
            with self.subTest(question=question[:50]):
                self.assertTrue(question and question.strip().endswith((".", "?")))
                self.assertTrue(answer and answer == answer.strip() and "\n" not in answer)

    async def test_every_answer_is_reachable_through_the_tools(self):
        async with Client(self.mcp, raise_exceptions=True) as client:
            for (question, answer), (calls, read, _) in zip(self.pairs, REACH):
                with self.subTest(question=question[:60]):
                    for tool, arguments in calls:
                        result = await client.call_tool(tool, arguments)
                        self.assertFalse(result.is_error, (tool, result.content))
                    self.assertEqual(read(result.structured_content), answer,
                                     json.dumps(result.structured_content)[:400])

    def test_no_answer_is_given_away_by_its_question(self):
        # The fixture facts are invented, so a model cannot answer from memory; the question must not contain
        # them either, unless its REACH entry says it supplies the text or offers the answer as a choice.
        for (question, answer), (_, _, may_contain) in zip(self.pairs, REACH):
            if not may_contain:
                with self.subTest(question=question[:60]):
                    self.assertNotIn(answer.casefold(), question.casefold())

    async def test_a_default_search_sees_only_part_of_the_corpus(self):
        # With a corpus no bigger than one search's candidates, every search returns everything and the search
        # questions only test reading. A default search must consider fewer chunks than the index holds.
        async with Client(self.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("bridge_search", {"query": "lantern", **INDEX})
        body = result.structured_content
        self.assertLess(body["considered"], body["chunks"])
        self.assertLess(len(body["results"]), body["considered"])
        # The guides quote these figures; they must be the ones a search reports.
        self.assertIn(f"The notes split into {body['chunks']} chunks, so a default search considers "
                      f"{body['considered']} and returns {len(body['results'])} of them",
                      (DOCS / "mcp.md").read_text(encoding="utf-8"))
        self.assertIn(f"the {body['chunks']}-chunk fixture corpus", (DOCS / "testing.md").read_text(encoding="utf-8"))

    async def test_the_two_step_question_needs_both_steps(self):
        async with Client(self.mcp, raise_exceptions=True) as client:
            for query in FIRST_HOPS:
                with self.subTest(query=query):
                    result = await client.call_tool("bridge_search", {"query": query, **INDEX})
                    shown = snippets(result.structured_content)
                    self.assertIn("Tollin Marine", shown)
                    self.assertNotIn("Carrowby", shown)

    def test_the_rerank_question_s_line_index_is_the_tool_s_index(self):
        # The question asks for a line index; bridge_rerank counts non-blank lines. They agree only while the
        # file has no blank line.
        lines = (FIXTURES / "tasks.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines and all(line.strip() for line in lines), "tasks.txt must not contain a blank line")

    def test_the_documented_build_command_uses_the_tested_chunking(self):
        self.assertIn(BUILD_COMMAND, (DOCS / "mcp.md").read_text(encoding="utf-8"))
        # The runner prints the same command when the index is missing.
        spec = importlib.util.spec_from_file_location("mcp_evaluate", HERE.parent.parent / "scripts" / "mcp_evaluate.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        self.assertIn(BUILD_COMMAND, runner.build_command("lexical"))

    def test_every_backend_answer_follows_a_default_one(self):
        # The builder's harness reads a pair's first answer, so that one is the demo worker's and has no backend.
        for pair in ElementTree.parse(EVALUATION).getroot().iter("qa_pair"):
            answers = pair.findall("answer")
            with self.subTest(question=pair.findtext("question")[:50]):
                self.assertNotIn("backend", answers[0].attrib)
                self.assertTrue(all(answer.get("backend") in {"lexical", "onnx"} for answer in answers[1:]))
