from __future__ import annotations


def _bind_server_helpers() -> None:
    """Bind server launch helpers lazily to keep server.py as the public shim."""
    from . import server as server_helpers

    module_globals = globals()
    for name, value in vars(server_helpers).items():
        if name.startswith("__") or name == "main":
            continue
        module_globals[name] = value

def main(argv: list[str] | None = None) -> None:
    _bind_server_helpers()
    _load_local_env_file()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "tui":
        from .tui import run as run_tui

        run_tui()
        return
    if args_list and args_list[0] == "gateway":
        command = args_list[1] if len(args_list) > 1 else "status"
        gateway = GatewayRuntime()
        if command == "status":
            print(json.dumps(gateway.status(), ensure_ascii=False, indent=2, sort_keys=True))
            return
        if command == "setup":
            store = GatewayConfigStore()
            for platform in ("feishu", "dingtalk", "wecom", "weixin", "email", "webhook", "api_server", "local"):
                store.save_platform(platform, {"enabled": True})
            print(json.dumps(gateway.status(), ensure_ascii=False, indent=2, sort_keys=True))
            return
        if command in {"start", "stop"}:
            state = "running" if command == "start" else "stopped"
            print(json.dumps({"object": "aiask.gateway_command", "command": command, "status": gateway.write_runtime_status(state=state)}, ensure_ascii=False))
            return
        raise SystemExit(f"unsupported gateway command: {command}")
    if args_list and args_list[0] == "doctor":
        full_native = "--full-hermes-native" in args_list
        if full_native:
            temp_store = AgentSessionStore()
            temp_runtime = AgentRuntime(
                session_store=temp_store,
                tool_registry=build_default_tool_registry(
                    session_store=temp_store,
                    policy_engine=ToolPolicyEngine(ToolPolicy(GENERAL_FULL_TOOLSET, True, (os.getcwd(),))),
                ),
            )
            gateway = GatewayRuntime(messages=GatewayMessageStore(temp_runtime.session_store.path))
            rl = RLAtroposManager(temp_runtime.session_store.path)
            parity = parity_summary(temp_runtime.tool_registry.names(), env=dict(os.environ), gateway_adapters=ADAPTERS.keys())
            payload = {
                "object": "aiask.doctor",
                "mode": "full-hermes-native",
                "embedded_vendor_runtime": False,
                "dependencies": {
                    "docker": bool(shutil.which("docker") or importlib.util.find_spec("docker")),
                    "ssh": bool(shutil.which("ssh") or importlib.util.find_spec("asyncssh")),
                    "textual": bool(importlib.util.find_spec("textual")),
                    "atroposlib": bool(importlib.util.find_spec("atroposlib")),
                    "tinker_atropos": bool(importlib.util.find_spec("tinker_atropos")),
                },
                "terminal_backends": list_backends(),
                "gateway": gateway.status(),
                "mcp": MCPAggregator().registration_diagnostics(),
                "rl": rl.readiness(),
                "plugins": NativePluginManager().readiness(),
                "feature_mapping": parity.get("feature_mapping", []),
                "missing_features": parity.get("missing_features", []),
                "implemented_features_count": parity.get("implemented_features_count", 0),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return
        raise SystemExit("unsupported doctor command; use: aiask-agent doctor --full-hermes-native")
    parser = argparse.ArgumentParser(description="Run the AIASK Agent HTTP server.")
    parser.add_argument("--host", default=os.getenv("AIASK_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIASK_AGENT_PORT", "8765")))
    parser.add_argument("--legacy-http", action="store_true", help="Run the compatibility ThreadingHTTPServer instead of ASGI.")
    args = parser.parse_args(args_list)

    if not _is_loopback(args.host) and not os.getenv("AIASK_AGENT_API_TOKEN"):
        raise SystemExit("AIASK_AGENT_API_TOKEN is required when binding aiask-agent to a non-loopback host")

    if args.legacy_http:
        server = build_server(args.host, args.port)
        print(f"aiask-agent listening on http://{args.host}:{args.port} (legacy-http)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    import uvicorn

    print(f"aiask-agent listening on http://{args.host}:{args.port} (fastapi-asgi)", flush=True)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=os.getenv("AIASK_AGENT_UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
