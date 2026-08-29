#!/usr/bin/env python3
"""Install the AIASK Hermes plugin into a named Hermes profile.

This script is a bootstrap bridge for the moment before the plugin is already
enabled. It intentionally does not create an AIASK venv, install AIASK
packages, read secrets, or touch broker/runtime state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    """Return the root of this tree.

    This used to be parents[3], which assumed the old <repo>/integrations/aiask
    nesting. Now that the tree is standalone that walked two levels above the
    repo (to the user's home directory on a typical checkout) and inserted it
    into sys.path. scripts/install_hermes_plugin.py -> <aiask root>.
    """
    return Path(__file__).resolve().parents[1]


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1] / "hermes-plugin" / "aiask-hermes-plugin"


def _load_readiness_module():
    repo_root = _repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    readiness_path = _plugin_root() / "readiness.py"
    spec = importlib.util.spec_from_file_location("aiask_hermes_plugin_bootstrap_readiness", readiness_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load readiness helper from {readiness_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def install_profile(*, profile: str, force: bool = False) -> dict[str, Any]:
    readiness = _load_readiness_module()
    return readiness.install_plugin_to_profile(
        profile_name=profile,
        force=force,
        source_dir=_plugin_root(),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install and enable the AIASK Hermes plugin in an existing Hermes profile.",
    )
    parser.add_argument("--profile", default="aiask-finance", help="Target Hermes profile name")
    parser.add_argument("--force", action="store_true", help="Replace an existing plugin copy")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = install_profile(profile=args.profile, force=bool(args.force))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if payload.get("ok"):
            print(f"Installed AIASK Hermes plugin for profile: {payload.get('profile_name')}")
            print(f"  plugin_dir: {payload.get('plugin_dir')}")
            print(f"  config_path: {payload.get('config_path')}")
            for note in payload.get("notes") or []:
                print(f"- {note}")
        else:
            print(f"AIASK plugin install failed: {payload.get('error')}")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
