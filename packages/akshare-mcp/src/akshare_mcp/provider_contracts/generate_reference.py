"""Generate read-only provider contract reference assets.

By default this module prints JSON to stdout. Passing ``--output-dir`` writes
reference files intentionally requested by the caller; it never rewrites
business code or the registry.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .fetcher import ProviderRegistryMap
from .registry import provider_tool_contracts


def build_tool_reference() -> list[dict[str, Any]]:
    contracts = provider_tool_contracts()
    return [contracts[name] for name in sorted(contracts)]


def build_provider_capability_reference() -> list[dict[str, Any]]:
    return ProviderRegistryMap(provider_tool_contracts()).provider_capabilities()


def build_contract_coverage_report(*, known_tools: list[str] | None = None) -> dict[str, Any]:
    return ProviderRegistryMap(provider_tool_contracts()).coverage_report(known_tools=known_tools)


def build_reference_bundle() -> dict[str, Any]:
    return {
        "tool_reference": build_tool_reference(),
        "provider_capabilities": build_provider_capability_reference(),
        "coverage_report": build_contract_coverage_report(),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate AIASK provider contract reference JSON.")
    parser.add_argument("--kind", choices=["all", "tools", "providers", "coverage"], default="all")
    parser.add_argument("--output-dir", default="", help="Optional directory for generated JSON assets.")
    args = parser.parse_args(argv)

    payloads = {
        "tools": build_tool_reference(),
        "providers": build_provider_capability_reference(),
        "coverage": build_contract_coverage_report(),
    }
    if args.kind == "all":
        selected: Any = {
            "tool_reference": payloads["tools"],
            "provider_capabilities": payloads["providers"],
            "coverage_report": payloads["coverage"],
        }
    else:
        selected = payloads[args.kind]

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if args.kind == "all":
            _write_json(output_dir / "tool_reference.json", payloads["tools"])
            _write_json(output_dir / "provider_capabilities.json", payloads["providers"])
            _write_json(output_dir / "contract_coverage_report.json", payloads["coverage"])
        else:
            _write_json(output_dir / f"{args.kind}.json", selected)
    print(json.dumps(selected, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
