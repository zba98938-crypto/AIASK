#!/usr/bin/env python3
"""Call every tool on the vendor data services and record what comes back.

The four vendor servers — East money, 通达信, 同花顺, QMT — were verified to *start*
and to *advertise* their tools. Neither says anything about whether a tool returns
data, and this project has been caught by exactly that gap before: `akshare` was
imported by 26 files, declared by none, and every call fell into an `except
ImportError` branch that returned an empty list — so nothing failed loudly and nobody
noticed for as long as it lasted.

So this calls each tool, with arguments built from its own ``inputSchema``, and
records one of:

    OK          returned content that is not empty
    EMPTY       returned successfully with nothing in it

A tool whose response is well formed is OK even when the rows are zero —
"the provider answered and there were none" is not a broken tool. EMPTY is
reserved for a success envelope that carries no payload at all.
    FAIL        raised or returned an error
    UNTESTABLE  required arguments could not be constructed from the schema

An empty answer is reported separately from a failure on purpose. "The provider
answered and had nothing" and "the provider could not be asked" are different facts,
and a check that merges them cannot tell a quiet market from a dead channel.

Usage::

    python scripts/check_data_service_channels.py
    python scripts/check_data_service_channels.py --only aiask-eastmoney
    python scripts/check_data_service_channels.py --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / ".claude" / "mcp-servers-recommended.json"
PYTHON = sys.executable
CALL_TIMEOUT_SECONDS = 45.0
LIST_TIMEOUT_SECONDS = 90.0

#: Values for required arguments whose meaning is inferable from the name. Anything
#: not listed and not inferable makes the tool UNTESTABLE rather than guessed.
CODE_FIELDS = ("code", "stock_code", "ts_code", "symbol", "ticker", "secu_code")
DATE_FIELDS = ("date", "trade_date", "start_date", "end_date", "beg", "period_date")
#: Dispatchers take an action; `help` is the one every one of them accepts, and it
#: reveals the real list without performing anything.
ACTION_FIELD = "action"


def _services() -> dict[str, str]:
    manifest = json.loads(MANIFEST.read_text())
    return {name: cfg["args"][-1] for name, cfg in (manifest.get("mcpServers") or {}).items()}


class Channel:
    """One MCP server over stdio, spoken to directly."""

    def __init__(self, module: str):
        self.module = module
        self.proc = subprocess.Popen(
            [PYTHON, "scripts/run_mcp_stdio.py", module],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(REPO_ROOT),
        )
        self._id = 0

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _await(self, request_id: int, timeout: float) -> Optional[dict[str, Any]]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except Exception:
                continue
            if message.get("id") == request_id:
                return message
        return None

    def handshake(self) -> bool:
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "channel-check", "version": "1"}}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self._await(1, 30) is not None

    def tools(self) -> list[dict[str, Any]]:
        self._id = 2
        self._send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        reply = self._await(2, LIST_TIMEOUT_SECONDS)
        return ((reply or {}).get("result") or {}).get("tools") or []

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, str]:
        """Return ``(verdict, detail)`` for one tool call."""

        self._id += 1
        request_id = self._id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        reply = self._await(request_id, CALL_TIMEOUT_SECONDS)
        if reply is None:
            return "FAIL", "no reply"
        if "error" in reply:
            return "FAIL", str((reply.get("error") or {}).get("message"))[:110]
        text = "".join(c.get("text", "") for c in ((reply.get("result") or {}).get("content") or []))
        if not text.strip():
            return "FAIL", "no content"
        if "Error executing tool" in text or '"success": false' in text or '"success":false' in text:
            return "FAIL", text.strip().replace("\n", " ")[:110]
        try:
            payload = json.loads(text)
        except Exception:
            return "OK", f"{len(text)} chars (not json)"
        # Whether the *tool* works and whether its *content* is empty are different
        # questions, and conflating them produced a false alarm here: the strategy
        # factory answers with `{object, contract_version, ...}` rather than
        # `{success, data}`, so keying emptiness on a `data` field called three
        # working tools empty. A tool that returned a well-formed response works;
        # whether that response holds rows is reported alongside.
        if isinstance(payload, dict) and "success" in payload:
            data = payload.get("data")
            if data is None:
                return "EMPTY", "success but data=None"
            if isinstance(data, (list, dict)) and not data:
                return "EMPTY", f"success but data is empty {type(data).__name__}"
            return "OK", f"data={len(data) if isinstance(data, (list, dict)) else str(data)[:36]}"
        return "OK", f"{type(payload).__name__} envelope"

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _arguments_for(schema: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
    """Build the smallest call the schema will accept, or explain why it cannot be."""

    properties = dict(schema.get("properties") or {})
    required = list(schema.get("required") or [])
    # Optional fields whose *name* identifies them are filled too. A tool that
    # declares `code` optional and then answers "需要提供股票代码" is a schema that
    # does not match its implementation — a real defect worth surfacing — but a
    # checker that only fills `required` cannot tell that apart from its own gap,
    # and reported eight working tools as failing.
    candidates = list(required) + [
        name for name in properties
        if name not in required and (name in CODE_FIELDS or name in DATE_FIELDS)
    ]
    arguments: dict[str, Any] = {}
    for field in candidates:
        spec = dict(properties.get(field) or {})
        kind = spec.get("type")
        if field == ACTION_FIELD or field in (spec.get("enum") or [])[:1]:
            arguments[field] = (spec.get("enum") or ["help"])[0]
        elif field in CODE_FIELDS:
            arguments[field] = "600519"
        elif field in DATE_FIELDS:
            arguments[field] = time.strftime("%Y-%m-%d")
        elif kind == "integer":
            arguments[field] = 1
        elif kind == "number":
            arguments[field] = 1.0
        elif kind == "boolean":
            arguments[field] = False
        elif kind == "array":
            arguments[field] = []
        elif spec.get("enum"):
            arguments[field] = spec["enum"][0]
        else:
            return None, f"required field {field!r} has no inferable value"
    return arguments, ""


def check_service(name: str, module: str, only_tools: Optional[list[str]] = None) -> dict[str, Any]:
    channel = Channel(module)
    result: dict[str, Any] = {"service": name, "module": module, "tools": {}}
    try:
        if not channel.handshake():
            result["error"] = "handshake failed"
            return result
        tools = channel.tools()
        if not tools:
            result["error"] = "advertised no tools"
            return result
        for tool in tools:
            tool_name = tool["name"]
            if only_tools and tool_name not in only_tools:
                continue
            arguments, problem = _arguments_for(tool.get("inputSchema") or {})
            if arguments is None:
                result["tools"][tool_name] = {"verdict": "UNTESTABLE", "detail": problem}
                continue
            verdict, detail = channel.call(tool_name, arguments)
            result["tools"][tool_name] = {"verdict": verdict, "detail": detail}
    finally:
        channel.close()
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", default="", help="comma-separated service names")
    parser.add_argument("--tools", default="", help="comma-separated tool names to restrict to")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    only_tools = [t.strip() for t in args.tools.split(",") if t.strip()] or None
    services = {k: v for k, v in _services().items() if not wanted or k in wanted}

    reports = [check_service(name, module, only_tools) for name, module in sorted(services.items())]

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0

    totals: dict[str, int] = {}
    for report in reports:
        print(f"\n[{report['service']}]  module={report['module']}")
        if report.get("error"):
            print(f"    !! {report['error']}")
            totals["FAIL"] = totals.get("FAIL", 0) + 1
            continue
        counts: dict[str, int] = {}
        for tool_name, entry in sorted(report["tools"].items()):
            counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
            totals[entry["verdict"]] = totals.get(entry["verdict"], 0) + 1
            if entry["verdict"] != "OK":
                print(f"    {entry['verdict']:10s} {tool_name:34s} {entry['detail'][:60]}")
        print(f"    -> {counts}")

    print(f"\n=== 合计 === {totals}")
    failing = totals.get("FAIL", 0) + totals.get("EMPTY", 0)
    if failing == 0:
        print("every advertised tool on every service returned content")
        return 0
    print("上列 FAIL/EMPTY 即「宣告了但不能用」的工具；EMPTY 与 FAIL 分开计数，"
          "因为「问了、没有」与「问不到」是两件事")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
