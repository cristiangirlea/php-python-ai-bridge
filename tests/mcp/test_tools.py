"""The MCP tools, driven through the SDK's in-memory client against a real in-process worker."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from bridge_mcp.protocol import Bridge
from bridge_mcp.tools import BY_VALUE_DOCUMENTS, TOOL_NAMES, _rankings, _snippet, _spans, _vectors, build_server

TOKEN = "test-only-bridge-token-never-use-in-production"


class ToolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Test tasks are enabled on purpose: the tool list must not grow because of them.
        cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=32, test_tasks=True))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.bridge = Bridge(f"http://{host}:{port}", TOKEN)
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "data"
        cls.root.mkdir()
        documents = ["no matching words"] * 40
        documents[7] = "needle token"
        (cls.root / "candidates.json").write_text(json.dumps(documents), encoding="utf-8")
        (cls.root / "candidates.txt").write_text("\n".join(documents) + "\n", encoding="utf-8")
        (cls.root / "large.txt").write_text("\n".join(["needle token"] + ["other"] * 511) + "\n", encoding="utf-8")
        (cls.root / "toomany.txt").write_text("\n".join(["x"] * 513) + "\n", encoding="utf-8")
        (cls.root / "empty.txt").write_text("\n\n", encoding="utf-8")
        (cls.root / "blank-lines.txt").write_text("first line\n\n   \nneedle token\n", encoding="utf-8")
        (cls.root / "budget.txt").write_text("\n".join(["y" * 8000] * 24) + "\n", encoding="utf-8")
        cls.outside = Path(cls.temp.name) / "outside.json"
        cls.outside.write_text(json.dumps(documents), encoding="utf-8")
        cls.mcp = build_server(cls.bridge, backend="lexical", root=cls.root, timeout_ms=10000)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.temp.cleanup()

    async def call(self, name, arguments, server=None, **options):
        async with Client(server or self.mcp, raise_exceptions=True) as client:
            return await client.call_tool(name, arguments, **options)

    async def tools(self, server=None):
        async with Client(server or self.mcp, raise_exceptions=True) as client:
            return (await client.list_tools()).tools

    async def test_tool_list_is_exactly_the_allowlisted_tasks(self):
        self.assertEqual(os.environ.get("BRIDGE_TEST_TASKS"), "1", "the harness must run with test tasks enabled")
        tools = await self.tools()
        self.assertEqual(sorted(tool.name for tool in tools), sorted(TOOL_NAMES))
        self.assertEqual(sorted(TOOL_NAMES), ["bridge_embed_similarity", "bridge_health", "bridge_redact", "bridge_rerank",
                                              "bridge_search"])
        self.assertFalse(any("test" in tool.name for tool in tools))

    async def test_descriptions_state_the_real_backend_and_never_the_token(self):
        text = " ".join(tool.description or "" for tool in await self.tools())
        for honest in ["lexical-demo-not-a-model", "hashing-bow-not-a-model", "rules-only-not-a-model"]:
            self.assertIn(honest, text)
        self.assertNotIn(TOKEN, text)
        onnx = build_server(self.bridge, backend="onnx", root=None, timeout_ms=10000)
        text = " ".join(tool.description or "" for tool in await self.tools(onnx))
        for model in ["cross-encoder/ms-marco-TinyBERT-L2-v2", "sentence-transformers/all-MiniLM-L6-v2", "bert-base-NER",
                      "bert-base-NER-uncased"]:
            self.assertIn(model, text)
        self.assertNotIn("not-a-model", text)
        self.assertNotIn(TOKEN, text)

    async def test_every_timing_claim_is_a_measured_median(self):
        # Descriptions may only quote the measured cold-job table, never an unmeasured range.
        for backend in ("lexical", "onnx"):
            server = build_server(self.bridge, backend=backend, root=None, timeout_ms=10000)
            for tool in await self.tools(server):
                if tool.name == "bridge_health":
                    continue
                with self.subTest(backend=backend, tool=tool.name):
                    self.assertNotIn("one to ten seconds", tool.description)
                    self.assertIn("median of five cold jobs", tool.description)
                    self.assertIn("docs/testing.md", tool.description)

    async def test_health_reports_the_worker_backend(self):
        result = await self.call("bridge_health", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["backend"], "lexical")
        self.assertEqual(result.structured_content["protocol"], 1)
        self.assertEqual(result.structured_content["models"], {
            "rerank": "lexical-demo-not-a-model", "embed": "hashing-bow-not-a-model", "redact": "rules-only-not-a-model"})

    async def test_rerank_by_value_returns_only_the_top_k_with_snippets(self):
        documents = ["no match"] * 10 + ["needle token"]
        result = await self.call("bridge_rerank", {"query": "needle token", "documents": documents, "top_k": 2})
        self.assertFalse(result.is_error, result.content)
        body = result.structured_content
        self.assertEqual(body["model"], "lexical-demo-not-a-model")
        self.assertEqual(body["considered"], 11)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["results"][0], {"index": 10, "score": 1.0, "snippet": "needle token"})

    async def test_rerank_by_path_reads_json_arrays_and_line_files_under_the_root(self):
        for name in ["candidates.json", "candidates.txt", str(self.root / "candidates.txt")]:
            with self.subTest(path=name):
                result = await self.call("bridge_rerank", {"query": "needle token", "documents_path": name, "top_k": 3})
                self.assertFalse(result.is_error, result.content)
                body = result.structured_content
                self.assertEqual(body["considered"], 40)
                self.assertEqual([item["index"] for item in body["results"]][0], 7)
                self.assertEqual(len(body["results"]), 3)

    async def test_a_line_file_is_indexed_by_its_non_blank_lines(self):
        # What the description promises: blank and whitespace-only lines are not documents, so they are not counted.
        result = await self.call("bridge_rerank", {"query": "needle token", "documents_path": "blank-lines.txt", "top_k": 1})
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.structured_content["considered"], 2)
        self.assertEqual(result.structured_content["results"][0]["index"], 1)

    async def test_paths_and_positions_are_explained_where_the_agent_reads_them(self):
        # A model sees a tool's description and input schema; many hosts never show it the output schema. In the
        # first LLM run Opus had to infer both of these from the data (docs/testing.md).
        tools = {tool.name: tool for tool in await self.tools()}
        rerank, similarity, search = tools["bridge_rerank"], tools["bridge_embed_similarity"], tools["bridge_search"]
        for tool, argument in [(rerank, "documents_path"), (search, "index_path")]:
            with self.subTest(argument=argument):
                described = tool.input_schema["properties"][argument]["description"]
                self.assertIn("relative to the server's root", described)
                # Relative paths are confined too, and after symlinks, not only absolute ones.
                self.assertIn("must stay inside that root after following symlinks", described)
        self.assertIn("non-blank line", rerank.input_schema["properties"]["documents_path"]["description"])
        self.assertIn("zero-based position", rerank.description)
        self.assertIn("non-blank lines", rerank.description)
        self.assertIn("zero-based position", similarity.description)

    async def test_every_position_in_a_result_is_described_in_the_output_schema(self):
        # For hosts that do show the output schema: each field that locates something says what it counts. The
        # walk follows $ref wherever the SDK puts a definition, so inlining or renaming them changes nothing here.
        def described(schema, *path):
            node = schema
            for name in path:
                while "$ref" in node:
                    node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
                node = node["items"] if name == "[]" else node["properties"][name]
            return node.get("description", "")

        tools = {tool.name: tool.output_schema for tool in await self.tools()}
        expected = [("bridge_rerank", ("results", "[]", "index"), "zero-based"),
                    ("bridge_embed_similarity", ("pairs", "[]", "a"), "zero-based"),
                    ("bridge_embed_similarity", ("pairs", "[]", "b"), "zero-based"),
                    ("bridge_embed_similarity", ("matrix",), "in the order of texts"),
                    ("bridge_search", ("results", "[]", "source"), "relative to the server's root"),
                    ("bridge_search", ("results", "[]", "chunk"), "zero-based"),
                    ("bridge_search", ("results", "[]", "start"), "code-point offset"),
                    ("bridge_search", ("results", "[]", "end"), "exclusive"),
                    ("bridge_redact", ("spans", "[]", "start"), "code-point offset"),
                    ("bridge_redact", ("spans", "[]", "end"), "exclusive")]
        for tool, path, words in expected:
            with self.subTest(tool=tool, field=".".join(path)):
                self.assertIn(words, described(tools[tool], *path))

    async def test_rerank_refuses_paths_outside_the_root_and_bad_files(self):
        for path in ["../outside.json", str(self.outside), "missing.json", "/etc/hostname", "empty.txt", "toomany.txt"]:
            with self.subTest(path=path):
                result = await self.call("bridge_rerank", {"query": "x", "documents_path": path})
                self.assertTrue(result.is_error, path)
        for arguments in [{"query": "x"}, {"query": "x", "documents": ["a"], "documents_path": "candidates.json"},
                          {"query": "x", "documents": ["a"] * (BY_VALUE_DOCUMENTS + 1)}, {"query": " ", "documents": ["a"]}]:
            with self.subTest(arguments=str(arguments)[:60]):
                self.assertTrue((await self.call("bridge_rerank", arguments)).is_error)

    async def test_rerank_without_a_configured_root_refuses_paths(self):
        unrooted = build_server(self.bridge, backend="lexical", root=None, timeout_ms=10000)
        result = await self.call("bridge_rerank", {"query": "x", "documents_path": "candidates.json"}, server=unrooted)
        self.assertTrue(result.is_error)
        self.assertIn("BRIDGE_MCP_ROOT", result.content[0].text)

    async def test_embed_similarity_returns_a_matrix_and_pairs_never_vectors(self):
        result = await self.call("bridge_embed_similarity", {"texts": ["red apple", "apple red", "blue sky"]})
        self.assertFalse(result.is_error, result.content)
        body = result.structured_content
        self.assertEqual(body["model"], "hashing-bow-not-a-model")
        self.assertNotIn("vectors", body)
        matrix = body["matrix"]
        self.assertEqual(len(matrix), 3)
        self.assertEqual([matrix[i][i] for i in range(3)], [1.0, 1.0, 1.0])
        self.assertEqual(matrix[0][1], 1.0)
        self.assertEqual(matrix[0][1], matrix[1][0])
        self.assertEqual(body["pairs"][0], {"a": 0, "b": 1, "similarity": 1.0})
        self.assertTrue((await self.call("bridge_embed_similarity", {"texts": ["only one"]})).is_error)

    async def test_redact_masks_and_reports_sources(self):
        result = await self.call("bridge_redact", {"text": "mail x@y.io today"})
        self.assertFalse(result.is_error, result.content)
        body = result.structured_content
        self.assertEqual(body["text"], "mail [EMAIL] today")
        self.assertEqual(body["spans"][0]["source"], "rule:email")
        self.assertEqual(body["model"], "rules-only-not-a-model")
        self.assertTrue((await self.call("bridge_redact", {"text": " "})).is_error)

    async def test_progress_is_forwarded_when_the_host_listens(self):
        seen = []

        async def listen(progress, total, message):
            seen.append((progress, total))

        result = await self.call("bridge_rerank", {"query": "needle token", "documents_path": "large.txt", "top_k": 1},
                                 progress_callback=listen)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.structured_content["results"][0]["index"], 0)
        self.assertTrue(seen, "at least the final progress must be reported")
        self.assertEqual(seen[-1], (512, 512))
        self.assertEqual(seen, sorted(seen))

    async def test_rerank_pre_checks_include_the_query_in_the_budget(self):
        documents = ["y" * 8000] * 24
        result = await self.call("bridge_rerank", {"query": "x" * 8001, "documents": documents[:1]})
        self.assertFalse(result.is_error, result.content)
        result = await self.call("bridge_rerank", {"query": "x" * 8001, "documents_path": "budget.txt"})
        self.assertTrue(result.is_error)
        self.assertIn("200000", result.content[0].text)

    async def test_worker_failures_become_tool_errors_without_internals(self):
        broken = build_server(Bridge("http://127.0.0.1:1", TOKEN, request_timeout_s=0.5), backend="lexical", root=None, timeout_ms=10000)
        result = await self.call("bridge_health", {}, server=broken)
        self.assertTrue(result.is_error)
        self.assertNotIn(TOKEN, result.content[0].text)


class ResultValidationTests(unittest.TestCase):
    """The tools apply the PHP typed results' rules to what the worker returns."""

    def test_rankings_need_exact_count_unique_indexes_and_stable_order(self):
        good = {"model": "m", "rankings": [{"index": 1, "score": 2.0}, {"index": 0, "score": 1.0}]}
        self.assertEqual(_rankings(good, 2, 2), good["rankings"])
        self.assertEqual(len(_rankings({"model": "m", "rankings": good["rankings"][:1]}, 2, 1)), 1)
        for bad in [{"model": "m", "rankings": good["rankings"][:1]},
                    {"model": "m", "rankings": [{"index": 1, "score": 2.0}, {"index": 1, "score": 1.0}]},
                    {"model": "m", "rankings": [{"index": 0, "score": 1.0}, {"index": 1, "score": 2.0}]},
                    {"model": "m", "rankings": [{"index": 1, "score": 1.0}, {"index": 0, "score": 1.0}]},
                    {"model": "m", "rankings": [{"index": 1, "score": 2.0}, {"index": 0, "score": float("inf")}]},
                    {"model": "m", "rankings": [{"index": 1, "score": 2.0}, {"index": 2, "score": 1.0}]},
                    {"model": "", "rankings": good["rankings"]}]:
            with self.subTest(bad=str(bad)[:70]), self.assertRaises(ToolError):
                _rankings(bad, 2, 2)

    def test_vectors_need_the_declared_dimension_finite_and_unit_length(self):
        good = {"model": "m", "dimensions": 3, "vectors": [[1.0, 0, 0], [0, 0.6, 0.8]]}
        self.assertEqual(_vectors(good, 2), good["vectors"])
        for bad in [{"model": "m", "dimensions": 3, "vectors": [[1.0, 0, 0]]},
                    {"model": "m", "dimensions": 2, "vectors": good["vectors"]},
                    {"model": "m", "dimensions": 3, "vectors": [[2.0, 0, 0], [0, 0.6, 0.8]]},
                    {"model": "m", "dimensions": 3, "vectors": [[1.0, 0, float("nan")], [0, 0.6, 0.8]]},
                    {"model": "m", "dimensions": 3, "vectors": [[1.0, 0, "0"], [0, 0.6, 0.8]]}]:
            with self.subTest(bad=str(bad)[:70]), self.assertRaises(ToolError):
                _vectors(bad, 2)

    def test_spans_need_to_be_sorted_disjoint_and_inside_the_text(self):
        text = "caf\u00e9: x@y.io"
        span = {"start": 6, "end": 12, "label": "EMAIL", "source": "rule:email", "score": 1.0}
        self.assertEqual(_spans({"model": "m", "text": "caf\u00e9: [EMAIL]", "spans": [span]}, text), [span])
        for spans in [[{**span, "end": 13}], [{**span, "start": 12}], [{**span, "score": 1.5}], [{**span, "label": ""}],
                      [{**span, "start": 7, "end": 12}, {**span, "start": 0, "end": 4}],
                      [{**span, "start": 0, "end": 8}, {**span, "start": 6, "end": 12}], ["not a span"]]:
            with self.subTest(spans=str(spans)[:70]), self.assertRaises(ToolError):
                _spans({"model": "m", "text": "masked", "spans": spans}, text)
        with self.assertRaises(ToolError):
            _spans({"model": "m", "text": "", "spans": []}, text)


class SnippetTests(unittest.TestCase):
    def test_centres_on_the_longest_whole_query_word(self):
        text = "this chunk " + "filler " * 40 + "then the needle is here " + "tail " * 30
        snippet = _snippet(text, "what is the needle")
        self.assertIn("needle", snippet, "short words inside longer ones must not pull the window back")
        self.assertTrue(snippet.startswith("\u2026") and snippet.endswith("\u2026"))
        self.assertLessEqual(len(snippet), 160)
        self.assertEqual(_snippet(text, "absent words"), _snippet(text))
        self.assertEqual(_snippet("short text", "text"), "short text")
