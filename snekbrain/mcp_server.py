"""SnekBrain AI hook: a tiny MCP server (stdio) so an AI assistant on this PC can watch and steer training.

It only works while "Let my own AI assistant connect" is on in SnekBrain's settings. It talks to the
SnekBrain app on 127.0.0.1 with the key from ai_hook.json, and it can only read stats and start,
pause or switch training: it can't delete, export or change settings. Uses the Python standard library only.
"""
from __future__ import annotations

import json
import sys
import urllib.request

from . import __version__, brains

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "get_status", "description": "What SnekBrain is doing now: training or paused, generation, steps, speed, level.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_brains", "description": "All saved brains with their generation and best average length.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_progress", "description": "Per-generation stats (average length, score, survival, kills) for a brain.",
     "inputSchema": {"type": "object", "properties": {"brain": {"type": "string", "description": "brain id; default the active one"},
                                                      "last": {"type": "integer", "description": "only the last N generations", "default": 30}}}},
    {"name": "get_records", "description": "Hall of Fame records and recent events (level ups, records) for a brain.",
     "inputSchema": {"type": "object", "properties": {"brain": {"type": "string"}}}},
    {"name": "start_training", "description": "Start (or resume) training the active brain, or the brain given.",
     "inputSchema": {"type": "object", "properties": {"brain": {"type": "string"}}}},
    {"name": "pause_training", "description": "Pause training. Everything is saved first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "set_mode", "description": "Normal (background) or turbo (uses more CPU to learn faster).",
     "inputSchema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["normal", "turbo"]}}, "required": ["mode"]}},
]


def _hook() -> dict:
    from .settings import read_json
    h = read_json(brains.data_dir() / "ai_hook.json", {})
    if not h.get("key"):
        raise RuntimeError("The SnekBrain AI hook is off. Turn on 'Let my own AI assistant connect' in SnekBrain's settings.")
    return h


def _api(method: str, path: str, body: dict | None = None):
    h = _hook()
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:{int(h['port'])}/api/{path}", data=data, method=method,
                                 headers={"X-SL-AI-Key": h["key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - local app only
            return json.loads(r.read() or b"{}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach SnekBrain (is it running?): {e}") from None


def _active(args: dict) -> str:
    return args.get("brain") or _api("GET", "status")["active"]


def call(name: str, args: dict):
    if name == "get_status":
        return _api("GET", "status")
    if name == "list_brains":
        return _api("GET", "brains")
    if name == "get_progress":
        rows = _api("GET", f"brain/{_active(args)}/progress")["rows"]
        return rows[-max(1, min(500, int(args.get("last", 30)))):]
    if name == "get_records":
        b = _active(args)
        return {"records": _api("GET", f"brain/{b}/hall")["records"][-50:], "events": _api("GET", f"brain/{b}/events")["events"]}
    if name == "start_training":
        return _api("POST", "start", {"brain": args.get("brain")} if args.get("brain") else {})
    if name == "pause_training":
        return _api("POST", "pause")
    if name == "set_mode":
        if args.get("mode") not in ("normal", "turbo"):
            raise ValueError("mode must be normal or turbo")
        return _api("POST", "mode", {"mode": args["mode"]})
    raise ValueError(f"unknown tool {name}")


def handle(msg: dict):
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "snekbrain", "version": __version__}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = msg.get("params", {})
        try:
            out = call(p.get("name"), p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(out, indent=1)}], "isError": False}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    if method == "ping":
        return {}
    if mid is None:
        return None  # a notification
    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        try:
            res = handle(msg)
            if mid is None:
                continue
            out = {"jsonrpc": "2.0", "id": mid, "result": res}
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {e}"}}
        except Exception as e:  # noqa: BLE001
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
