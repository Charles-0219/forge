"""Local stdio stand-in for MCP tests. It never opens a socket."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    print("mcp-stderr", file=sys.stderr, flush=True)
    if os.environ.get("SENTINEL_SECRET"):
        print("leaked-secret", file=sys.stderr, flush=True)
    mode = os.environ.get("FAKE_MCP_MODE", "ok")
    if mode == "disconnect":
        return
    for line in sys.stdin:
        if mode == "bad":
            sys.stdout.write("not-json\n")
            sys.stdout.flush()
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return
        method = message.get("method")
        request_id = message.get("id")
        if mode == "timeout" and method == "tools/call":
            time.sleep(30)
            return
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            text = str((message.get("params") or {}).get("arguments", {}).get("text") or "")
            result = {"content": [{"type": "text", "text": text}]}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
