"""The MCP server cannot import the worker, so this is what keeps its model table in step with the worker's."""

import unittest

from ai_bridge.tasks import MODEL_NAMES
from bridge_mcp.tools import BACKENDS


class ModelNameTests(unittest.TestCase):
    def test_every_backend_names_exactly_what_the_worker_reports(self):
        self.assertEqual(set(BACKENDS), set(MODEL_NAMES))
        for backend, tasks in MODEL_NAMES.items():
            with self.subTest(backend=backend):
                self.assertEqual({task: name for task, (name, _) in BACKENDS[backend].items()}, tasks)
