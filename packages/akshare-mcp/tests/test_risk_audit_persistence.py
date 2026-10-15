"""An audit write that failed must not look like one that succeeded.

Background
----------
``risk_guard.audit_event`` swallowed every exception and then returned the event
id anyway::

    except Exception:
        # 审计不应影响主流程
        pass
    return resolved_id

Not breaking the caller's main flow is the right goal. Returning an id
regardless is not: the live-order payload then carried the same
``audit_event_id`` whether the record existed or not.

A second layer did the same thing. ``build_manager_meta`` synthesised an id
whenever the caller supplied none::

    audit_event_id = normalized_extra.pop("audit_event_id", None) or generate_audit_event_id(...)

so a field that reads like a reference to a persisted record was invented at
response-build time on almost every tool call. ``generate_audit_event_id``
itself documents its output as per-invocation, which is fine as a correlation
id and misleading as an audit reference.
"""

from __future__ import annotations

import time
from pathlib import Path

from akshare_mcp.tools.manager_protocol import build_manager_meta
from akshare_mcp.tools.risk_guard import audit_event


def test_audit_event_returns_the_id_when_the_record_is_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "risk_audit.jsonl"
    monkeypatch.setenv("AKSHARE_AUDIT_LOG", str(target))

    event_id = audit_event("live_trading:submit_order", {"symbol": "600519"}, {"success": True})

    assert event_id is not None
    contents = target.read_text(encoding="utf-8")
    assert event_id in contents, "the returned id must identify a written record"


def test_audit_event_returns_none_when_the_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A directory where the log file should be makes the append fail."""

    blocker = tmp_path / "risk_audit.jsonl"
    blocker.mkdir()
    monkeypatch.setenv("AKSHARE_AUDIT_LOG", str(blocker))

    result = audit_event("live_trading:submit_order", {"symbol": "600519"}, {"success": True})

    assert result is None, (
        "an unwritten audit must not hand back an id; that is what made a "
        "failed audit indistinguishable from a successful one"
    )


def test_manager_meta_labels_a_synthesized_id() -> None:
    meta = build_manager_meta(
        tool_name="risk_manager", action="stress_test", started_at=time.perf_counter()
    )

    assert meta["audit_event_id"]
    assert meta["audit_event_id_source"] == "synthesized", (
        "no audit record was written for this envelope, so the id is a "
        "correlation id and must say so"
    )


def test_manager_meta_labels_a_supplied_id() -> None:
    meta = build_manager_meta(
        tool_name="risk_manager",
        action="stress_test",
        started_at=time.perf_counter(),
        extra={"audit_event_id": "audit:pinned:record"},
    )

    assert meta["audit_event_id"] == "audit:pinned:record"
    assert meta["audit_event_id_source"] == "audit_record"
