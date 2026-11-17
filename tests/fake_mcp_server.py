"""A tiny stdio MCP server for tests: tools echo, big and hang."""

import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Echo text", "inputSchema": {
        "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
        "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "big", "description": "Lots of text"},
    {"name": "hang", "description": "Never answers"},
    {"name": "wipe", "description": "Deletes\nthings", "annotations": {"destructiveHint": True}},
    {"name": "peek", "annotations": {"destructiveHint": True, "readOnlyHint": True}},
]

print("a log line on stdout", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, params = msg["method"], msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call" and params["name"] == "echo":
        result = {"content": [{"type": "text", "text": "echo: " + params["arguments"]["text"]},
                              {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]}
    elif method == "tools/call" and params["name"] == "big":
        result = {"content": [{"type": "text", "text": "x" * 50000}], "isError": True}
    elif method == "tools/call" and params["name"] == "hang":
        time.sleep(30)
        continue
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
