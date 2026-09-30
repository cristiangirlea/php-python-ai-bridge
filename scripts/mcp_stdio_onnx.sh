#!/bin/sh
# Launch the ONNX-backed stdio MCP server the way an MCP host does. Needs BRIDGE_TOKEN and the fetched models;
# BRIDGE_MCP_DATA, if set, must be an absolute path. The first start waits for the model worker's wheels.
exec docker compose -f "$(dirname "$0")/../docker/compose.yaml" run --rm -i -T mcp-model
