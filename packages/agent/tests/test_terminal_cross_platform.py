"""Regression guard for cross-platform terminal execution (P0-1 fixes).

Three real bugs were found while running the full agent suite on Windows and
fixed in ``terminal_backends``:

  1. ``_shell()`` preferred COMSPEC (cmd.exe) over PowerShell, so POSIX-style
     commands like ``pwd`` returned non-zero ("not recognized").
  2. ``_direct_python_command_args`` only matched single-quoted, bare-``python``
     invocations, so ``python -c "print('x')"`` (double quotes) and full
     interpreter paths (``C:\\...\\python.exe -c ...``) fell through to the
     PowerShell ``-Command`` path and lost their output.
  3. Wrapper backends (modal/daytona) never used the direct-python shortcut.

These tests lock the fixes in. They run on every platform: the direct-python
parsing assertions are Windows-specific (the helper returns None elsewhere),
so those are guarded by ``os.name``.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from aiask_agent import terminal_backends as tb
from aiask_agent.control_context import internal_control_context
from aiask_agent.general_tools import build_general_tool_handlers
from aiask_agent.tools.policy import GENERAL_FULL_TOOLSET, ToolPolicy


def test_shell_prefers_powershell_over_cmd_on_windows(monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows shell preference only relevant on nt")
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    shell = tb._shell()
    name = shell.lower()
    assert ("powershell" in name) or ("pwsh" in name), (
        f"expected PowerShell to win over cmd.exe, got {shell!r}"
    )


def test_explicit_shell_env_still_wins(monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows shell preference only relevant on nt")
    monkeypatch.setenv("SHELL", r"C:\custom\bash.exe")
    assert tb._shell() == r"C:\custom\bash.exe"


@pytest.mark.skipif(os.name != "nt", reason="direct python shortcut is Windows-only")
@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
        'python -c "print(1)"',
        'python -c "print(\'x\')"',
        "python3 -c 'print(1)'",
        'py -c "print(1)"',
    ],
)
def test_direct_python_handles_both_quote_styles(command: str) -> None:
    args = tb._direct_python_command_args(command)
    assert args is not None, f"should detect python -c for {command!r}"
    assert args[1] == "-c"
    assert "print" in args[2]
    # the code argument must not retain wrapping quotes
    assert not (args[2].startswith(("'", '"')) and args[2].endswith(("'", '"')))


@pytest.mark.skipif(os.name != "nt", reason="direct python shortcut is Windows-only")
def test_direct_python_handles_full_interpreter_path() -> None:
    command = f'{sys.executable} -c "print(42)"'
    args = tb._direct_python_command_args(command)
    assert args is not None, "full python.exe path must be detected"
    # backslashes in the path must be preserved (non-posix split)
    assert args[0] == sys.executable or args[0].lower().endswith("python.exe")
    assert args[1:] == ["-c", "print(42)"]


@pytest.mark.skipif(os.name != "nt", reason="direct python shortcut is Windows-only")
def test_direct_python_ignores_non_python_commands() -> None:
    assert tb._direct_python_command_args("pwd") is None
    assert tb._direct_python_command_args("node -e 'x'") is None
    assert tb._direct_python_command_args("") is None


@pytest.mark.skipif(os.name != "nt", reason="wrapper backend routing is Windows-only")
def test_wrapper_backend_uses_direct_python(monkeypatch) -> None:
    from aiask_agent.process_registry import ProcessRegistry

    monkeypatch.setenv("AIASK_MODAL_TERMINAL_COMMAND", f'{sys.executable} -c "print(\'ok\')"')
    backend = tb.ModalBackend(ProcessRegistry(None))
    invocation = tb.TerminalInvocation(command="ignored", cwd=".", backend="modal")
    args, meta = backend.build_command(invocation)
    assert meta.get("direct_command") == "python_c", "modal backend should use direct python exec"
    assert args[1] == "-c"


def test_general_terminal_bounds_invalid_numeric_inputs(tmp_path) -> None:
    handlers = build_general_tool_handlers(
        ToolPolicy(GENERAL_FULL_TOOLSET, True, (str(tmp_path),)),
        state_path=tmp_path / "state.sqlite3",
    )

    pending = asyncio.run(
        handlers["agent_terminal"](
            {
                "command": f'{sys.executable} -c "print(123)"',
                "cwd": str(tmp_path),
                "timeout_seconds": "nan",
                "max_output_bytes": "not-an-int",
            }
        )
    )
    assert pending["success"] is False
    assert pending["error_code"] == "ACTION_INTENT_REQUIRED"

    with internal_control_context("test.terminal_numeric_bounds"):
        result = asyncio.run(
            handlers["agent_terminal"](
                {
                    "command": f'{sys.executable} -c "print(123)"',
                    "cwd": str(tmp_path),
                    "timeout_seconds": "nan",
                    "max_output_bytes": "not-an-int",
                }
            )
        )

    assert result["success"] is True
    assert result["data"]["stdout"].strip() == "123"
