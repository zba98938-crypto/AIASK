from __future__ import annotations

from aiask_quant_core.incubation_stages import normalize_incubation_stage

from ._base import *  # noqa: F401,F403
from ._base import (
    _coerce_ts,
    _fallback_execution_audit_gate,
    _safe_float,
    _safe_int,
    _safe_rules_dict,
    _string,
)


class _IncReadsMixin:
    """孵化账户 + 孵化指标 + 模拟盘(paper) + 孵化流水线快照"""

    # ── 孵化账户 ──

    def _decode_incubation_account(self, row: dict) -> dict:
        result = dict(row)
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def save_strategy_incubation_account(
        self,
        strategy_id: str,
        account_id: str,
        stage: str = "warmup",
        status: str = "active",
        source_run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_incubation_accounts
                    (strategy_id, account_id, stage, status, source_run_id, metadata, bound_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (strategy_id, account_id) DO UPDATE SET
                    stage = EXCLUDED.stage,
                    status = EXCLUDED.status,
                    source_run_id = EXCLUDED.source_run_id,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                strategy_id,
                account_id,
                normalize_incubation_stage(stage),
                str(status or "active"),
                source_run_id,
                json.dumps(metadata or {}, ensure_ascii=False, default=str),
            )
        return self._decode_incubation_account(dict(row))

    async def get_strategy_incubation_account(
        self,
        strategy_id: str,
        account_id: Optional[str] = None,
    ) -> Optional[dict]:
        async with self.acquire() as conn:
            if account_id:
                row = await conn.fetchrow(
                    """
                    SELECT * FROM strategy_incubation_accounts
                    WHERE strategy_id = $1 AND account_id = $2
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    strategy_id,
                    account_id,
                )
            else:
                exclusions_present = bool(await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_evidence_exclusions')"
                ))
                exclusion_filter = (
                    "AND NOT EXISTS (SELECT 1 FROM paper_evidence_exclusions e "
                    "WHERE e.account_id = strategy_incubation_accounts.account_id)"
                    if exclusions_present else ""
                )
                row = await conn.fetchrow(
                    f"""
                    SELECT * FROM strategy_incubation_accounts
                    WHERE strategy_id = $1 AND status = 'active'
                      {exclusion_filter}
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    strategy_id,
                )
        if not row:
            return None
        return self._decode_incubation_account(dict(row))

    async def list_strategy_incubation_accounts(
        self,
        strategy_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_incubation_accounts WHERE 1=1"
            params: list = []
            idx = 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            if status:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            sql += f" ORDER BY updated_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 200)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_incubation_account(dict(row)) for row in rows]

    # ── 孵化指标 ──

    def _decode_incubation_metric(self, row: dict) -> dict:
        result = dict(row)
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        result["blockers"] = self._decode_json_field(result.get("blockers"), [])
        result["risk_flags"] = self._decode_json_field(result.get("risk_flags"), [])
        return result

    async def save_strategy_incubation_metric(self, strategy_id: str, metric_date, metric: dict) -> dict:
        payload = dict(metric or {})
        payload["stage"] = normalize_incubation_stage(payload.get("stage"))
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_incubation_metrics
                    (strategy_id, account_id, metric_date, stage, total_value, cash, market_value, nav,
                     daily_return, max_drawdown, sharpe_ratio, hit_rate_5d, hit_rate_lcb_5d, skill_lcb_5d,
                     effective_n_5d, recent_hit_rate_5d, recent_skill_lcb_5d, stability_gap_5d,
                     forward_ic_5d, forward_sharpe_5d,
                     total_signals, total_orders, total_trades, turnover_rate, exposure_rate, alpha_decay,
                     drift_score, blockers, risk_flags, decision, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, $18, $19, $20,
                        $21, $22, $23, $24, $25, $26,
                        $27, $28, $29, $30, $31, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (strategy_id, metric_date) DO UPDATE SET
                    account_id = EXCLUDED.account_id,
                    stage = EXCLUDED.stage,
                    total_value = EXCLUDED.total_value,
                    cash = EXCLUDED.cash,
                    market_value = EXCLUDED.market_value,
                    nav = EXCLUDED.nav,
                    daily_return = EXCLUDED.daily_return,
                    max_drawdown = EXCLUDED.max_drawdown,
                    sharpe_ratio = EXCLUDED.sharpe_ratio,
                    hit_rate_5d = EXCLUDED.hit_rate_5d,
                    hit_rate_lcb_5d = EXCLUDED.hit_rate_lcb_5d,
                    skill_lcb_5d = EXCLUDED.skill_lcb_5d,
                    effective_n_5d = EXCLUDED.effective_n_5d,
                    recent_hit_rate_5d = EXCLUDED.recent_hit_rate_5d,
                    recent_skill_lcb_5d = EXCLUDED.recent_skill_lcb_5d,
                    stability_gap_5d = EXCLUDED.stability_gap_5d,
                    forward_ic_5d = EXCLUDED.forward_ic_5d,
                    forward_sharpe_5d = EXCLUDED.forward_sharpe_5d,
                    total_signals = EXCLUDED.total_signals,
                    total_orders = EXCLUDED.total_orders,
                    total_trades = EXCLUDED.total_trades,
                    turnover_rate = EXCLUDED.turnover_rate,
                    exposure_rate = EXCLUDED.exposure_rate,
                    alpha_decay = EXCLUDED.alpha_decay,
                    drift_score = EXCLUDED.drift_score,
                    blockers = EXCLUDED.blockers,
                    risk_flags = EXCLUDED.risk_flags,
                    decision = EXCLUDED.decision,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                strategy_id,
                payload.get("account_id"),
                metric_date,
                str(payload.get("stage") or "warmup"),
                payload.get("total_value"),
                payload.get("cash"),
                payload.get("market_value"),
                payload.get("nav"),
                payload.get("daily_return"),
                payload.get("max_drawdown"),
                payload.get("sharpe_ratio"),
                payload.get("hit_rate_5d"),
                payload.get("hit_rate_lcb_5d"),
                payload.get("skill_lcb_5d"),
                payload.get("effective_n_5d"),
                payload.get("recent_hit_rate_5d"),
                payload.get("recent_skill_lcb_5d"),
                payload.get("stability_gap_5d"),
                payload.get("forward_ic_5d"),
                payload.get("forward_sharpe_5d"),
                int(payload.get("total_signals") or 0),
                int(payload.get("total_orders") or 0),
                int(payload.get("total_trades") or 0),
                payload.get("turnover_rate"),
                payload.get("exposure_rate"),
                payload.get("alpha_decay"),
                payload.get("drift_score"),
                json.dumps(payload.get("blockers") or [], ensure_ascii=False, default=str),
                json.dumps(payload.get("risk_flags") or [], ensure_ascii=False, default=str),
                payload.get("decision"),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
            )
        return self._decode_incubation_metric(dict(row))

    async def get_latest_strategy_incubation_metric(self, strategy_id: str) -> Optional[dict]:
        rows = await self.list_strategy_incubation_metrics(strategy_id, limit=1)
        return rows[0] if rows else None

    # ── Phase 5 candidate / signal evidence ──

    def _decode_strategy_candidate_evidence(self, row: dict) -> dict:
        result = dict(row)
        result["payload"] = self._decode_json_field(result.get("payload"), {})
        return result

    async def save_strategy_candidate_evidence(self, evidence: dict) -> dict:
        payload = dict(evidence or {})
        candidate_id = str(payload.get("candidate_id") or "").strip()
        evidence_id = str(payload.get("evidence_id") or "").strip()
        row_id = str(payload.get("id") or f"{candidate_id}:{evidence_id}").strip()
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_candidate_evidence
                    (id, candidate_id, strategy_id, candidate_artifact_id, experiment_id, evidence_id,
                     evidence_type, source_type, event_type, target_symbols, direction, horizon_days,
                     raw_confidence, calibrated_confidence, freshness_ts, proxy_only, support_metric,
                     doc_uid, headline_label_id, source_task_key, payload, created_at)
                VALUES ($1, $2, $3, $4, $5, $6,
                        $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17,
                        $18, $19, $20, $21, CURRENT_TIMESTAMP)
                ON CONFLICT (candidate_id, evidence_id) DO UPDATE SET
                    strategy_id = COALESCE(EXCLUDED.strategy_id, strategy_candidate_evidence.strategy_id),
                    candidate_artifact_id = COALESCE(EXCLUDED.candidate_artifact_id, strategy_candidate_evidence.candidate_artifact_id),
                    experiment_id = COALESCE(EXCLUDED.experiment_id, strategy_candidate_evidence.experiment_id),
                    evidence_type = COALESCE(EXCLUDED.evidence_type, strategy_candidate_evidence.evidence_type),
                    source_type = COALESCE(EXCLUDED.source_type, strategy_candidate_evidence.source_type),
                    event_type = COALESCE(EXCLUDED.event_type, strategy_candidate_evidence.event_type),
                    target_symbols = COALESCE(EXCLUDED.target_symbols, strategy_candidate_evidence.target_symbols),
                    direction = COALESCE(EXCLUDED.direction, strategy_candidate_evidence.direction),
                    horizon_days = COALESCE(EXCLUDED.horizon_days, strategy_candidate_evidence.horizon_days),
                    raw_confidence = COALESCE(EXCLUDED.raw_confidence, strategy_candidate_evidence.raw_confidence),
                    calibrated_confidence = COALESCE(EXCLUDED.calibrated_confidence, strategy_candidate_evidence.calibrated_confidence),
                    freshness_ts = COALESCE(EXCLUDED.freshness_ts, strategy_candidate_evidence.freshness_ts),
                    proxy_only = COALESCE(EXCLUDED.proxy_only, strategy_candidate_evidence.proxy_only),
                    support_metric = COALESCE(EXCLUDED.support_metric, strategy_candidate_evidence.support_metric),
                    doc_uid = COALESCE(EXCLUDED.doc_uid, strategy_candidate_evidence.doc_uid),
                    headline_label_id = COALESCE(EXCLUDED.headline_label_id, strategy_candidate_evidence.headline_label_id),
                    source_task_key = COALESCE(EXCLUDED.source_task_key, strategy_candidate_evidence.source_task_key),
                    payload = COALESCE(EXCLUDED.payload, strategy_candidate_evidence.payload)
                RETURNING *
                """,
                row_id,
                candidate_id,
                payload.get("strategy_id"),
                payload.get("candidate_artifact_id"),
                payload.get("experiment_id"),
                evidence_id,
                payload.get("evidence_type"),
                payload.get("source_type") or payload.get("evidence_type"),
                payload.get("event_type"),
                json.dumps(payload.get("target_symbols") or [], ensure_ascii=False, default=str),
                payload.get("direction"),
                payload.get("horizon_days"),
                payload.get("raw_confidence"),
                payload.get("calibrated_confidence"),
                _coerce_ts(payload.get("freshness_ts")),
                bool(payload.get("proxy_only")),
                json.dumps(payload.get("support_metric") or {}, ensure_ascii=False, default=str),
                payload.get("doc_uid"),
                payload.get("headline_label_id"),
                payload.get("source_task_key") or payload.get("task_key"),
                json.dumps(
                    payload.get("payload") or payload.get("evidence_payload") or payload,
                    ensure_ascii=False,
                    default=str,
                ),
            )
        return self._decode_strategy_candidate_evidence(dict(row))

    async def list_strategy_candidate_evidence(
        self,
        *,
        candidate_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_candidate_evidence WHERE 1=1"
            params: list[Any] = []
            idx = 1
            if candidate_id:
                sql += f" AND candidate_id = ${idx}"
                params.append(candidate_id)
                idx += 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            sql += f" ORDER BY created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 5000)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_strategy_candidate_evidence(dict(row)) for row in rows]

    def _decode_strategy_signal_evidence(self, row: dict) -> dict:
        result = dict(row)
        result["payload"] = self._decode_json_field(result.get("payload"), {})
        return result

    async def save_strategy_signal_evidence(self, evidence: dict) -> dict:
        payload = dict(evidence or {})
        signal_id = str(payload.get("signal_id") or "").strip()
        evidence_id = str(payload.get("evidence_id") or "").strip()
        applied_claim_id = _string(payload.get("applied_claim_id")) or None
        applied_trade_step_id = _string(payload.get("applied_trade_step_id")) or None
        row_id = str(
            payload.get("id")
            or f"{signal_id}:{evidence_id}:{applied_claim_id or 'unclaimed'}:{applied_trade_step_id or 'unmapped_step'}"
        ).strip()
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_signal_evidence
                    (id, signal_id, strategy_id, signal_date, signal_ts, code, candidate_artifact_id, experiment_id,
                     evidence_id, applied_claim_id, applied_trade_step_id, source_type, direction, horizon_days, raw_confidence,
                     calibrated_confidence, proxy_only, doc_uid, headline_label_id, runtime_action_reason, runtime_action_source,
                     payload, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14, $15,
                        $16, $17, $18, $19, $20, $21,
                        $22, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    strategy_id = COALESCE(EXCLUDED.strategy_id, strategy_signal_evidence.strategy_id),
                    signal_date = COALESCE(EXCLUDED.signal_date, strategy_signal_evidence.signal_date),
                    signal_ts = COALESCE(EXCLUDED.signal_ts, strategy_signal_evidence.signal_ts),
                    code = COALESCE(EXCLUDED.code, strategy_signal_evidence.code),
                    candidate_artifact_id = COALESCE(EXCLUDED.candidate_artifact_id, strategy_signal_evidence.candidate_artifact_id),
                    experiment_id = COALESCE(EXCLUDED.experiment_id, strategy_signal_evidence.experiment_id),
                    applied_claim_id = COALESCE(EXCLUDED.applied_claim_id, strategy_signal_evidence.applied_claim_id),
                    applied_trade_step_id = COALESCE(EXCLUDED.applied_trade_step_id, strategy_signal_evidence.applied_trade_step_id),
                    source_type = COALESCE(EXCLUDED.source_type, strategy_signal_evidence.source_type),
                    direction = COALESCE(EXCLUDED.direction, strategy_signal_evidence.direction),
                    horizon_days = COALESCE(EXCLUDED.horizon_days, strategy_signal_evidence.horizon_days),
                    raw_confidence = COALESCE(EXCLUDED.raw_confidence, strategy_signal_evidence.raw_confidence),
                    calibrated_confidence = COALESCE(EXCLUDED.calibrated_confidence, strategy_signal_evidence.calibrated_confidence),
                    proxy_only = COALESCE(EXCLUDED.proxy_only, strategy_signal_evidence.proxy_only),
                    doc_uid = COALESCE(EXCLUDED.doc_uid, strategy_signal_evidence.doc_uid),
                    headline_label_id = COALESCE(EXCLUDED.headline_label_id, strategy_signal_evidence.headline_label_id),
                    runtime_action_reason = COALESCE(EXCLUDED.runtime_action_reason, strategy_signal_evidence.runtime_action_reason),
                    runtime_action_source = COALESCE(EXCLUDED.runtime_action_source, strategy_signal_evidence.runtime_action_source),
                    payload = COALESCE(EXCLUDED.payload, strategy_signal_evidence.payload)
                RETURNING *
                """,
                row_id,
                signal_id,
                payload.get("strategy_id"),
                payload.get("signal_date"),
                _coerce_ts(payload.get("signal_ts") or payload.get("signal_date")),
                payload.get("code") or payload.get("symbol"),
                payload.get("candidate_artifact_id"),
                payload.get("experiment_id"),
                evidence_id,
                applied_claim_id,
                applied_trade_step_id,
                payload.get("source_type") or payload.get("evidence_type"),
                payload.get("direction"),
                payload.get("horizon_days"),
                payload.get("raw_confidence"),
                payload.get("calibrated_confidence"),
                bool(payload.get("proxy_only")),
                payload.get("doc_uid"),
                payload.get("headline_label_id"),
                payload.get("runtime_action_reason"),
                payload.get("runtime_action_source"),
                json.dumps(
                    payload.get("payload") or payload.get("evidence_payload") or payload,
                    ensure_ascii=False,
                    default=str,
                ),
            )
        return self._decode_strategy_signal_evidence(dict(row))

    async def list_strategy_signal_evidence(
        self,
        *,
        signal_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_signal_evidence WHERE 1=1"
            params: list[Any] = []
            idx = 1
            if signal_id:
                sql += f" AND signal_id = ${idx}"
                params.append(signal_id)
                idx += 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            sql += f" ORDER BY COALESCE(signal_ts, signal_date) DESC, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 5000)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_strategy_signal_evidence(dict(row)) for row in rows]

    # ── 模拟盘 ──

    async def get_paper_account(self, account_id: str) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM paper_accounts WHERE id = $1 LIMIT 1",
                account_id,
            )
        return dict(row) if row else None

    async def get_paper_account_by_strategy(self, strategy_id: str) -> Optional[dict]:
        async with self.acquire() as conn:
            exclusions_present = bool(await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_evidence_exclusions')"
            ))
            exclusion_filter = (
                "AND NOT EXISTS (SELECT 1 FROM paper_evidence_exclusions e "
                "WHERE e.account_id = paper_accounts.id)"
                if exclusions_present else ""
            )
            row = await conn.fetchrow(
                f"""SELECT * FROM paper_accounts
                   WHERE strategy_id = $1 AND COALESCE(status, 'active') = 'active'
                     {exclusion_filter}
                   ORDER BY created_at LIMIT 1""",
                strategy_id,
            )
        return dict(row) if row else None

    async def save_paper_account(self, account: dict) -> dict:
        payload = dict(account or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_accounts
                    (id, user_id, name, initial_capital, current_capital, total_value, risk_rules,
                     strategy_id, account_type, incubation_stage, promotion_candidate, archived_reason, status, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7,
                        $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    name = EXCLUDED.name,
                    initial_capital = EXCLUDED.initial_capital,
                    current_capital = EXCLUDED.current_capital,
                    total_value = EXCLUDED.total_value,
                    risk_rules = EXCLUDED.risk_rules,
                    strategy_id = EXCLUDED.strategy_id,
                    account_type = EXCLUDED.account_type,
                    incubation_stage = EXCLUDED.incubation_stage,
                    promotion_candidate = EXCLUDED.promotion_candidate,
                    archived_reason = EXCLUDED.archived_reason,
                    status = EXCLUDED.status,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                str(payload.get('id') or ''),
                payload.get('user_id') or 'default',
                str(payload.get('name') or 'paper_account'),
                float(payload.get('initial_capital') or 0.0),
                float(payload.get('current_capital') or 0.0),
                float(payload.get('total_value') or 0.0),
                json.dumps(_safe_rules_dict(payload.get('risk_rules')), ensure_ascii=False, default=str),
                payload.get('strategy_id'),
                payload.get('account_type') or 'manual',
                payload.get('incubation_stage') or 'warmup',
                bool(payload.get('promotion_candidate')),
                payload.get('archived_reason'),
                payload.get('status') or 'active',
            )
        return dict(row)

    async def update_paper_account_status(
        self,
        account_id: str,
        status: str,
        stage=None,
        promotion_candidate=None,
        observation_candidate=None,
    ) -> Optional[dict]:
        if promotion_candidate is None and observation_candidate is not None:
            promotion_candidate = False
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE paper_accounts
                SET status = $2,
                    incubation_stage = COALESCE($3, incubation_stage),
                    promotion_candidate = COALESCE($4, promotion_candidate),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                RETURNING *
                """,
                account_id,
                status,
                stage,
                promotion_candidate,
            )
        return dict(row) if row else None

    async def list_strategy_paper_orders(self, strategy_id: str, signal_date = None, status: Optional[str] = None, limit: int = 200) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM paper_orders WHERE strategy_id = $1"
            params: list = [strategy_id]
            idx = 2
            if signal_date is not None:
                sql += f" AND signal_date = ${idx}"
                params.append(signal_date)
                idx += 1
            if status:
                sql += f" AND status = ${idx}"
                params.append(status)
                idx += 1
            sql += f" ORDER BY created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 200), 2000)))
            rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]

    async def list_strategy_paper_trades(
        self,
        strategy_id: str,
        account_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM paper_trades WHERE strategy_id = $1"
            params: list = [strategy_id]
            idx = 2
            if account_id:
                sql += f" AND account_id = ${idx}"
                params.append(account_id)
                idx += 1
            sql += f" ORDER BY trade_time DESC, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 500), 5000)))
            rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]
