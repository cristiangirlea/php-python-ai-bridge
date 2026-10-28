"""The MCP server cannot import the worker, so this is what keeps its model table in step with the worker's."""

import os
import subprocess
import sys
import unittest

from ai_bridge.tasks import MODEL_NAMES
from bridge_mcp.tools import BACKENDS


class ModelNameTests(unittest.TestCase):
    def test_selectable_rerankers_are_named_and_described_together(self):
        for choice in ("tinybert", "minilm-l6", "bge-base"):
            with self.subTest(choice=choice):
                subprocess.run([sys.executable, "-c", "from ai_bridge import tasks; from bridge_mcp import tools; "
                                "name, description = tools.BACKENDS['onnx']['rerank']; "
                                "assert name == tasks.MODEL_NAMES['onnx']['rerank']; assert name in description"],
                               env={**os.environ, "BRIDGE_RERANK_MODEL": choice}, check=True, timeout=30)

    def test_every_backend_names_exactly_what_the_worker_reports(self):
        self.assertEqual(set(BACKENDS), set(MODEL_NAMES))
        for backend, tasks in MODEL_NAMES.items():
            with self.subTest(backend=backend):
                self.assertEqual({task: name for task, (name, _) in BACKENDS[backend].items()}, tasks)
