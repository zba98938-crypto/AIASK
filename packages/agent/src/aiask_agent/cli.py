from __future__ import annotations

import argparse
import json
import os
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _base_url() -> str:
    return str(os.getenv("AIASK_AGENT_URL") or "http://127.0.0.1:8765").rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = str(os.getenv("AIASK_AGENT_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = _headers()
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{_base_url()}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {detail}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _events_from_response(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        data = value.get("data")
        return [dict(item) for item in data] if isinstance(data, list) else []
    if not isinstance(value, str):
        return []
    events: list[dict[str, Any]] = []
    for chunk in value.split("\n\n"):
        lines = [line for line in chunk.splitlines() if line.startswith("data:")]
        if not lines:
            continue
        raw = "\n".join(line.split("data:", 1)[1].strip() for line in lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _query(params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return f"?{urllib.parse.urlencode(clean)}" if clean else ""


def _cmd_run(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "input": args.prompt,
        "session_id": args.session_id,
        "user_id": args.user_id,
        "mode": args.mode,
    }
    _print_json(_request("POST", "/v1/responses", payload))


def _cmd_artifacts(args: argparse.Namespace) -> None:
    if getattr(args, "export_id", None):
        query = _query({"max_bytes": args.max_bytes})
        payload = _request("GET", f"/v1/artifacts/{urllib.parse.quote(args.export_id)}/content{query}")
        if not isinstance(payload, dict):
            raise SystemExit("artifact content endpoint returned an unexpected payload")
        content = payload.get("content")
        if content is None:
            raise SystemExit("artifact has no readable content")
        mode = "wb" if payload.get("encoding") == "base64" else "w"
        if mode == "wb":
            import base64

            data = base64.b64decode(str(content).encode("ascii"))
            with open(args.to, "wb") as handle:
                handle.write(data)
        else:
            with open(args.to, "w", encoding="utf-8") as handle:
                handle.write(str(content))
        _print_json({"object": "artifact.export", "artifact_id": args.export_id, "path": args.to, "encoding": payload.get("encoding")})
        return
    if args.artifact_id:
        _print_json(_request("GET", f"/v1/artifacts/{urllib.parse.quote(args.artifact_id)}"))
        return
    if args.run_id:
        path = f"/v1/runs/{urllib.parse.quote(args.run_id)}/artifacts{_query({'kind': args.kind, 'limit': args.limit})}"
    elif args.session_id:
        path = f"/v1/sessions/{urllib.parse.quote(args.session_id)}/artifacts{_query({'kind': args.kind, 'limit': args.limit})}"
    else:
        raise SystemExit("artifacts list requires --run or --session; artifacts show requires artifact_id")
    _print_json(_request("GET", path))


def _cmd_sources(args: argparse.Namespace) -> None:
    if args.source_id:
        _print_json(_request("GET", f"/v1/sources/{urllib.parse.quote(args.source_id)}"))
        return
    if args.run_id:
        path = f"/v1/runs/{urllib.parse.quote(args.run_id)}/sources{_query({'source_type': args.source_type, 'limit': args.limit})}"
    elif args.session_id:
        path = f"/v1/sessions/{urllib.parse.quote(args.session_id)}/sources{_query({'source_type': args.source_type, 'limit': args.limit})}"
    else:
        raise SystemExit("sources list requires --run or --session; sources show requires source_id")
    _print_json(_request("GET", path))


def _cmd_events(args: argparse.Namespace) -> None:
    after = int(args.after or 0)
    if not getattr(args, "follow", False):
        path = f"/v1/runs/{urllib.parse.quote(args.run_id)}/events{_query({'after': after})}"
        _print_json(_events_from_response(_request("GET", path)))
        return
    while True:
        path = f"/v1/runs/{urllib.parse.quote(args.run_id)}/events{_query({'after': after})}"
        events = _events_from_response(_request("GET", path))
        for event in events:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
            try:
                after = max(after, int(event.get("id") or 0))
            except (TypeError, ValueError):
                pass
        time.sleep(max(0.2, float(args.interval or 1.0)))


def _cmd_tools(args: argparse.Namespace) -> None:
    _print_json(_request("GET", "/v1/tools"))


def _cmd_data_sources(args: argparse.Namespace) -> None:
    _print_json(_request("GET", "/v1/data/stock-data-sources"))


def _cmd_export_run(args: argparse.Namespace) -> None:
    run_id = urllib.parse.quote(args.run_id)
    bundle = {
        "object": "aiask.run_export",
        "run_id": args.run_id,
        "run": _request("GET", f"/v1/runs/{run_id}"),
        "events": _events_from_response(_request("GET", f"/v1/runs/{run_id}/events")),
        "tool_invocations": _request("GET", f"/v1/runs/{run_id}/tool-invocations{_query({'limit': args.limit})}").get("data", []),
        "artifacts": _request("GET", f"/v1/runs/{run_id}/artifacts{_query({'limit': args.limit})}").get("data", []),
        "sources": _request("GET", f"/v1/runs/{run_id}/sources{_query({'limit': args.limit})}").get("data", []),
        "artifact_contents_included": False,
    }
    with open(args.to, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2, sort_keys=True)
    _print_json({"object": "aiask.run_export.written", "run_id": args.run_id, "path": args.to})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIASK Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a prompt through the Agent HTTP API.")
    run.add_argument("prompt")
    run.add_argument("--session", dest="session_id")
    run.add_argument("--user", dest="user_id")
    run.add_argument("--mode", default="finance_safe")
    run.set_defaults(func=_cmd_run)

    artifacts = sub.add_parser("artifacts", help="List or show durable artifacts.")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_command", required=True)
    artifacts_list = artifacts_sub.add_parser("list")
    artifacts_list.add_argument("--run", dest="run_id")
    artifacts_list.add_argument("--session", dest="session_id")
    artifacts_list.add_argument("--kind")
    artifacts_list.add_argument("--limit", type=int, default=100)
    artifacts_list.set_defaults(func=_cmd_artifacts, artifact_id=None)
    artifacts_show = artifacts_sub.add_parser("show")
    artifacts_show.add_argument("artifact_id")
    artifacts_show.set_defaults(func=_cmd_artifacts, run_id=None, session_id=None, kind=None, limit=100)
    artifacts_export = artifacts_sub.add_parser("export")
    artifacts_export.add_argument("export_id")
    artifacts_export.add_argument("--to", required=True)
    artifacts_export.add_argument("--max-bytes", type=int, default=1048576)
    artifacts_export.set_defaults(func=_cmd_artifacts, artifact_id=None, run_id=None, session_id=None, kind=None, limit=100)

    sources = sub.add_parser("sources", help="List or show source/citation records.")
    sources_sub = sources.add_subparsers(dest="sources_command", required=True)
    sources_list = sources_sub.add_parser("list")
    sources_list.add_argument("--run", dest="run_id")
    sources_list.add_argument("--session", dest="session_id")
    sources_list.add_argument("--source-type", dest="source_type")
    sources_list.add_argument("--limit", type=int, default=100)
    sources_list.set_defaults(func=_cmd_sources, source_id=None)
    sources_show = sources_sub.add_parser("show")
    sources_show.add_argument("source_id")
    sources_show.set_defaults(func=_cmd_sources, run_id=None, session_id=None, source_type=None, limit=100)

    events = sub.add_parser("events", help="Read run events.")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    events_list = events_sub.add_parser("list")
    events_list.add_argument("run_id")
    events_list.add_argument("--after", type=int, default=0)
    events_list.set_defaults(func=_cmd_events, follow=False, interval=1.0)
    events_follow = events_sub.add_parser("follow")
    events_follow.add_argument("run_id")
    events_follow.add_argument("--after", type=int, default=0)
    events_follow.add_argument("--interval", type=float, default=1.0)
    events_follow.set_defaults(func=_cmd_events, follow=True)

    tools = sub.add_parser("tools", help="List Agent tools.")
    tools.set_defaults(func=_cmd_tools)

    data_sources = sub.add_parser("data-sources", help="List configured stock data sources.")
    data_sources.set_defaults(func=_cmd_data_sources)

    export_run = sub.add_parser("export-run", help="Export one run manifest with events, tool invocations, artifacts, and sources.")
    export_run.add_argument("run_id")
    export_run.add_argument("--to", required=True)
    export_run.add_argument("--limit", type=int, default=500)
    export_run.set_defaults(func=_cmd_export_run)

    # The four-factory control plane. Imported lazily so the HTTP-only
    # subcommands above keep working even if strategy-factory is not installed.
    try:
        from .factory_cli import build_factory_parser, build_readiness_parser
    except ImportError:  # pragma: no cover - depends on install extras
        pass
    else:
        build_factory_parser(sub)
        build_readiness_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    # Factory subcommands return a governed envelope; print it and let the
    # envelope decide the exit code so schedulers can distinguish
    # clean success (0) from failure (1) and completed-but-not-clean (2).
    if result is not None and hasattr(result, "to_dict"):
        _print_json(result.to_dict())
        raise SystemExit(result.exit_code())


if __name__ == "__main__":
    main(sys.argv[1:])
