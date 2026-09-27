#!/bin/sh
# Launch the stdio MCP server the way an MCP host does, for hosts and harnesses that take a single command.
# Needs BRIDGE_TOKEN; BRIDGE_MCP_DATA, if set, must be an absolute path. Everything but JSON-RPC goes to stderr.
exec docker compose -f "$(dirname "$0")/../docker/compose.yaml" run --rm -i -T mcp
