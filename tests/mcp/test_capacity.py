"""A full worker refuses submissions with HTTP 429 until its finished jobs age out of retention.

The tools wait within their call's deadline instead of handing that refusal to the agent: in the first semantic LLM
run, Sonnet gave up on five searches that a short wait would have answered (docs/testing.md).
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcp import Client

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from bridge_mcp import index
from bridge_mcp.protocol import Bridge
from bridge_mcp.tools import build_server

TOKEN = "test-only-bridge-token-never-use-in-production"
RERANK = {"query": "needle", "documents": ["a needle here", "only hay"]}


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, retention_seconds, capacity=1):
        """A worker with room for one job by default; test tasks let the test fill it."""
        server = BridgeServer(("127.0.0.1", 0), TOKEN,
                              Settings(capacity=capacity, retention_seconds=retention_seconds, test_tasks=True))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(stop)
        self.url = "http://127.0.0.1:%d" % server.server_address[1]
        return Bridge(self.url, TOKEN)

    async def call(self, bridge, timeout_ms, tool="bridge_rerank", arguments=RERANK, root=None):
        server = build_server(bridge, backend="lexical", root=root, timeout_ms=timeout_ms)
        async with Client(server, raise_exceptions=True) as client:
            return await client.call_tool(tool, arguments)

    def indexed_root(self):
        """A small index built by a roomy worker, so the one-slot worker's capacity is the test's alone."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "notes").mkdir()
        (root / "notes" / "a.txt").write_text("The keeper polished the brass lamp every morning.", encoding="utf-8")
        (root / "notes" / "b.txt").write_text("The relief boat brings water every second Thursday.", encoding="utf-8")
        index.build(self.worker(retention_seconds=300, capacity=16), [root / "notes"], root / "index")
        return root

    async def test_a_tool_waits_for_a_full_worker_within_its_deadline(self):
        bridge = self.worker(retention_seconds=0.5)
        # The one slot is held while this runs and for half a second after it ends.
        bridge.submit("test.delay", {"seconds": 0.5}, 5000)
        started = time.monotonic()
        result = await self.call(bridge, timeout_ms=10000)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.structured_content["results"][0]["index"], 0)
        self.assertGreater(time.monotonic() - started, 0.4, "the slot was full, so the tool must have waited")

    async def test_a_worker_full_for_the_whole_call_is_a_clear_refusal(self):
        bridge = self.worker(retention_seconds=300)
        bridge.submit("test.delay", {"seconds": 0}, 5000)  # finished at once, then held for the whole test
        started = time.monotonic()
        result = await self.call(bridge, timeout_ms=3000)
        elapsed = time.monotonic() - started
        self.assertTrue(result.is_error)
        text = result.content[0].text
        self.assertIn("at capacity", text)
        self.assertIn("waited", text)
        # The last retry comes with one second of the three left, so it gives up after about two.
        self.assertGreater(elapsed, 1.8, "it gives up only once the call's time is nearly spent")
        self.assertLess(elapsed, 5)

    async def test_a_search_waits_for_capacity_before_each_of_its_jobs(self):
        root = self.indexed_root()
        bridge = self.worker(retention_seconds=0.5)
        bridge.submit("test.delay", {"seconds": 0}, 5000)  # the embed job waits for this, the rerank for the embed
        started = time.monotonic()
        result = await self.call(bridge, 10000, "bridge_search", {"query": "brass lamp", "index_path": "index"}, root)
        self.assertFalse(result.is_error, result.content)
        self.assertIn("brass", result.structured_content["results"][0]["snippet"])
        self.assertGreater(time.monotonic() - started, 0.8, "two waits of about half a second each")

    async def test_a_search_has_one_budget_for_both_its_jobs(self):
        # Each job holds the slot for 1.5 s after it ends. The embed job waits about 1.5 s of the 3 s call; a fresh
        # budget would let the rerank wait 1.5 s more and succeed, one budget leaves it too little and it refuses.
        root = self.indexed_root()
        bridge = self.worker(retention_seconds=1.5)
        bridge.submit("test.delay", {"seconds": 0}, 5000)
        started = time.monotonic()
        result = await self.call(bridge, 3000, "bridge_search", {"query": "brass lamp", "index_path": "index"}, root)
        self.assertTrue(result.is_error, result.content)
        self.assertIn("of its 3 s", result.content[0].text)
        self.assertLess(time.monotonic() - started, 3.2)

    async def test_other_refusals_are_not_retried(self):
        self.worker(retention_seconds=0.5)
        bridge = Bridge(self.url, "a-token-the-worker-does-not-accept-at-all")
        started = time.monotonic()
        result = await self.call(bridge, timeout_ms=10000)
        self.assertTrue(result.is_error)
        self.assertIn("BRIDGE_TOKEN", result.content[0].text)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
