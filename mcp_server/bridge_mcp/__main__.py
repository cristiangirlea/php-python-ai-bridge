"""Start the stdio MCP server after proving the worker answers. Nothing is ever written to stdout by hand."""

import os
import sys
from pathlib import Path

from .protocol import Bridge, BridgeError
from .tools import build_server


def main() -> None:
    root = os.environ.get("BRIDGE_MCP_ROOT")
    try:
        bridge = Bridge(os.environ.get("BRIDGE_URL", "http://127.0.0.1:8090"), os.environ.get("BRIDGE_TOKEN", ""))
        # Descriptions state the backend, so the server refuses to start rather than describe one it cannot see.
        backend = bridge.health()["backend"]
        server = build_server(bridge, backend, Path(root) if root else None,
                              int(os.environ.get("BRIDGE_MCP_TIMEOUT_MS", "60000")))
    except (ValueError, BridgeError) as error:
        print(f"bridge_mcp: {error}", file=sys.stderr)
        sys.exit(2)
    print(f"bridge_mcp: worker backend {backend!r}, root {root or 'not configured'}", file=sys.stderr)
    server.run()


if __name__ == "__main__":
    main()
