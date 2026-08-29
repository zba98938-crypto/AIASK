from __future__ import annotations

import sqlite3
from typing import Any

from .session_store_utils import _loads, _metadata_archived


def _session_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    return item


def _session_is_archived(conn: sqlite3.Connection, session_id: str) -> bool:
    if not session_id:
        return False
    row = conn.execute("SELECT metadata_json FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        return False
    return _metadata_archived(_loads(row["metadata_json"], {}))


def _activity_event_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    return item


def _tool_invocation_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["input_summary"] = _loads(item.pop("input_summary_json", None), {})
    item["output_summary"] = _loads(item.pop("output_summary_json", None), {})
    item["source_chain"] = _loads(item.pop("source_chain_json", None), [])
    item["secrets_redacted"] = bool(item.get("secrets_redacted"))
    return item


def _source_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    return item


def _artifact_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["preview_json"] = _loads(item.pop("preview_json", None), {})
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    return item


def _context_snapshot_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["compacted"] = bool(item.get("compacted"))
    item["source_message_ids"] = _loads(item.pop("source_message_ids_json", None), [])
    item["source_ids"] = _loads(item.pop("source_ids_json", None), [])
    item["artifact_ids"] = _loads(item.pop("artifact_ids_json", None), [])
    item["risk_flags"] = _loads(item.pop("risk_flags_json", None), [])
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    item["secrets_redacted"] = True
    return item


def _feedback_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    item["allow_learning"] = bool(item.get("allow_learning"))
    return item


def _policy_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["allow_product_analytics"] = bool(item.get("allow_product_analytics"))
    item["allow_learning"] = bool(item.get("allow_learning"))
    return item


def _broker_profile_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["read_only_enabled"] = bool(item.get("read_only_enabled"))
    item["write_enabled"] = bool(item.get("write_enabled"))
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    item["secrets_redacted"] = True
    return item


def _broker_account_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    item["secrets_redacted"] = True
    return item


def _broker_position_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    item["secrets_redacted"] = True
    return item


def _broker_order_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    item["secrets_redacted"] = True
    return item


def _broker_deal_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    item["secrets_redacted"] = True
    return item


def _broker_analytics_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["metrics"] = _loads(item.pop("metrics_json", None), {})
    item["signals"] = _loads(item.pop("signals_json", None), {})
    item["risk_flags"] = _loads(item.pop("risk_flags_json", None), [])
    item["source_snapshot_ids"] = _loads(item.pop("source_snapshot_ids_json", None), {})
    item["secrets_redacted"] = True
    return item


def _handoff_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    return item


def _subgoal_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["criteria"] = _loads(item.pop("criteria_json", None), [])
    return item


def _session_user_id(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return row["user_id"] if row else None
