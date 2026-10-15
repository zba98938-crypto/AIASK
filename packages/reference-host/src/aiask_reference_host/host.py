"""Minimal reference host: the smallest thing that satisfies the contract.

Phase 5 of the extraction plan. Its purpose is to prove the Host Adapter Contract
is implementable without Hermes — a conformance fixture and a starting point for a
second host, not a product.

Scope, stated plainly. It provides:

* skill-bundle discovery from a directory,
* MCP registration into a local JSON registry,
* capability discovery that reports gaps instead of hiding them,
* ``aiask`` CLI invocation with live trading forced off,
* side-effect classification and structured error rendering.

It deliberately provides **no** conversation loop, no gateway, no platform
matrix, no desktop, no model routing, and no Hermes parity surface. A host that
needs those builds them; the contract does not require them.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from strategy_factory.api.runtime import (
    HOST_ADAPTER_PROTOCOL_VERSION,
    CapabilityReport,
    McpManifest,
    SkillBundle,
    assert_policy_not_loosened,
    default_side_effect,
)

logger = logging.getLogger(__name__)


#: Capabilities this host can offer once its dependencies resolve.
REFERENCE_CAPABILITIES: tuple[str, ...] = (
    "factory.status",
    "factory.preflight",
    "factory.evidence",
    "factory.schedule_template",
    "factory.once",
    "factory.cycle",
)


class ReferenceHost:
    """A host adapter with no framework behind it.

    State lives in ``state_dir`` as plain JSON so conformance can be inspected
    without running anything.
    """

    name = "aiask-reference-host"

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        state_dir: Path | str | None = None,
        skills_dir: Path | str | None = None,
        python: str | None = None,
    ):
        explicit_root = str(project_root or os.getenv("AIASK_PROJECT_ROOT") or "").strip()
        self.project_root = (
            Path(explicit_root).expanduser().resolve()
            if explicit_root
            else Path(__file__).resolve().parents[4]
        )
        self.state_dir = Path(
            state_dir or (Path.home() / ".aiask" / "reference-host")
        ).expanduser()
        self.skills_dir = Path(skills_dir) if skills_dir else self.project_root / "skills" / "finance"
        self.python = python or sys.executable

    # --- state ----------------------------------------------------------------

    @property
    def _registry_path(self) -> Path:
        return self.state_dir / "mcp_registry.json"

    @property
    def _bundles_path(self) -> Path:
        return self.state_dir / "skill_bundles.json"

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("reference host: unreadable state at %s: %s", path, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # --- contract -------------------------------------------------------------

    def install_skill_bundle(self, bundle: SkillBundle) -> dict[str, Any]:
        """Record a bundle after verifying its skills exist on disk. Idempotent."""
        present: list[str] = []
        missing: list[str] = []
        for skill in bundle.skills:
            if (self.skills_dir / skill / "SKILL.md").is_file():
                present.append(skill)
            else:
                missing.append(skill)

        installed = self._read_json(self._bundles_path)
        installed[bundle.name] = {
            **bundle.to_dict(),
            "installed_skills": present,
            "missing_skills": missing,
        }
        self._write_json(self._bundles_path, installed)

        return {
            "bundle": bundle.name,
            "installed": len(present),
            "missing": missing,
            # A bundle with any missing skill is not usable; say so rather than
            # reporting a partial install as success.
            "ok": not missing and bool(present),
        }

    def register_mcp(self, manifest: McpManifest) -> dict[str, Any]:
        """Record an MCP server. Idempotent by name."""
        if manifest.transport != "stdio":
            return {
                "name": manifest.name,
                "ok": False,
                "error": f"unsupported_transport:{manifest.transport}",
            }

        registry = self._read_json(self._registry_path)
        registry[manifest.name] = manifest.to_dict()
        self._write_json(self._registry_path, registry)
        return {"name": manifest.name, "ok": True, "registered": len(registry)}

    def discover_capabilities(self) -> CapabilityReport:
        """Report what is usable, naming every gap found."""
        degraded: list[str] = []

        skills = tuple(
            sorted(
                path.parent.name
                for path in self.skills_dir.glob("*/SKILL.md")
            )
        )
        if not skills:
            degraded.append(f"no_skills_found:{self.skills_dir}")

        servers = tuple(sorted(self._read_json(self._registry_path)))
        if not servers:
            degraded.append("no_mcp_registered")

        cli_available = self._cli_available()
        if not cli_available:
            degraded.append("aiask_cli_unavailable")

        return CapabilityReport(
            host=self.name,
            protocol_version=HOST_ADAPTER_PROTOCOL_VERSION,
            capabilities=REFERENCE_CAPABILITIES if cli_available else (),
            mcp_servers=servers,
            skills=skills,
            degraded_reasons=tuple(degraded),
        )

    def invoke_aiask_cli(self, argv: list[str], *, timeout: float = 7200.0) -> dict[str, Any]:
        """Run ``aiask ...`` and return its governed envelope."""
        command = [self.python, "-m", "aiask_agent.cli", *argv]
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._child_env(),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timeout_after_{int(timeout)}s", "command": command}
        except OSError as exc:
            return {"ok": False, "error": f"spawn_failed:{exc}", "command": command}

        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(completed.stdout)
            envelope = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            envelope = None

        status = str((envelope or {}).get("status") or "")
        return {
            # Only a clean success is ok; degraded and partial are not.
            "ok": status == "success" if envelope else completed.returncode == 0,
            "returncode": completed.returncode,
            "envelope": envelope,
            "command": command,
            "stderr_tail": (completed.stderr or "")[-2000:],
        }

    def map_side_effect_policy(self, operation: str) -> str:
        """Baseline classification; unknown operations are treated as mutating."""
        resolved = default_side_effect(operation)
        # Guard against a future edit weakening this below the shared baseline.
        assert_policy_not_loosened(operation, resolved)
        return resolved

    def render_structured_error(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Present a failure or degradation without laundering it."""
        payload = dict(envelope or {})
        status = str(payload.get("status") or "unknown")
        degraded = list(payload.get("degraded_reasons") or [])
        errors = list(payload.get("errors") or [])

        if errors:
            headline = errors[0]
        elif degraded:
            headline = f"completed with degradation: {degraded[0]}"
        else:
            headline = f"status={status}"

        return {
            "host": self.name,
            "status": status,
            "headline": headline,
            "errors": errors,
            "degraded_reasons": degraded,
            "run_id": payload.get("run_id"),
            # Never true for anything but a clean success.
            "clean": status == "success" and not errors and not degraded,
        }

    # --- helpers --------------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["LIVE_TRADING_ENABLED"] = "0"
        env.setdefault("AIASK_PROJECT_ROOT", str(self.project_root))
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _cli_available(self) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("aiask_agent.cli") is not None
        except Exception as _swallowed:
            logger.warning("packages/reference-host/src/aiask_reference_host/host.py:263: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return False

    def default_skill_bundle(self) -> SkillBundle:
        """The bundle shipped with the product, read from disk."""
        skills = tuple(
            sorted(path.parent.name for path in self.skills_dir.glob("*/SKILL.md"))
        )
        return SkillBundle(
            name="aiask-finance",
            version="1.0.0",
            skills=skills,
            required_mcp=("aiask-factories",),
        )

    def default_mcp_manifest(self) -> McpManifest:
        """The read-only factory ledger MCP."""
        launcher = self.project_root / "scripts" / "run_mcp_stdio.py"
        return McpManifest(
            name="aiask-factories",
            command=(self.python, str(launcher), "strategy_factory.mcp_server"),
            description="Read-only AIASK four-factory run ledger and status.",
            read_only=True,
            env={"AIASK_PROJECT_ROOT": str(self.project_root)},
        )


def build_reference_host(**kwargs: Any) -> ReferenceHost:
    return ReferenceHost(**kwargs)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="aiask-reference-host")
    parser.add_argument("--project-root", default="")
    args = parser.parse_args(argv)
    host = ReferenceHost(project_root=args.project_root or None)
    print(json.dumps(host.discover_capabilities().to_dict(), ensure_ascii=False, indent=2))
    return 0


__all__ = ["REFERENCE_CAPABILITIES", "ReferenceHost", "build_reference_host", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
