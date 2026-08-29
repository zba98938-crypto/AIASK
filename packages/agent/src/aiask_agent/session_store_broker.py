from __future__ import annotations

from typing import Any
from uuid import uuid4

from .session_store_utils import _clean_optional, _dumps, _float_or_none, _loads, now_iso, sanitize_for_audit


class SessionStoreBrokerMixin:
    def upsert_broker_profile(
        self,
        *,
        user_id: str,
        provider: str,
        display_name: str | None = None,
        account_ref_hash: str | None = None,
        market: str | None = None,
        read_only_enabled: bool = True,
        consent_status: str = "granted",
        consent_version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        broker = str(provider or "").strip().lower()
        if not broker:
            raise ValueError("provider is required")
        ts = now_iso()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT broker_profile_id, metadata_json, created_at FROM broker_profiles WHERE user_id = ? AND provider = ? ORDER BY updated_at DESC LIMIT 1",
                (uid, broker),
            ).fetchone()
            profile_id = row["broker_profile_id"] if row else f"broker_profile_{uuid4().hex}"
            existing_metadata = _loads(row["metadata_json"], {}) if row else {}
            merged_metadata = {**dict(existing_metadata or {}), **dict(metadata or {})}
            conn.execute(
                """
                INSERT OR REPLACE INTO broker_profiles
                    (broker_profile_id, user_id, provider, display_name, account_ref_hash,
                     market, read_only_enabled, write_enabled, consent_status, consent_version,
                     status, error_code, metadata_json, last_sync_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    uid,
                    broker,
                    _clean_optional(display_name),
                    _clean_optional(account_ref_hash),
                    _clean_optional(market),
                    1 if read_only_enabled else 0,
                    str(consent_status or "unknown").strip() or "unknown",
                    _clean_optional(consent_version),
                    "configured" if read_only_enabled else "disabled",
                    None,
                    _dumps(sanitize_for_audit(merged_metadata)),
                    None,
                    row["created_at"] if row else ts,
                    ts,
                ),
            )
            conn.commit()
        return self.get_broker_profile(profile_id) or {"broker_profile_id": profile_id, "user_id": uid, "provider": broker}

    def mark_broker_profile_synced(
        self,
        *,
        broker_profile_id: str,
        status: str = "ready",
        error_code: str | None = None,
    ) -> dict[str, Any]:
        profile_id = str(broker_profile_id or "").strip()
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                "UPDATE broker_profiles SET status = ?, error_code = ?, last_sync_at = ?, updated_at = ? WHERE broker_profile_id = ?",
                (str(status or "ready"), _clean_optional(error_code), ts, ts, profile_id),
            )
            conn.commit()
        return self.get_broker_profile(profile_id) or {"broker_profile_id": profile_id, "status": status}

    def get_broker_profile(self, broker_profile_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM broker_profiles WHERE broker_profile_id = ?", (str(broker_profile_id or "").strip(),)).fetchone()
        return self._broker_profile_row(row)

    def list_broker_profiles(
        self,
        *,
        user_id: str | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            values.append(str(user_id).strip())
        if provider:
            clauses.append("provider = ?")
            values.append(str(provider).strip().lower())
        values.append(max(1, min(int(limit or 100), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT * FROM broker_profiles {where} ORDER BY updated_at DESC LIMIT ?", tuple(values)).fetchall()
        return [item for row in rows if (item := self._broker_profile_row(row)) is not None]

    def record_broker_account_snapshot(
        self,
        *,
        broker_profile_id: str,
        user_id: str,
        provider: str,
        account: dict[str, Any],
        source_tool: str | None = None,
        source_run_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        snapshot_id = f"broker_account_{uuid4().hex}"
        ts = now_iso()
        observed = str(observed_at or ts)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO broker_account_snapshots
                    (snapshot_id, broker_profile_id, user_id, provider, account_ref_hash,
                     currency, total_asset, cash_available, market_value, frozen_cash,
                     buying_power, source_tool, source_run_id, observed_at, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    broker_profile_id,
                    user_id,
                    provider,
                    _clean_optional(account.get("account_ref_hash")),
                    _clean_optional(account.get("currency")),
                    _float_or_none(account.get("total_asset")),
                    _float_or_none(account.get("cash_available")),
                    _float_or_none(account.get("market_value")),
                    _float_or_none(account.get("frozen_cash")),
                    _float_or_none(account.get("buying_power")),
                    _clean_optional(source_tool),
                    _clean_optional(source_run_id),
                    observed,
                    _dumps(sanitize_for_audit(account)),
                    ts,
                ),
            )
            conn.commit()
        return self.list_broker_account_snapshots(broker_profile_id=broker_profile_id, limit=1)[0]

    def record_broker_position_snapshots(
        self,
        *,
        broker_profile_id: str,
        user_id: str,
        provider: str,
        positions: list[dict[str, Any]],
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        ts = now_iso()
        observed = str(observed_at or ts)
        rows: list[tuple[Any, ...]] = []
        for item in list(positions or [])[:1000]:
            rows.append(
                (
                    f"broker_position_{uuid4().hex}",
                    broker_profile_id,
                    user_id,
                    provider,
                    _clean_optional(item.get("symbol")),
                    _clean_optional(item.get("exchange")),
                    _clean_optional(item.get("name")),
                    _float_or_none(item.get("quantity")),
                    _float_or_none(item.get("available_quantity")),
                    _float_or_none(item.get("cost_basis")),
                    _float_or_none(item.get("last_price")),
                    _float_or_none(item.get("market_value")),
                    _float_or_none(item.get("unrealized_pnl")),
                    _float_or_none(item.get("unrealized_pnl_pct")),
                    observed,
                    _dumps(sanitize_for_audit(item)),
                    ts,
                )
            )
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO broker_position_snapshots
                    (snapshot_id, broker_profile_id, user_id, provider, symbol, exchange,
                     name, quantity, available_quantity, cost_basis, last_price, market_value,
                     unrealized_pnl, unrealized_pnl_pct, observed_at, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return self.list_broker_position_snapshots(broker_profile_id=broker_profile_id, limit=len(rows) or 1)

    def record_broker_order_snapshots(
        self,
        *,
        broker_profile_id: str,
        user_id: str,
        provider: str,
        orders: list[dict[str, Any]],
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        ts = now_iso()
        observed = str(observed_at or ts)
        rows: list[tuple[Any, ...]] = []
        for item in list(orders or [])[:1000]:
            rows.append(
                (
                    f"broker_order_{uuid4().hex}",
                    broker_profile_id,
                    user_id,
                    provider,
                    _clean_optional(item.get("order_ref_hash")),
                    _clean_optional(item.get("symbol")),
                    _clean_optional(item.get("side")),
                    _clean_optional(item.get("order_type")),
                    _float_or_none(item.get("price")),
                    _float_or_none(item.get("quantity")),
                    _float_or_none(item.get("filled_quantity")),
                    _clean_optional(item.get("status")),
                    _clean_optional(item.get("submitted_at")),
                    _clean_optional(item.get("updated_at")),
                    observed,
                    _dumps(sanitize_for_audit(item)),
                    ts,
                )
            )
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO broker_order_snapshots
                    (snapshot_id, broker_profile_id, user_id, provider, order_ref_hash,
                     symbol, side, order_type, price, quantity, filled_quantity, status,
                     submitted_at, updated_at, observed_at, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return self.list_broker_order_snapshots(broker_profile_id=broker_profile_id, limit=len(rows) or 1)

    def record_broker_deal_snapshots(
        self,
        *,
        broker_profile_id: str,
        user_id: str,
        provider: str,
        deals: list[dict[str, Any]],
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        ts = now_iso()
        observed = str(observed_at or ts)
        rows: list[tuple[Any, ...]] = []
        for item in list(deals or [])[:1000]:
            rows.append(
                (
                    f"broker_deal_{uuid4().hex}",
                    broker_profile_id,
                    user_id,
                    provider,
                    _clean_optional(item.get("deal_ref_hash")),
                    _clean_optional(item.get("order_ref_hash")),
                    _clean_optional(item.get("symbol")),
                    _clean_optional(item.get("side")),
                    _float_or_none(item.get("price")),
                    _float_or_none(item.get("quantity")),
                    _float_or_none(item.get("amount")),
                    _float_or_none(item.get("fee")),
                    _clean_optional(item.get("occurred_at")),
                    observed,
                    _dumps(sanitize_for_audit(item)),
                    ts,
                )
            )
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO broker_deal_snapshots
                    (snapshot_id, broker_profile_id, user_id, provider, deal_ref_hash,
                     order_ref_hash, symbol, side, price, quantity, amount, fee,
                     occurred_at, observed_at, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return self.list_broker_deal_snapshots(broker_profile_id=broker_profile_id, limit=len(rows) or 1)

    def record_broker_behavior_analytics(
        self,
        *,
        broker_profile_id: str | None,
        user_id: str,
        provider: str,
        period_start: str | None = None,
        period_end: str | None = None,
        metrics: dict[str, Any] | None = None,
        signals: dict[str, Any] | None = None,
        risk_flags: list[dict[str, Any]] | None = None,
        source_snapshot_ids: dict[str, Any] | None = None,
        model_version: str = "deterministic-p0",
    ) -> dict[str, Any]:
        analytics_id = f"broker_analytics_{uuid4().hex}"
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO broker_behavior_analytics
                    (analytics_id, broker_profile_id, user_id, provider, period_start,
                     period_end, metrics_json, signals_json, risk_flags_json,
                     source_snapshot_ids_json, model_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analytics_id,
                    _clean_optional(broker_profile_id),
                    str(user_id or "local").strip() or "local",
                    str(provider or "unknown").strip().lower() or "unknown",
                    _clean_optional(period_start),
                    _clean_optional(period_end),
                    _dumps(sanitize_for_audit(metrics or {})),
                    _dumps(sanitize_for_audit(signals or {})),
                    _dumps(sanitize_for_audit(risk_flags or [])),
                    _dumps(sanitize_for_audit(source_snapshot_ids or {})),
                    str(model_version or "deterministic-p0"),
                    ts,
                ),
            )
            conn.commit()
        return self.latest_broker_analytics(user_id=user_id, provider=provider) or {"analytics_id": analytics_id}

    def list_broker_account_snapshots(self, **filters: Any) -> list[dict[str, Any]]:
        return self._list_broker_rows("broker_account_snapshots", self._broker_account_row, **filters)

    def list_broker_position_snapshots(self, **filters: Any) -> list[dict[str, Any]]:
        return self._list_broker_rows("broker_position_snapshots", self._broker_position_row, **filters)

    def list_broker_order_snapshots(self, **filters: Any) -> list[dict[str, Any]]:
        return self._list_broker_rows("broker_order_snapshots", self._broker_order_row, **filters)

    def list_broker_deal_snapshots(self, **filters: Any) -> list[dict[str, Any]]:
        return self._list_broker_rows("broker_deal_snapshots", self._broker_deal_row, **filters)

    def latest_broker_analytics(
        self,
        *,
        user_id: str | None = None,
        provider: str | None = None,
        broker_profile_id: str | None = None,
    ) -> dict[str, Any] | None:
        rows = self.list_broker_analytics(user_id=user_id, provider=provider, broker_profile_id=broker_profile_id, limit=1)
        return rows[0] if rows else None

    def list_broker_analytics(
        self,
        *,
        user_id: str | None = None,
        provider: str | None = None,
        broker_profile_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses, values = self._broker_filter_clauses(user_id=user_id, provider=provider, broker_profile_id=broker_profile_id)
        values.append(max(1, min(int(limit or 20), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT * FROM broker_behavior_analytics {where} ORDER BY created_at DESC LIMIT ?", tuple(values)).fetchall()
        return [item for row in rows if (item := self._broker_analytics_row(row)) is not None]

    def _list_broker_rows(
        self,
        table: str,
        row_mapper: Any,
        *,
        user_id: str | None = None,
        provider: str | None = None,
        broker_profile_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses, values = self._broker_filter_clauses(user_id=user_id, provider=provider, broker_profile_id=broker_profile_id)
        values.append(max(1, min(int(limit or 100), 5000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT * FROM {table} {where} ORDER BY observed_at DESC, created_at DESC LIMIT ?", tuple(values)).fetchall()
        return [item for row in rows if (item := row_mapper(row)) is not None]

    @staticmethod
    def _broker_filter_clauses(
        *,
        user_id: str | None = None,
        provider: str | None = None,
        broker_profile_id: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            values.append(str(user_id).strip())
        if provider:
            clauses.append("provider = ?")
            values.append(str(provider).strip().lower())
        if broker_profile_id:
            clauses.append("broker_profile_id = ?")
            values.append(str(broker_profile_id).strip())
        return clauses, values
