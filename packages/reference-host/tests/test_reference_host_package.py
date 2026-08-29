from __future__ import annotations

import subprocess
import sys

from aiask_reference_host import ReferenceHost
from strategy_factory.api.runtime import HostAdapter, verify_host_adapter


def test_reference_host_is_standalone_and_conformant(tmp_path) -> None:
    host = ReferenceHost(project_root=tmp_path, state_dir=tmp_path / "state")
    assert isinstance(host, HostAdapter)
    report = verify_host_adapter(host)
    assert report["conforms"] is True
    assert host.default_mcp_manifest().command[-1] == "strategy_factory.mcp_server"


def test_reference_host_imports_without_full_agent_or_data_provider(tmp_path) -> None:
    code = (
        "import importlib.util, json\n"
        "from aiask_reference_host import ReferenceHost\n"
        "print(json.dumps({'agent': importlib.util.find_spec('aiask_agent') is not None, "
        "'provider': importlib.util.find_spec('akshare_mcp') is not None, "
        "'host': ReferenceHost(project_root='" + str(tmp_path) + "').name}))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    payload = __import__("json").loads(result.stdout)
    assert payload["host"] == "aiask-reference-host"
    assert payload["agent"] is False
    assert payload["provider"] is False
