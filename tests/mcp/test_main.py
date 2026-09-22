"""The entry point as a host would run it: a subprocess speaking JSON-RPC on stdout and nothing else."""

import json
import os
import queue
import subprocess
import sys
import threading
import unittest

from ai_bridge.jobs import Settings
from ai_bridge.server import BridgeServer
from bridge_mcp.tools import TOOL_NAMES

TOKEN = "test-only-bridge-token-never-use-in-production"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def environment(**overrides):
    return {**os.environ, "BRIDGE_TOKEN": TOKEN, "BRIDGE_MCP_ROOT": "", **overrides}


class EntryPointTests(unittest.TestCase):
    def test_refuses_to_start_when_the_worker_never_answers(self):
        completed = subprocess.run([sys.executable, "-m", "bridge_mcp"], capture_output=True, text=True, timeout=60,
                                   env=environment(BRIDGE_URL="http://127.0.0.1:1", BRIDGE_MCP_STARTUP_S="0.6"))
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("bridge_mcp:", completed.stderr)
        self.assertNotIn(TOKEN, completed.stderr)

    def test_refuses_to_start_on_a_rejected_token_without_waiting(self):
        server = BridgeServer(("127.0.0.1", 0), "another-token-that-the-worker-expects-instead", Settings(capacity=4))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "bridge_mcp"], capture_output=True, text=True, timeout=60,
                env=environment(BRIDGE_URL="http://127.0.0.1:%d" % server.server_address[1], BRIDGE_MCP_STARTUP_S="30"))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("401", completed.stderr)

    def test_speaks_json_rpc_on_stdout_and_nothing_else(self):
        server = BridgeServer(("127.0.0.1", 0), TOKEN, Settings(capacity=4))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        process = subprocess.Popen(
            [sys.executable, "-m", "bridge_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment(BRIDGE_URL="http://127.0.0.1:%d" % server.server_address[1], BRIDGE_MCP_STARTUP_S="10"))
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
        reader.start()
        try:
            process.stdin.write("".join(json.dumps(message) + "\n" for message in (INITIALIZE, INITIALIZED, LIST_TOOLS)))
            process.stdin.flush()
            first, second = json.loads(lines.get(timeout=30)), json.loads(lines.get(timeout=30))
        finally:
            process.stdin.close()
            stderr = process.stderr.read()
            process.wait(timeout=30)
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(first["id"], 1)
        self.assertIn("instructions", first["result"])
        self.assertEqual(second["id"], 2)
        self.assertEqual(sorted(tool["name"] for tool in second["result"]["tools"]), sorted(TOOL_NAMES))
        self.assertIn("worker backend 'lexical'", stderr)
        self.assertNotIn(TOKEN, stderr)
        self.assertEqual(process.returncode, 0, stderr)
