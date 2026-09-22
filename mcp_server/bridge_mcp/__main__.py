"""Start the stdio MCP server after proving the worker answers. Nothing is ever written to stdout by hand."""

import os
import sys
import time
from pathlib import Path

from .protocol import Bridge, BridgeError
from .tools import build_server


def wait_for_worker(bridge: Bridge, window_s: float) -> dict:
    """Compose starts the worker beside this server with no readiness gate, so a refused connection
    is waited out for a bounded window; a rejected token or a bad contract is not."""
    deadline = time.monotonic() + window_s
    while True:
        try:
            return bridge.health()
        except BridgeError as error:
            if error.code != "transport_error" or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def main() -> None:
    root = os.environ.get("BRIDGE_MCP_ROOT")
    try:
        bridge = Bridge(os.environ.get("BRIDGE_URL", "http://127.0.0.1:8090"), os.environ.get("BRIDGE_TOKEN", ""))
        # Descriptions state the backend, so the server refuses to start rather than describe one it cannot see.
        backend = wait_for_worker(bridge, float(os.environ.get("BRIDGE_MCP_STARTUP_S", "10")))["backend"]
        server = build_server(bridge, backend, Path(root) if root else None,
                              int(os.environ.get("BRIDGE_MCP_TIMEOUT_MS", "60000")))
    except (ValueError, BridgeError) as error:
        print(f"bridge_mcp: {error}", file=sys.stderr)
        sys.exit(2)
    print(f"bridge_mcp: worker backend {backend!r}, root {root or 'not configured'}", file=sys.stderr)
    server.run()


if __name__ == "__main__":
    main()
