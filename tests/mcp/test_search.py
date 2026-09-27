"""bridge_search through the SDK's in-memory client, against a real in-process worker and a built index."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from bridge_mcp import index
from bridge_mcp.protocol import Bridge
from bridge_mcp.tools import build_server

TOKEN = "test-only-bridge-token-never-use-in-production"


def corpus(needle_paragraph):
    """Twelve paragraphs of words unique to each paragraph; one also holds the needle words."""
    paragraphs = []
    for p in range(12):
        words = [f"para{p}w{j}" for j in range(20)]
        if p == needle_paragraph:
            words[10:13] = ["needle", "token", "quartz"]
        paragraphs.append(" ".join(words))
    return "\n\n".join(paragraphs)


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=128))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.bridge = Bridge("http://127.0.0.1:%d" % cls.server.server_address[1], TOKEN)
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "data"
        (cls.root / "docs").mkdir(parents=True)
        (cls.root / "docs" / "field.md").write_text(corpus(7), encoding="utf-8")
        # 60 characters of overlap keep the 19-character needle phrase whole in at least one chunk.
        index.build(cls.bridge, [cls.root / "docs"], cls.root / "index", chars=200, overlap=60)
        segments = [" ".join(f"s{i}w{j}" for j in range(36))[:190] for i in range(3)]
        (cls.root / "tiny.txt").write_text("\n".join(segments), encoding="utf-8")
        index.build(cls.bridge, [cls.root / "tiny.txt"], cls.root / "small", chars=200, overlap=0)
        assert index.load_index(cls.root / "small" / index.SIDECAR).count == 3, "the small index must hold 3 chunks"
        cls.outside = Path(cls.temp.name) / "outside_index"
        index.build(cls.bridge, [cls.root / "docs"], cls.outside, chars=200, overlap=60)
        cls.mcp = build_server(cls.bridge, backend="lexical", root=cls.root, timeout_ms=10000)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.temp.cleanup()

    async def call(self, arguments, server=None, **options):
        async with Client(server or self.mcp, raise_exceptions=True) as client:
            return await client.call_tool("bridge_search", arguments, **options)

    async def description(self, server):
        async with Client(server, raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        return tools["bridge_search"].description

    def copy_index(self, name, **changes):
        target = self.root / name
        shutil.copytree(self.root / "index", target)
        meta = json.loads((target / index.SIDECAR).read_text(encoding="utf-8"))
        for key, value in changes.items():
            if key == "vectors_file":
                meta["vectors"]["file"] = value
            else:
                meta[key] = value
        (target / index.SIDECAR).write_text(json.dumps(meta), encoding="utf-8")
        return target

    async def test_the_description_names_both_backends_and_the_builder(self):
        text = await self.description(self.mcp)
        for expected in ["hashing-bow-not-a-model", "lexical-demo-not-a-model", "python -m bridge_mcp.index build"]:
            self.assertIn(expected, text)
        text = await self.description(build_server(self.bridge, backend="onnx", root=self.root, timeout_ms=10000))
        for expected in ["sentence-transformers/all-MiniLM-L6-v2", "cross-encoder/ms-marco-TinyBERT-L2-v2"]:
            self.assertIn(expected, text)
        self.assertNotIn("not-a-model", text)

    async def test_finds_the_needle_chunk_with_offsets_snippets_and_never_vectors(self):
        for index_path in ["index", "index/index.json", str(self.root / "index")]:
            with self.subTest(index_path=index_path):
                result = await self.call({"query": "needle token quartz", "index_path": index_path, "top_k": 3})
                self.assertFalse(result.is_error, result.content)
                body = result.structured_content
                self.assertNotIn("vectors", json.dumps(body))
                self.assertEqual((body["embed_model"], body["rerank_model"]), ("hashing-bow-not-a-model", "lexical-demo-not-a-model"))
                loaded = index.load_index(self.root / "index" / index.SIDECAR)
                self.assertEqual(body["chunks"], loaded.count)
                self.assertEqual(body["considered"], min(loaded.count, 20))
                self.assertEqual(len(body["results"]), 3)
                top = body["results"][0]
                self.assertEqual(top["source"], "docs/field.md")
                text = (self.root / top["source"]).read_text(encoding="utf-8")
                self.assertIn("quartz", text[top["start"]:top["end"]])
                self.assertIn("quartz", top["snippet"])
                self.assertEqual(top["rerank_score"], 1.0)
                self.assertEqual(top["similarity"], max(hit["similarity"] for hit in body["results"]))

    async def test_refuses_an_index_built_with_another_model_before_any_job(self):
        self.copy_index("other_model", model="sentence-transformers/all-MiniLM-L6-v2")
        with patch.object(self.bridge, "run", wraps=self.bridge.run) as run:
            result = await self.call({"query": "needle", "index_path": "other_model"})
            self.assertTrue(result.is_error)
            for expected in ["sentence-transformers/all-MiniLM-L6-v2", "hashing-bow-not-a-model", "bridge_health"]:
                self.assertIn(expected, result.content[0].text)
            onnx = build_server(self.bridge, backend="onnx", root=self.root, timeout_ms=10000)
            result = await self.call({"query": "needle", "index_path": "index"}, server=onnx)
            self.assertTrue(result.is_error)
            self.assertIn("hashing-bow-not-a-model", result.content[0].text)
        run.assert_not_called()

    async def test_refuses_an_embedding_reply_from_another_model(self):
        class Other:
            calls = 0

            def health(self):
                return {"backend": "lexical", "protocol": 1}

            def run(self, task, payload, *args, **kwargs):
                Other.calls += 1
                return {"model": "another-model", "dimensions": 384, "vectors": [[1.0] + [0.0] * 383]}

        server = build_server(Other(), backend="lexical", root=self.root, timeout_ms=10000)
        result = await self.call({"query": "needle", "index_path": "index"}, server=server)
        self.assertTrue(result.is_error)
        self.assertIn("another-model", result.content[0].text)
        self.assertEqual(Other.calls, 1)

    async def test_confines_index_paths_and_their_vectors_to_the_root(self):
        self.copy_index("escaping", vectors_file="../../x")
        linked = self.copy_index("linked")
        (linked / index.VECTORS).unlink()
        (linked / index.VECTORS).symlink_to(self.outside / index.VECTORS)
        for index_path in ["../outside_index", str(self.outside), "missing", "docs", "escaping", "linked"]:
            with self.subTest(index_path=index_path):
                result = await self.call({"query": "needle", "index_path": index_path})
                self.assertTrue(result.is_error, index_path)
        unrooted = build_server(self.bridge, backend="lexical", root=None, timeout_ms=10000)
        result = await self.call({"query": "needle", "index_path": "index"}, server=unrooted)
        self.assertTrue(result.is_error)
        self.assertIn("BRIDGE_MCP_ROOT", result.content[0].text)

    async def test_reloads_the_index_when_its_files_change(self):
        (self.root / "moving").mkdir()
        (self.root / "moving" / "field.md").write_text(corpus(2), encoding="utf-8")
        index.build(self.bridge, [self.root / "moving" / "field.md"], self.root / "moving" / "idx", chars=200, overlap=60)
        server = build_server(self.bridge, backend="lexical", root=self.root, timeout_ms=10000)
        arguments = {"query": "needle token quartz", "index_path": "moving/idx", "top_k": 1, "rerank": False}
        with patch.object(index, "load_index", wraps=index.load_index) as load:
            before = (await self.call(arguments, server=server)).structured_content["results"][0]
            again = (await self.call(arguments, server=server)).structured_content["results"][0]
            self.assertEqual((before, load.call_count), (again, 1))
            (self.root / "moving" / "field.md").write_text(corpus(9), encoding="utf-8")
            index.build(self.bridge, [self.root / "moving" / "field.md"], self.root / "moving" / "idx", chars=200, overlap=60)
            for name in (index.SIDECAR, index.VECTORS):
                path = self.root / "moving" / "idx" / name
                stamp = path.stat()
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
            after = (await self.call(arguments, server=server)).structured_content["results"][0]
        self.assertEqual(load.call_count, 2)
        self.assertGreater(after["start"], before["start"], "the needle moved from paragraph 2 to paragraph 9")

    async def test_without_rerank_orders_by_similarity_and_submits_one_job(self):
        with patch.object(self.bridge, "run", wraps=self.bridge.run) as run:
            body = (await self.call({"query": "needle token quartz", "index_path": "index", "top_k": 4,
                                     "rerank": False})).structured_content
            self.assertEqual(run.call_count, 1)
            self.assertIsNone(body["rerank_model"])
            self.assertTrue(all(hit["rerank_score"] is None for hit in body["results"]))
            similarities = [hit["similarity"] for hit in body["results"]]
            self.assertEqual(similarities, sorted(similarities, reverse=True))
            await self.call({"query": "needle token quartz", "index_path": "index", "top_k": 4})
            self.assertEqual(run.call_count, 3)

    async def test_rerank_candidates_fit_the_request_byte_limit(self):
        # About 1.4 bytes per character: 200 candidates of 1000 characters fit the character budget, not the bytes.
        (self.root / "wide").mkdir()
        (self.root / "wide" / "text.md").write_text(" ".join(f"\u00e9\u00e8\u00ea\u00eb{i:05d}" for i in range(20000)),
                                                    encoding="utf-8")
        index.build(self.bridge, [self.root / "wide" / "text.md"], self.root / "wide" / "idx")
        result = await self.call({"query": "\u00e9\u00e8\u00ea\u00eb00042", "index_path": "wide/idx", "top_k": 50})
        self.assertFalse(result.is_error, result.content)
        body = result.structured_content
        self.assertEqual(len(body["results"]), 50)
        self.assertLess(body["considered"], 200, "candidates are trimmed to the encoded request limit")

    async def test_top_k_above_the_chunk_count_returns_every_chunk(self):
        for rerank in (True, False):
            with self.subTest(rerank=rerank):
                body = (await self.call({"query": "gamma", "index_path": "small", "top_k": 5,
                                         "rerank": rerank})).structured_content
                self.assertEqual((body["chunks"], body["considered"], len(body["results"])), (3, 3, 3))

    async def test_progress_names_each_phase(self):
        messages = []

        async def listen(progress, total, message):
            messages.append(message)

        result = await self.call({"query": "needle token quartz", "index_path": "index"}, progress_callback=listen)
        self.assertFalse(result.is_error, result.content)
        self.assertTrue(any(message.startswith("embed ") for message in messages), messages)
        self.assertTrue(any(message.startswith("rerank ") for message in messages), messages)
