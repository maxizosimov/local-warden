#!/usr/bin/env bash
# MCP server (stdio) for Claude Desktop / Claude Code / Cursor. Start the hub with ./run.sh first.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt >&2
exec python -m warden.mcp_server
