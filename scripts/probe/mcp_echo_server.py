from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import IO

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "shim-probe", "version": "0"}
TOOL = {
    "name": "probe_echo",
    "description": (
        "Return the given customer record unchanged, or the text of the named "
        "page in the working folder."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "customer_email": {"type": "string"},
            "note": {"type": "string"},
            "page": {"type": "string"},
        },
        "required": ["customer_email"],
        "additionalProperties": False,
    },
}


def respond(request: dict[str, object]) -> dict[str, object] | None:
    method = request.get("method")
    identifier = request.get("id")
    if identifier is None:
        return None
    if method == "initialize":
        return _result(
            identifier,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _result(identifier, {})
    if method == "tools/list":
        return _result(identifier, {"tools": [TOOL]})
    if method == "tools/call":
        return _result(identifier, _call(request.get("params")))
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": -32601, "message": "method not found"},
    }


def _result(identifier: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _call(params: object) -> dict[str, object]:
    arguments: object = params.get("arguments", {}) if isinstance(params, dict) else {}
    page = arguments.get("page") if isinstance(arguments, dict) else None
    if isinstance(page, str):
        # A fetch-style server hands back a whole page as one text block.
        text = Path(Path(page).name).read_text(encoding="utf-8")
    else:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def serve(source: IO[str], sink: IO[str]) -> None:
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        response = respond(request)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
