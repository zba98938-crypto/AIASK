from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from aiask_reference_host import ReferenceHost
from strategy_factory.api.runtime import HostAdapter, verify_host_adapter


def test_reference_host_is_standalone_and_conformant(tmp_path) -> None:
    host = ReferenceHost(project_root=tmp_path, state_dir=tmp_path / "state")
    assert isinstance(host, HostAdapter)
    report = verify_host_adapter(host)
    assert report["conforms"] is True
    assert host.default_mcp_manifest().command[-1] == "strategy_factory.mcp_server"


def test_reference_host_imports_without_pulling_in_agent_or_data_provider(tmp_path) -> None:
    """Constructing the host must not drag in the Agent or a data provider.

    This asserts what the package controls — which modules its own import graph
    loads — rather than whether the current interpreter happens to have them
    installed. In a dev environment every package is installed editable, so
    ``find_spec`` always succeeds and the old form of this test could only pass
    while ``aiask_reference_host`` itself was unimportable.
    """
    code = (
        "import json, sys\n"
        "from aiask_reference_host import ReferenceHost\n"
        "host = ReferenceHost(project_root=" + repr(str(tmp_path)) + ")\n"
        "print(json.dumps({\n"
        "  'host': host.name,\n"
        "  'agent_loaded': any(m == 'aiask_agent' or m.startswith('aiask_agent.') for m in sys.modules),\n"
        "  'provider_loaded': any(m == 'akshare_mcp' or m.startswith('akshare_mcp.') for m in sys.modules),\n"
        "}))"
    )
    source_roots = [
        Path(__file__).resolve().parents[1] / "src",
        Path(__file__).resolve().parents[2] / "strategy-factory" / "src",
        Path(__file__).resolve().parents[2] / "aiask-quant-core" / "src",
    ]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [*(str(path) for path in source_roots), existing_pythonpath]
        if existing_pythonpath
        else [str(path) for path in source_roots]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["host"] == "aiask-reference-host"
    assert payload["agent_loaded"] is False
    assert payload["provider_loaded"] is False


def test_reference_host_declares_no_agent_or_provider_dependency() -> None:
    """The dependency list must stay narrow, not just the import graph."""
    manifest = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    declared = tomllib.loads(manifest)["project"]["dependencies"]
    assert declared == ["strategy-factory"]
