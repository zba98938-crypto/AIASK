"""SQLite 适配器 — 信号跟踪 Mixin"""

import json
import logging
import math
import random
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_SIGNAL_STATS_NEUTRAL_EPS = 0.0015
DEFAULT_SIGNAL_STATS_RECENT_WINDOW_DAYS = 20
DEFAULT_SIGNAL_STATS_CONFIDENCE = 0.90
HORIZON_OVERLAP_FACTORS = {
    1: 1.0,
    5: 3.0,
    10: 5.0,
    20: 8.0,
}
_PREDICTIVE_SIGNAL_WHERE = """
AND LOWER(
    COALESCE(
        NULLIF(ss.event_action, ''),
        CASE
            WHEN json_valid(ss.signal_metadata)
            THEN NULLIF(json_extract(ss.signal_metadata, '$.event_action'), '')
            ELSE NULL
        END,
        CASE
            WHEN json_valid(ss.signal_metadata)
            THEN NULLIF(json_extract(ss.signal_metadata, '$.latest_event_action'), '')
            ELSE NULL
        END,
        ''
    )
) NOT IN ('exit', 'close', 'reduce', 'take_profit', 'stop_loss', 'time_stop_exit', 'risk_exit', 'sell_exit')
"""


def _clamp_probability(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return max(0.0, min(float(value), 1.0))


def _round_metric(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _conservative_effective_n(sample_count: int, horizon: int) -> int:
    sample_n = max(int(sample_count or 0), 0)
    if sample_n <= 0:
        return 0
    factor = float(HORIZON_OVERLAP_FACTORS.get(int(horizon or 0), max(int(horizon or 1), 1)))
    if factor <= 0:
        factor = 1.0
    return max(1, min(sample_n, int(sample_n / factor)))


def _wilson_lower_bound(
    p_hat: Optional[float],
    n_eff: float,
    *,
    confidence: float = DEFAULT_SIGNAL_STATS_CONFIDENCE,
) -> Optional[float]:
    if p_hat is None:
        return None
    n = max(float(n_eff or 0.0), 0.0)
    if n <= 0:
        return None
    z = 1.6448536269514722 if confidence >= 0.90 else 1.2815515655446004
    denominator = 1.0 + (z * z) / n
    center = float(p_hat) + (z * z) / (2.0 * n)
    margin = z * math.sqrt(((float(p_hat) * (1.0 - float(p_hat))) + (z * z) / (4.0 * n)) / n)
    return _clamp_probability((center - margin) / denominator)


def _resolve_recent_cutoff(latest_signal_date: Optional[date], lookback_days: Optional[int]) -> Optional[date]:
    if latest_signal_date is None or lookback_days is None:
        return None
    days = max(int(lookback_days or 0), 1)
    return latest_signal_date - timedelta(days=max(days - 1, 0))


def _calc_bucket_stats(
    records: List[Dict[str, Any]],
    horizon: int,
    *,
    eps: float,
    recent_cutoff: Optional[date],
) -> Dict[str, Optional[float]]:
    if not records:
        return {
            "raw_hit_rate": None,
            "hit_rate_lcb": None,
            "null_hit_rate": None,
            "skill_lcb": None,
            "sample_count": 0,
            "effective_n": 0,
            "hit_count": 0,
            "miss_count": 0,
            "neutral_count": 0,
            "recent_hit_rate": None,
            "recent_hit_rate_lcb": None,
            "recent_null_hit_rate": None,
            "recent_skill_lcb": None,
            "recent_sample_count": 0,
            "recent_effective_n": 0,
            "recent_hit_count": 0,
            "recent_miss_count": 0,
            "recent_neutral_count": 0,
            "stability_gap": None,
            "forward_ic": None,
            "forward_sharpe": None,
        }

    import numpy as np

    signals_arr = np.array([float(row.get("signal") or 0.0) for row in records], dtype=float)
    returns_arr = np.array([float(row.get("actual_return") or 0.0) for row in records], dtype=float)
    directed_returns = signals_arr * returns_arr
    hit_mask = directed_returns > eps
    miss_mask = directed_returns < -eps
    decisive_mask = hit_mask | miss_mask

    hit_count = int(np.sum(hit_mask))
    miss_count = int(np.sum(miss_mask))
    sample_count = hit_count + miss_count
    neutral_count = max(len(records) - sample_count, 0)
    raw_hit_rate = _clamp_probability(hit_count / sample_count) if sample_count > 0 else None
    effective_n = _conservative_effective_n(sample_count, horizon)
    hit_rate_lcb = _wilson_lower_bound(raw_hit_rate, min(float(sample_count), float(effective_n)))

    null_hit_rate = None
    skill_lcb = None
    if sample_count > 0:
        decisive_signals = signals_arr[decisive_mask]
        decisive_returns = returns_arr[decisive_mask]
        p_long = float(np.mean(decisive_signals > 0))
        p_up = float(np.mean(decisive_returns > eps))
        null_hit_rate = _clamp_probability((p_long * p_up) + ((1.0 - p_long) * (1.0 - p_up)))
        skill_lcb = None if hit_rate_lcb is None or null_hit_rate is None else float(hit_rate_lcb) - float(null_hit_rate)

    recent_records = records
    if recent_cutoff is not None:
        recent_records = [
            row for row in records
            if isinstance(row.get("signal_date"), date) and row.get("signal_date") >= recent_cutoff
        ]

    recent_signals_arr = np.array([float(row.get("signal") or 0.0) for row in recent_records], dtype=float)
    recent_returns_arr = np.array([float(row.get("actual_return") or 0.0) for row in recent_records], dtype=float)
    recent_directed_returns = recent_signals_arr * recent_returns_arr
    recent_hit_mask = recent_directed_returns > eps
    recent_miss_mask = recent_directed_returns < -eps
    recent_decisive_mask = recent_hit_mask | recent_miss_mask
    recent_hit_count = int(np.sum(recent_hit_mask))
    recent_miss_count = int(np.sum(recent_miss_mask))
    recent_sample_count = recent_hit_count + recent_miss_count
    recent_neutral_count = max(len(recent_records) - recent_sample_count, 0)
    recent_hit_rate = _clamp_probability(recent_hit_count / recent_sample_count) if recent_sample_count > 0 else None
    recent_effective_n = _conservative_effective_n(recent_sample_count, horizon)
    recent_hit_rate_lcb = _wilson_lower_bound(
        recent_hit_rate,
        min(float(recent_sample_count), float(recent_effective_n)),
    )
    recent_null_hit_rate = None
    recent_skill_lcb = None
    if recent_sample_count > 0:
        decisive_recent_signals = recent_signals_arr[recent_decisive_mask]
        decisive_recent_returns = recent_returns_arr[recent_decisive_mask]
        recent_p_long = float(np.mean(decisive_recent_signals > 0))
        recent_p_up = float(np.mean(decisive_recent_returns > eps))
        recent_null_hit_rate = _clamp_probability(
            (recent_p_long * recent_p_up) + ((1.0 - recent_p_long) * (1.0 - recent_p_up))
        )
        recent_skill_lcb = (
            None
            if recent_hit_rate_lcb is None or recent_null_hit_rate is None
            else float(recent_hit_rate_lcb) - float(recent_null_hit_rate)
        )

    stability_gap = None
    if raw_hit_rate is not None and recent_hit_rate is not None:
        stability_gap = abs(float(raw_hit_rate) - float(recent_hit_rate))

    if len(records) >= 5:
        from scipy import stats as sp_stats

        ic, _ = sp_stats.spearmanr(signals_arr, returns_arr)
        forward_ic = float(ic) if not np.isnan(ic) else 0.0
    else:
        forward_ic = 0.0

    directed_mean = float(np.mean(directed_returns))
    directed_std = float(np.std(directed_returns))
    forward_sharpe = directed_mean / directed_std if directed_std > 0 else 0.0

    return {
        "raw_hit_rate": raw_hit_rate,
        "hit_rate_lcb": hit_rate_lcb,
        "null_hit_rate": null_hit_rate,
        "skill_lcb": skill_lcb,
        "sample_count": sample_count,
        "effective_n": effective_n,
        "hit_count": hit_count,
        "miss_count": miss_count,
        "neutral_count": neutral_count,
        "recent_hit_rate": recent_hit_rate,
        "recent_hit_rate_lcb": recent_hit_rate_lcb,
        "recent_null_hit_rate": recent_null_hit_rate,
        "recent_skill_lcb": recent_skill_lcb,
        "recent_sample_count": recent_sample_count,
        "recent_effective_n": recent_effective_n,
        "recent_hit_count": recent_hit_count,
        "recent_miss_count": recent_miss_count,
        "recent_neutral_count": recent_neutral_count,
        "stability_gap": stability_gap,
        "forward_ic": forward_ic,
        "forward_sharpe": forward_sharpe,
    }


class SignalTrackingMixin:
    """前向信号记录与收益验证"""

    async def save_signals(
        self, strategy_id: str, signal_date: date, signals: List[Dict[str, Any]]
    ) -> int:
        """批量写入信号。每个 dict: {code, signal, score, execution_semantic_mode, ...}"""
        if not signals:
            return 0
        async with self.acquire() as conn:
            count = 0
            for s in signals:
                try:
                    await conn.execute(
                        """INSERT INTO strategy_signals (
                               strategy_id,
                               signal_date,
                               code,
                               signal,
                               score,
                               execution_semantic_mode,
                               action_source,
                               event_action,
                               action_reason,
                               signal_metadata
                           )
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                           ON CONFLICT (strategy_id, signal_date, code) DO UPDATE
                           SET signal = EXCLUDED.signal,
                               score = EXCLUDED.score,
                               execution_semantic_mode = EXCLUDED.execution_semantic_mode,
                               action_source = EXCLUDED.action_source,
                               event_action = EXCLUDED.event_action,
                               action_reason = EXCLUDED.action_reason,
                               signal_metadata = EXCLUDED.signal_metadata""",
                        strategy_id, signal_date,
                        str(s["code"]), int(s["signal"]),
                        float(s.get("score") or 0),
                        str(s.get("execution_semantic_mode") or "").strip() or None,
                        str(s.get("action_source") or "").strip() or None,
                        str(s.get("event_action") or "").strip() or None,
                        str(s.get("action_reason") or "").strip() or None,
                        json.dumps(s.get("signal_metadata") or {}, ensure_ascii=False, default=str),
                    )
                    count += 1
                except Exception as e:
                    logger.warning("save_signal error: %s", e)
            return count

    async def get_signals(
        self,
        strategy_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 100,
    ) -> List[dict]:
        """查询信号（实时，无延迟）"""
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_signals WHERE strategy_id = $1"
            params: list = [strategy_id]
            idx = 2
            if start_date:
                sql += f" AND signal_date >= ${idx}"
                params.append(start_date)
                idx += 1
            if end_date:
                sql += f" AND signal_date <= ${idx}"
                params.append(end_date)
                idx += 1
            sql += f" ORDER BY signal_date DESC, code LIMIT ${idx}"
            params.append(limit)
            rows = await conn.fetch(sql, *params)
        results: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            result["signal_metadata"] = self._decode_json_field(result.get("signal_metadata"), {})
            results.append(result)
        return results

    def _decode_strategy_signal_event_snapshot(self, row: dict) -> dict:
        result = dict(row)
        result["recent_events"] = self._decode_json_field(result.get("recent_events"), [])
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        return result

    async def save_strategy_signal_event_snapshot(self, snapshot: dict) -> dict:
        payload = dict(snapshot or {})
        strategy_id = str(payload.get("strategy_id") or "").strip()
        code = str(payload.get("code") or "").strip()
        as_of_date = self._coerce_date(payload.get("as_of_date"))
        if not strategy_id or not code or as_of_date is None:
            raise ValueError("strategy_id/code/as_of_date are required for signal event snapshots")

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_signal_event_snapshots (
                    strategy_id,
                    code,
                    as_of_date,
                    latest_bar_date,
                    latest_bar_signal,
                    execution_semantic_mode,
                    latest_event_index,
                    latest_event_date,
                    latest_event_signal,
                    latest_event_action,
                    latest_event_action_source,
                    latest_event_reason,
                    latest_event_units,
                    latest_entry_date,
                    latest_exit_date,
                    event_count,
                    recent_events,
                    metadata,
                    created_at,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (strategy_id, code, as_of_date) DO UPDATE
                SET latest_bar_date = EXCLUDED.latest_bar_date,
                    latest_bar_signal = EXCLUDED.latest_bar_signal,
                    execution_semantic_mode = EXCLUDED.execution_semantic_mode,
                    latest_event_index = EXCLUDED.latest_event_index,
                    latest_event_date = EXCLUDED.latest_event_date,
                    latest_event_signal = EXCLUDED.latest_event_signal,
                    latest_event_action = EXCLUDED.latest_event_action,
                    latest_event_action_source = EXCLUDED.latest_event_action_source,
                    latest_event_reason = EXCLUDED.latest_event_reason,
                    latest_event_units = EXCLUDED.latest_event_units,
                    latest_entry_date = EXCLUDED.latest_entry_date,
                    latest_exit_date = EXCLUDED.latest_exit_date,
                    event_count = EXCLUDED.event_count,
                    recent_events = EXCLUDED.recent_events,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                strategy_id,
                code,
                as_of_date,
                self._coerce_date(payload.get("latest_bar_date")),
                int(payload.get("latest_bar_signal") or 0),
                str(payload.get("execution_semantic_mode") or "").strip() or None,
                int(payload.get("latest_event_index")) if payload.get("latest_event_index") is not None else None,
                self._coerce_date(payload.get("latest_event_date")),
                int(payload.get("latest_event_signal")) if payload.get("latest_event_signal") is not None else None,
                str(payload.get("latest_event_action") or "").strip() or None,
                str(payload.get("latest_event_action_source") or "").strip() or None,
                str(payload.get("latest_event_reason") or "").strip() or None,
                float(payload.get("latest_event_units")) if payload.get("latest_event_units") is not None else None,
                self._coerce_date(payload.get("latest_entry_date")),
                self._coerce_date(payload.get("latest_exit_date")),
                max(int(payload.get("event_count") or 0), 0),
                json.dumps(payload.get("recent_events") or [], ensure_ascii=False, default=str),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
            )
        return self._decode_strategy_signal_event_snapshot(dict(row))

    async def list_strategy_signal_event_snapshots(
        self,
        strategy_id: Optional[str] = None,
        code: Optional[str] = None,
        as_of_date: Optional[date] = None,
        *,
        latest_only: bool = False,
        limit: int = 20,
    ) -> List[dict]:
        table_name = "strategy_signal_event_snapshots_latest" if latest_only else "strategy_signal_event_snapshots"
        async with self.acquire() as conn:
            sql = f"SELECT * FROM {table_name} WHERE 1=1"
            params: list[Any] = []
            idx = 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(str(strategy_id))
                idx += 1
            if code:
                sql += f" AND code = ${idx}"
                params.append(str(code))
                idx += 1
            if as_of_date is not None:
                sql += f" AND as_of_date = ${idx}"
                params.append(self._coerce_date(as_of_date))
                idx += 1
            sql += f" ORDER BY as_of_date DESC, updated_at DESC, id DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 500)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_strategy_signal_event_snapshot(dict(row)) for row in rows]

    async def get_latest_strategy_signal_event_snapshot(
        self,
        strategy_id: str,
        code: Optional[str] = None,
    ) -> Optional[dict]:
        rows = await self.list_strategy_signal_event_snapshots(
            strategy_id=strategy_id,
            code=code,
            latest_only=True,
            limit=1,
        )
        return rows[0] if rows else None

    async def get_signals_public(
        self, strategy_id: str, limit: int = 100
    ) -> List[dict]:
        """公开 API：信号延迟 1-3 个交易日（IP 保护）"""
        delay_days = random.randint(1, 3)
        cutoff = date.today() - timedelta(days=delay_days)
        return await self.get_signals(strategy_id, end_date=cutoff, limit=limit)

    async def save_forward_returns(
        self, signal_id: int, forward_days: int, actual_return: float
    ) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """INSERT INTO signal_forward_returns (signal_id, forward_days, actual_return)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (signal_id, forward_days) DO UPDATE
                   SET actual_return = EXCLUDED.actual_return, calculated_at = CURRENT_TIMESTAMP""",
                signal_id, forward_days, actual_return,
            )

    async def save_forward_returns_batch(self, rows: List[Dict[str, Any]]) -> int:
        """批量写入前向收益。每个 dict: {signal_id, forward_days, actual_return}"""
        if not rows:
            return 0

        payload = []
        for row in list(rows or []):
            try:
                payload.append(
                    (
                        int(row["signal_id"]),
                        int(row["forward_days"]),
                        float(row["actual_return"]),
                    )
                )
            except Exception as exc:
                logger.warning("save_forward_returns_batch skip invalid row %s: %s", row, exc)

        if not payload:
            return 0

        async with self.acquire() as conn:
            await conn.executemany(
                """INSERT INTO signal_forward_returns (signal_id, forward_days, actual_return)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (signal_id, forward_days) DO UPDATE
                   SET actual_return = EXCLUDED.actual_return, calculated_at = CURRENT_TIMESTAMP""",
                payload,
            )
        return len(payload)

    async def list_signal_forward_returns(
        self,
        strategy_id: str,
        *,
        forward_days: int = 5,
        lookback_days: Optional[int] = None,
        limit: int = 2000,
    ) -> List[dict]:
        """INVERT-DESIGN P3：返回某策略在指定主窗口的原始前向收益序列。

        供 PromotionGate（DSR）消费。按 signal_date 升序，每条 {signal_id, signal_date,
        actual_return}。lookback_days 限定最近窗口（按最新 signal_date 回溯）。
        """
        batch_limit = max(1, min(int(limit or 2000), 20000))
        async with self.acquire() as conn:
            cutoff = None
            if lookback_days and int(lookback_days) > 0:
                latest = await conn.fetchrow(
                    "SELECT MAX(signal_date) AS d FROM strategy_signals WHERE strategy_id = $1",
                    strategy_id,
                )
                latest_date = (latest or {}).get("d")
                if isinstance(latest_date, date):
                    cutoff = latest_date - timedelta(days=int(lookback_days))
            if cutoff is None:
                rows = await conn.fetch(
                    """SELECT ss.id AS signal_id, ss.signal_date, sfr.actual_return
                       FROM strategy_signals ss
                       JOIN signal_forward_returns sfr ON sfr.signal_id = ss.id
                       WHERE ss.strategy_id = $1 AND sfr.forward_days = $2
                         AND sfr.actual_return IS NOT NULL
                       ORDER BY ss.signal_date, ss.id
                       LIMIT $3""",
                    strategy_id, int(forward_days), batch_limit,
                )
            else:
                rows = await conn.fetch(
                    """SELECT ss.id AS signal_id, ss.signal_date, sfr.actual_return
                       FROM strategy_signals ss
                       JOIN signal_forward_returns sfr ON sfr.signal_id = ss.id
                       WHERE ss.strategy_id = $1 AND sfr.forward_days = $2
                         AND ss.signal_date >= $3
                         AND sfr.actual_return IS NOT NULL
                       ORDER BY ss.signal_date, ss.id
                       LIMIT $4""",
                    strategy_id, int(forward_days), cutoff, batch_limit,
                )
        return [dict(r) for r in rows]

    async def get_pending_forward_returns(
        self,
        forward_days: int,
        limit: int = 500,
        after_signal_date: Optional[date] = None,
        after_id: Optional[int] = None,
    ) -> List[dict]:
        """找到 N 天前的信号中尚未计算前向收益的记录"""
        cutoff = date.today() - timedelta(days=forward_days)
        batch_limit = max(1, min(int(limit or 500), 5000))
        cursor_signal_date = after_signal_date
        cursor_id = int(after_id or 0)
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """WITH pending_signals AS (
                       SELECT
                           ss.id,
                           ss.strategy_id,
                           ss.signal_date,
                           ss.code,
                           ss.signal,
                           ss.signal_metadata,
                           COALESCE(
                               CASE
                                   WHEN json_valid(ss.signal_metadata)
                                   THEN NULLIF(json_extract(ss.signal_metadata, '$.latest_bar_date'), '')
                                   ELSE NULL
                               END,
                               CASE
                                   WHEN json_valid(ss.signal_metadata)
                                   THEN NULLIF(json_extract(ss.signal_metadata, '$.latest_nonzero_signal_date'), '')
                                   ELSE NULL
                               END,
                               ss.signal_date
                           ) AS effective_signal_date
                       FROM strategy_signals ss
                   )
                   SELECT ss.id, ss.strategy_id, ss.signal_date, ss.code, ss.signal,
                          ss.signal_metadata, ss.effective_signal_date
                   FROM pending_signals ss
                   WHERE ss.effective_signal_date <= $1
                     AND (
                         $3 IS NULL
                         OR ss.effective_signal_date > $3
                         OR (ss.effective_signal_date = $3 AND ss.id > $4)
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM signal_forward_returns sfr
                         WHERE sfr.signal_id = ss.id AND sfr.forward_days = $2
                     )
                   ORDER BY ss.effective_signal_date, ss.id
                   LIMIT $5""",
                cutoff, forward_days,
                cursor_signal_date, cursor_id, batch_limit,
            )
        return [dict(r) for r in rows]

    async def get_signal_stats(
        self,
        strategy_id: str,
        lookback_days: Optional[int] = None,
        eps: Optional[float] = None,
    ) -> dict:
        """聚合统计：命中率、置信下界、稳定性、前向 IC、前向 Sharpe。"""
        neutral_eps = max(float(eps or DEFAULT_SIGNAL_STATS_NEUTRAL_EPS), 0.0)
        requested_lookback_days = max(int(lookback_days or 0), 1) if lookback_days else None

        async with self.acquire() as conn:
            total_row = await conn.fetchrow(
                """SELECT COUNT(*) AS total_signals, MAX(signal_date) AS latest_signal_date
                   FROM strategy_signals ss
                   WHERE strategy_id = $1""",
                strategy_id,
            )
            latest_signal_date = (total_row or {}).get("latest_signal_date")
            window_cutoff = _resolve_recent_cutoff(latest_signal_date, requested_lookback_days)

            raw_signal_count_query = (
                f"""SELECT COUNT(*) AS total_signals
                   FROM strategy_signals ss
                   WHERE strategy_id = $1
                   {_PREDICTIVE_SIGNAL_WHERE}"""
                if window_cutoff is None
                else
                f"""SELECT COUNT(*) AS total_signals
                   FROM strategy_signals
                   WHERE strategy_id = $1 AND signal_date >= $2
                   {_PREDICTIVE_SIGNAL_WHERE}"""
            )
            raw_signal_count_params = (strategy_id,) if window_cutoff is None else (strategy_id, window_cutoff)
            raw_count_row = await conn.fetchrow(raw_signal_count_query, *raw_signal_count_params)

            rows_query = (
                f"""SELECT ss.id AS signal_id, ss.signal_date, ss.signal, sfr.forward_days, sfr.actual_return
                   FROM strategy_signals ss
                   JOIN signal_forward_returns sfr ON sfr.signal_id = ss.id
                   WHERE ss.strategy_id = $1
                   {_PREDICTIVE_SIGNAL_WHERE}
                   ORDER BY ss.signal_date, sfr.forward_days"""
                if window_cutoff is None
                else
                f"""SELECT ss.id AS signal_id, ss.signal_date, ss.signal, sfr.forward_days, sfr.actual_return
                   FROM strategy_signals ss
                   JOIN signal_forward_returns sfr ON sfr.signal_id = ss.id
                   WHERE ss.strategy_id = $1
                     AND ss.signal_date >= $2
                   {_PREDICTIVE_SIGNAL_WHERE}
                   ORDER BY ss.signal_date, sfr.forward_days"""
            )
            rows_params = (strategy_id,) if window_cutoff is None else (strategy_id, window_cutoff)
            rows = await conn.fetch(rows_query, *rows_params)

        raw_signal_count = int((raw_count_row or {}).get("total_signals") or 0)
        lifetime_signal_count = int((total_row or {}).get("total_signals") or 0)
        signals_with_forward_returns_count = len({int(r["signal_id"]) for r in rows}) if rows else 0
        observed_forward_return_count = len(rows)

        result = {
            "hit_rate": {},
            "hit_rate_lcb": {},
            "null_hit_rate": {},
            "skill_lcb": {},
            "recent_hit_rate": {},
            "recent_hit_rate_lcb": {},
            "recent_null_hit_rate": {},
            "recent_skill_lcb": {},
            "stability_gap": {},
            "sample_count": {},
            "effective_n": {},
            "neutral_count": {},
            "hit_count": {},
            "miss_count": {},
            "recent_sample_count": {},
            "recent_effective_n": {},
            "recent_neutral_count": {},
            "recent_hit_count": {},
            "recent_miss_count": {},
            "forward_ic": {},
            "forward_sharpe": {},
            "by_horizon": {},
            "total_signals": raw_signal_count,
            "raw_signal_count": raw_signal_count,
            "signals_with_forward_returns_count": signals_with_forward_returns_count,
            "observed_forward_return_count": observed_forward_return_count,
            "coverage_ratio": round(
                (signals_with_forward_returns_count / raw_signal_count),
                4,
            ) if raw_signal_count > 0 else 0.0,
            "hit_rate_lcb_method": "wilson_ess_approx",
            "effective_n_method": "overlap_adjusted_ess_v1",
            "recent_window_days": min(
                DEFAULT_SIGNAL_STATS_RECENT_WINDOW_DAYS,
                requested_lookback_days or DEFAULT_SIGNAL_STATS_RECENT_WINDOW_DAYS,
            ),
            "requested_lookback_days": requested_lookback_days,
            "neutral_band_epsilon": round(neutral_eps, 6),
            "lifetime_total_signals": lifetime_signal_count,
            "window_start_date": window_cutoff.isoformat() if window_cutoff else None,
            "window_end_date": latest_signal_date.isoformat() if isinstance(latest_signal_date, date) else None,
        }

        if not rows:
            return result

        by_days: Dict[int, list] = {}
        latest_available_signal_date = None
        for raw_row in rows:
            row = dict(raw_row)
            fd = int(row["forward_days"])
            signal_date = row.get("signal_date")
            if isinstance(signal_date, date):
                latest_available_signal_date = max(latest_available_signal_date, signal_date) if latest_available_signal_date else signal_date
            by_days.setdefault(fd, []).append(row)

        recent_window_cutoff = _resolve_recent_cutoff(
            latest_available_signal_date,
            min(
                DEFAULT_SIGNAL_STATS_RECENT_WINDOW_DAYS,
                requested_lookback_days or DEFAULT_SIGNAL_STATS_RECENT_WINDOW_DAYS,
            ),
        )

        for fd, records in sorted(by_days.items()):
            bucket = _calc_bucket_stats(
                records,
                fd,
                eps=neutral_eps,
                recent_cutoff=recent_window_cutoff,
            )
            result["hit_rate"][fd] = _round_metric(bucket["raw_hit_rate"])
            result["hit_rate_lcb"][fd] = _round_metric(bucket["hit_rate_lcb"])
            result["null_hit_rate"][fd] = _round_metric(bucket["null_hit_rate"])
            result["skill_lcb"][fd] = _round_metric(bucket["skill_lcb"])
            result["recent_hit_rate"][fd] = _round_metric(bucket["recent_hit_rate"])
            result["recent_hit_rate_lcb"][fd] = _round_metric(bucket["recent_hit_rate_lcb"])
            result["recent_null_hit_rate"][fd] = _round_metric(bucket["recent_null_hit_rate"])
            result["recent_skill_lcb"][fd] = _round_metric(bucket["recent_skill_lcb"])
            result["stability_gap"][fd] = _round_metric(bucket["stability_gap"])
            result["sample_count"][fd] = int(bucket["sample_count"] or 0)
            result["effective_n"][fd] = int(bucket["effective_n"] or 0)
            result["neutral_count"][fd] = int(bucket["neutral_count"] or 0)
            result["hit_count"][fd] = int(bucket["hit_count"] or 0)
            result["miss_count"][fd] = int(bucket["miss_count"] or 0)
            result["recent_sample_count"][fd] = int(bucket["recent_sample_count"] or 0)
            result["recent_effective_n"][fd] = int(bucket["recent_effective_n"] or 0)
            result["recent_neutral_count"][fd] = int(bucket["recent_neutral_count"] or 0)
            result["recent_hit_count"][fd] = int(bucket["recent_hit_count"] or 0)
            result["recent_miss_count"][fd] = int(bucket["recent_miss_count"] or 0)
            result["forward_ic"][fd] = _round_metric(bucket["forward_ic"])
            result["forward_sharpe"][fd] = _round_metric(bucket["forward_sharpe"])
            result["by_horizon"][str(fd)] = {
                "hit_rate": result["hit_rate"][fd],
                "hit_rate_lcb": result["hit_rate_lcb"][fd],
                "null_hit_rate": result["null_hit_rate"][fd],
                "skill_lcb": result["skill_lcb"][fd],
                "recent_hit_rate": result["recent_hit_rate"][fd],
                "recent_hit_rate_lcb": result["recent_hit_rate_lcb"][fd],
                "recent_skill_lcb": result["recent_skill_lcb"][fd],
                "stability_gap": result["stability_gap"][fd],
                "sample_count": result["sample_count"][fd],
                "effective_n": result["effective_n"][fd],
                "neutral_count": result["neutral_count"][fd],
                "hit_count": result["hit_count"][fd],
                "miss_count": result["miss_count"][fd],
                "recent_sample_count": result["recent_sample_count"][fd],
                "recent_effective_n": result["recent_effective_n"][fd],
                "recent_neutral_count": result["recent_neutral_count"][fd],
                "forward_ic": result["forward_ic"][fd],
                "forward_sharpe": result["forward_sharpe"][fd],
            }

        return result

    async def is_subscribed(self, strategy_id: str, user_id: str) -> bool:
        """检查用户是否订阅了策略"""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT 1 FROM strategy_subscriptions
                   WHERE strategy_id = $1 AND user_id = $2 AND status = 'active'""",
                strategy_id, user_id,
            )
        return row is not None
