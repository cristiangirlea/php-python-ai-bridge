"""The MCP server cannot import the worker, so this is what keeps its model table in step with the worker's."""

import os
import subprocess
import sys
import unittest

from ai_bridge.jobs import JOB_ID
from ai_bridge.tasks import MODEL_NAMES
from bridge_mcp import protocol
from bridge_mcp.tools import BACKENDS


class ModelNameTests(unittest.TestCase):
    def test_job_id_pattern_matches_the_worker(self):
        self.assertEqual(protocol.JOB_ID.pattern, JOB_ID)

    def test_selectable_rerankers_are_named_and_described_together(self):
        code = """
import asyncio
from mcp import Client
from ai_bridge import tasks
from bridge_mcp import tools

name, explanation = tools.BACKENDS['onnx']['rerank']
assert name == tasks.MODEL_NAMES['onnx']['rerank']
server = tools.build_server(None, 'onnx', None, 30000)

async def check():
    async with Client(server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == 'bridge_rerank')
        assert tool.description.split('Backend: ', 1)[1].startswith(name + ':')

asyncio.run(check())
"""
        for choice in ("tinybert", "minilm-l6", "bge-base"):
            with self.subTest(choice=choice):
                subprocess.run([sys.executable, "-c", code],
                               env={**os.environ, "BRIDGE_RERANK_MODEL": choice}, check=True, timeout=30)
                wrong = code.replace("server = tools.build_server", "tools.BACKENDS['onnx']['rerank'] = "
                                     "('wrong-selection', explanation)\nserver = tools.build_server")
                result = subprocess.run([sys.executable, "-c", wrong],
                                        env={**os.environ, "BRIDGE_RERANK_MODEL": choice},
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AssertionError", result.stderr)

    def test_invalid_reranker_stops_import_without_echoing_the_value(self):
        for backend in ("lexical", "onnx"):
            with self.subTest(backend=backend):
                result = subprocess.run([sys.executable, "-c", "from bridge_mcp import tools; "
                                         f"tools.build_server(None, {backend!r}, None, 30000)"],
                                        env={**os.environ, "BRIDGE_RERANK_MODEL": "private-invalid-selection"},
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), "BRIDGE_RERANK_MODEL must be tinybert, minilm-l6 or bge-base")
                self.assertNotIn("private-invalid-selection", result.stdout + result.stderr)

    def test_every_backend_names_exactly_what_the_worker_reports(self):
        self.assertEqual(set(BACKENDS), set(MODEL_NAMES))
        for backend, tasks in MODEL_NAMES.items():
            with self.subTest(backend=backend):
                self.assertEqual({task: name for task, (name, _) in BACKENDS[backend].items()}, tasks)
