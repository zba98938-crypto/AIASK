"""The quality session must not arm paper-execution compensation by default.

Compensation backfills paper orders/trades/positions, so a session that turns it
on is no longer an observation run — its evidence is partly manufactured. The
guard lives in ``scripts/factories/run_strategy_factory_quality_session.py``.

The previous check for this greped that file for the Chinese comment
``'# P0 FIX: 补偿逻辑默认禁用'`` and for the string ``'⚠️  补偿逻辑已启用'``,
asserted nothing, and returned a bool pytest ignores — so it passed even when
its own ``passed`` flag was False. This test calls the real function and reads
the environment it produces.
"""

from __future__ import annotations

import argparse
import ast
import logging
import os
from pathlib import Path

import pytest


_COMPENSATION_ENV_KEYS = (
    "INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED",
    "INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_BATCH_LIMIT",
    "INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED",
    "INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_BATCH_LIMIT",
    "INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED",
    "INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT",
)

_SESSION_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "factories"
    / "run_strategy_factory_quality_session.py"
)


def _load_compensation_gate():
    """Return the real ``_apply_compensation_logic_if_enabled``.

    The function is extracted from the session script's AST rather than imported,
    because importing that script pulls in sibling modules and module-level
    side effects. The body executed is the one in the file.
    """
    if not _SESSION_SCRIPT.is_file():
        pytest.skip(f"session script not present at {_SESSION_SCRIPT}")

    tree = ast.parse(_SESSION_SCRIPT.read_text(encoding="utf-8"), filename=str(_SESSION_SCRIPT))
    target = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_apply_compensation_logic_if_enabled"
        ),
        None,
    )
    assert target is not None, (
        "_apply_compensation_logic_if_enabled is gone from the quality session script; "
        "compensation may no longer be gated"
    )

    namespace: dict[str, object] = {
        "os": os,
        "argparse": argparse,
        "LOGGER": logging.getLogger("test_quality_session_compensation"),
    }
    exec(  # noqa: S102 - a function definition parsed from a repository file
        compile(ast.Module(body=[target], type_ignores=[]), str(_SESSION_SCRIPT), "exec"),
        namespace,
    )
    gate = namespace.get("_apply_compensation_logic_if_enabled")
    assert callable(gate)
    return gate


@pytest.fixture
def clean_compensation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _COMPENSATION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_compensation_is_off_when_no_flag_is_given(clean_compensation_env) -> None:
    _load_compensation_gate()(argparse.Namespace())
    for key in _COMPENSATION_ENV_KEYS:
        assert os.environ.get(key) is None, f"{key} was armed without an explicit request"


def test_compensation_is_off_when_explicitly_disabled(clean_compensation_env) -> None:
    _load_compensation_gate()(argparse.Namespace(enable_compensation=False))
    for key in _COMPENSATION_ENV_KEYS:
        assert os.environ.get(key) is None, f"{key} was armed despite enable_compensation=False"


def test_compensation_is_armed_only_on_explicit_opt_in(clean_compensation_env) -> None:
    # The guard must not be so tight that the documented opt-in stops working.
    _load_compensation_gate()(argparse.Namespace(enable_compensation=True))
    assert os.environ["INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED"] == "1"
    assert os.environ["INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED"] == "1"
    assert (
        os.environ["INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED"] == "1"
    )


def test_opt_in_warns_that_this_is_not_an_observation_run(
    clean_compensation_env, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="test_quality_session_compensation"):
        _load_compensation_gate()(argparse.Namespace(enable_compensation=True))
    assert any(
        "NOT production observation mode" in record.getMessage() for record in caplog.records
    ), "arming compensation must say the run is no longer an observation run"


def test_a_falsy_non_bool_flag_does_not_arm_compensation(clean_compensation_env) -> None:
    for falsy in (0, "", None, [], "false"):
        for key in _COMPENSATION_ENV_KEYS:
            os.environ.pop(key, None)
        _load_compensation_gate()(argparse.Namespace(enable_compensation=falsy))
        armed = os.environ.get("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED")
        if falsy == "false":
            # A non-empty string is truthy in Python; record the real behaviour
            # so a future change to strict parsing is a deliberate decision.
            assert armed == "1"
        else:
            assert armed is None, f"compensation armed by falsy flag {falsy!r}"
