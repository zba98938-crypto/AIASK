"""SQLite persistence for frozen trade prediction contracts and outcomes."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .strategy_factory_json_budget import bounded_json_text, strategy_json_field_max_bytes


def _string(value: Any) -> str:
    return str(value or "").strip()


def _coerce_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    token = _string(value)
    return token or None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _stable_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_payload_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    coerced = _coerce_float(value)
    return default if coerced is None else coerced


def _increment(counter: dict[str, int], key: Any) -> None:
    token = _string(key) or "unknown"
    counter[token] = counter.get(token, 0) + 1


def _bucket_score(value: Any) -> str:
    score = _coerce_float(value)
    if score is None:
        return "missing"
    if score >= 0.8:
        return "0.80-1.00"
    if score >= 0.6:
        return "0.60-0.79"
    if score >= 0.4:
        return "0.40-0.59"
    if score >= 0.2:
        return "0.20-0.39"
    return "0.00-0.19"


def _dimension_values(prediction: dict, outcome: dict | None, dimension: str) -> list[str]:
    sources = [
        prediction.get("metadata") or {},
        prediction.get("contract_json") or {},
        (outcome or {}).get("metadata") or {},
        (outcome or {}).get("outcome_json") or {},
    ]
    aliases = {
        "family": ("family", "strategy_family", "strategy_type"),
        "stage": ("stage", "incubation_stage", "pipeline_stage"),
        "regime": ("regime", "market_regime", "profile_regime"),
        "event": ("event", "event_family", "event_type", "theme_event"),
        "factor": ("factor", "factor_family", "factor_name"),
    }
    values: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in aliases.get(dimension, (dimension,)):
            value = source.get(key)
            if value in (None, "", []):
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(_string(item) for item in value if _string(item))
            else:
                values.append(_string(value))
    unique = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique or ["unknown"]


def _latest_by_prediction(outcomes: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for outcome in outcomes:
        prediction_id = _string(outcome.get("prediction_id"))
        if not prediction_id:
            continue
        previous = latest.get(prediction_id)
        if previous is None:
            latest[prediction_id] = outcome
            continue
        previous_ts = _string(previous.get("calculated_at") or previous.get("created_at"))
        current_ts = _string(outcome.get("calculated_at") or outcome.get("created_at"))
        if current_ts >= previous_ts:
            latest[prediction_id] = outcome
    return latest


class StrategyTradePredictionMixin:
    def _decode_trade_prediction(self, row: dict) -> dict:
        payload = dict(row)
        payload["contract_json"] = self._decode_json_field(payload.get("contract_json"), {})
        payload["metadata"] = self._decode_json_field(payload.get("metadata"), {})
        return payload

    def _decode_trade_prediction_outcome(self, row: dict) -> dict:
        payload = dict(row)
        payload["outcome_json"] = self._decode_json_field(payload.get("outcome_json"), {})
        payload["metadata"] = self._decode_json_field(payload.get("metadata"), {})
        return payload

    async def save_strategy_trade_prediction(self, payload: dict) -> dict:
        contract_json = payload.get("contract_json") or payload.get("contract") or payload.get("trade_prediction_contract") or {}
        if isinstance(contract_json, str):
            try:
                contract_json = json.loads(contract_json)
            except Exception:
                contract_json = {"raw": contract_json}
        if not isinstance(contract_json, dict):
            contract_json = {"value": contract_json}

        prediction_id = _string(payload.get("prediction_id"))
        contract_hash = _string(payload.get("contract_hash") or contract_json.get("contract_hash"))
        if not contract_hash:
            contract_hash = _stable_payload_hash(contract_json)
        if not prediction_id:
            prediction_id = f"tp_{contract_hash[:20]}" if contract_hash else f"tp_{uuid4().hex}"

        strategy_id = _string(payload.get("strategy_id") or contract_json.get("strategy_id"))
        stock_code = _string(payload.get("stock_code") or contract_json.get("stock_code"))
        prediction_as_of = _coerce_iso(payload.get("prediction_as_of") or contract_json.get("prediction_as_of"))
        target_trading_date = _coerce_iso(payload.get("target_trading_date") or contract_json.get("target_trading_date"))
        direction = _string(payload.get("direction") or contract_json.get("direction"))
        contract_version = _string(payload.get("contract_version") or contract_json.get("contract_version"))
        if not strategy_id:
            raise ValueError("strategy_id is required")
        if not stock_code:
            raise ValueError("stock_code is required")
        if not prediction_as_of:
            raise ValueError("prediction_as_of is required")
        if not target_trading_date:
            raise ValueError("target_trading_date is required")
        if not direction:
            raise ValueError("direction is required")
        if not contract_version:
            raise ValueError("contract_version is required")

        now = datetime.now(timezone.utc)
        metadata = payload.get("metadata") or {}
        async with self.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT prediction_id, contract_hash
                FROM strategy_trade_predictions
                WHERE prediction_id = $1
                """,
                prediction_id,
            )
            if existing and _string(dict(existing).get("contract_hash")) != contract_hash:
                raise ValueError("frozen trade prediction contract_hash mismatch")
            await conn.execute(
                """
                INSERT INTO strategy_trade_predictions (
                    prediction_id,
                    strategy_id,
                    stock_code,
                    prediction_as_of,
                    target_trading_date,
                    direction,
                    confidence,
                    horizon,
                    contract_version,
                    contract_source,
                    contract_hash,
                    contract_json,
                    prediction_status,
                    metadata,
                    created_at,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $15)
                ON CONFLICT (prediction_id) DO UPDATE SET
                    strategy_id = EXCLUDED.strategy_id,
                    stock_code = EXCLUDED.stock_code,
                    prediction_as_of = EXCLUDED.prediction_as_of,
                    target_trading_date = EXCLUDED.target_trading_date,
                    direction = EXCLUDED.direction,
                    confidence = EXCLUDED.confidence,
                    horizon = EXCLUDED.horizon,
                    contract_version = EXCLUDED.contract_version,
                    contract_source = EXCLUDED.contract_source,
                    prediction_status = EXCLUDED.prediction_status,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                prediction_id,
                strategy_id,
                stock_code,
                prediction_as_of,
                target_trading_date,
                direction,
                _coerce_float(payload.get("confidence") if payload.get("confidence") is not None else contract_json.get("confidence")),
                _string(payload.get("horizon") or contract_json.get("horizon")) or None,
                contract_version,
                _string(payload.get("contract_source") or contract_json.get("contract_source") or "explicit"),
                contract_hash,
                bounded_json_text(
                    "strategy_trade_predictions.contract_json",
                    contract_json,
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                _string(payload.get("prediction_status") or "pending") or "pending",
                bounded_json_text(
                    "strategy_trade_predictions.metadata",
                    metadata,
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                now,
            )
            row = await conn.fetchrow(
                "SELECT * FROM strategy_trade_predictions WHERE prediction_id = $1",
                prediction_id,
            )
        return self._decode_trade_prediction(dict(row))

    async def get_strategy_trade_prediction(self, prediction_id: str) -> Optional[dict]:
        token = _string(prediction_id)
        if not token:
            return None
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM strategy_trade_predictions WHERE prediction_id = $1",
                token,
            )
        return self._decode_trade_prediction(dict(row)) if row else None

    async def list_strategy_trade_predictions(
        self,
        *,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        prediction_status: str | None = None,
        target_trading_date_lte: Any | None = None,
        target_trading_date_gte: Any | None = None,
        exclude_outcome_score_version: str | None = None,
        pending_for_outcome: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        where = ["1=1"]
        params: list[Any] = []
        if strategy_id:
            params.append(strategy_id)
            where.append(f"strategy_id = ${len(params)}")
        if stock_code:
            params.append(stock_code)
            where.append(f"stock_code = ${len(params)}")
        if prediction_status:
            params.append(prediction_status)
            where.append(f"prediction_status = ${len(params)}")
        if target_trading_date_lte:
            params.append(_coerce_iso(target_trading_date_lte))
            where.append(f"date(target_trading_date) <= date(${len(params)})")
        if target_trading_date_gte:
            params.append(_coerce_iso(target_trading_date_gte))
            where.append(f"date(target_trading_date) >= date(${len(params)})")
        if pending_for_outcome:
            where.append("prediction_status IN ('pending', 'frozen', 'ready')")
        if exclude_outcome_score_version:
            params.append(exclude_outcome_score_version)
            where.append(
                f"""
                NOT EXISTS (
                    SELECT 1
                    FROM strategy_trade_prediction_outcomes o
                    WHERE o.prediction_id = strategy_trade_predictions.prediction_id
                      AND o.score_version = ${len(params)}
                )
                """
            )
        try:
            limit_value = max(1, min(int(limit or 200), 1000))
        except Exception:
            limit_value = 200
        params.append(limit_value)
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM strategy_trade_predictions
                WHERE {" AND ".join(where)}
                ORDER BY datetime(target_trading_date) DESC, datetime(updated_at) DESC
                LIMIT ${len(params)}
                """,
                *params,
            )
        return [self._decode_trade_prediction(dict(row)) for row in rows]

    async def save_strategy_trade_prediction_outcome(self, payload: dict) -> dict:
        prediction_id = _string(payload.get("prediction_id"))
        if not prediction_id:
            raise ValueError("prediction_id is required")
        outcome_json = payload.get("outcome_json") or payload.get("outcome") or {}
        if isinstance(outcome_json, str):
            try:
                outcome_json = json.loads(outcome_json)
            except Exception:
                outcome_json = {"raw": outcome_json}
        if not isinstance(outcome_json, dict):
            outcome_json = {"value": outcome_json}

        strategy_id = _string(payload.get("strategy_id") or outcome_json.get("strategy_id"))
        stock_code = _string(payload.get("stock_code") or outcome_json.get("stock_code"))
        async with self.acquire() as conn:
            if not strategy_id or not stock_code:
                prediction = await conn.fetchrow(
                    """
                    SELECT strategy_id, stock_code
                    FROM strategy_trade_predictions
                    WHERE prediction_id = $1
                    """,
                    prediction_id,
                )
                if prediction:
                    prediction_payload = dict(prediction)
                    strategy_id = strategy_id or _string(prediction_payload.get("strategy_id"))
                    stock_code = stock_code or _string(prediction_payload.get("stock_code"))
            if not strategy_id:
                raise ValueError("strategy_id is required")
            if not stock_code:
                raise ValueError("stock_code is required")
            score_version = _string(payload.get("score_version") or outcome_json.get("score_version"))
            if not score_version:
                raise ValueError("score_version is required")
            outcome_id = _string(payload.get("outcome_id"))
            if not outcome_id:
                outcome_id = f"tpo_{_stable_payload_hash([prediction_id, score_version])[:20]}"
            await conn.execute(
                """
                INSERT INTO strategy_trade_prediction_outcomes (
                    outcome_id,
                    prediction_id,
                    strategy_id,
                    stock_code,
                    actual_trading_date,
                    score_version,
                    score_status,
                    trade_prediction_score,
                    outcome_json,
                    data_quality_status,
                    metadata,
                    calculated_at,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (outcome_id) DO UPDATE SET
                    prediction_id = EXCLUDED.prediction_id,
                    strategy_id = EXCLUDED.strategy_id,
                    stock_code = EXCLUDED.stock_code,
                    actual_trading_date = EXCLUDED.actual_trading_date,
                    score_version = EXCLUDED.score_version,
                    score_status = EXCLUDED.score_status,
                    trade_prediction_score = EXCLUDED.trade_prediction_score,
                    outcome_json = EXCLUDED.outcome_json,
                    data_quality_status = EXCLUDED.data_quality_status,
                    metadata = EXCLUDED.metadata,
                    calculated_at = EXCLUDED.calculated_at
                """,
                outcome_id,
                prediction_id,
                strategy_id,
                stock_code,
                _coerce_iso(payload.get("actual_trading_date") or outcome_json.get("actual_trading_date")),
                score_version,
                _string(payload.get("score_status") or outcome_json.get("score_status") or "pending") or "pending",
                _coerce_float(payload.get("trade_prediction_score") if payload.get("trade_prediction_score") is not None else outcome_json.get("trade_prediction_score")),
                bounded_json_text(
                    "strategy_trade_prediction_outcomes.outcome_json",
                    outcome_json,
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                _string(payload.get("data_quality_status") or outcome_json.get("data_quality_status") or "unknown") or "unknown",
                bounded_json_text(
                    "strategy_trade_prediction_outcomes.metadata",
                    payload.get("metadata") or {},
                    max_bytes=strategy_json_field_max_bytes(),
                ),
                _coerce_iso(payload.get("calculated_at")) or datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )
            row = await conn.fetchrow(
                "SELECT * FROM strategy_trade_prediction_outcomes WHERE outcome_id = $1",
                outcome_id,
            )
        return self._decode_trade_prediction_outcome(dict(row))

    async def list_strategy_trade_prediction_outcomes(
        self,
        *,
        prediction_id: str | None = None,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        score_version: str | None = None,
        score_status: str | None = None,
        data_quality_status: str | None = None,
        actual_trading_date_lte: Any | None = None,
        actual_trading_date_gte: Any | None = None,
        limit: int = 200,
    ) -> list[dict]:
        where = ["1=1"]
        params: list[Any] = []
        if prediction_id:
            params.append(prediction_id)
            where.append(f"prediction_id = ${len(params)}")
        if strategy_id:
            params.append(strategy_id)
            where.append(f"strategy_id = ${len(params)}")
        if stock_code:
            params.append(stock_code)
            where.append(f"stock_code = ${len(params)}")
        if score_version:
            params.append(score_version)
            where.append(f"score_version = ${len(params)}")
        if score_status:
            params.append(score_status)
            where.append(f"score_status = ${len(params)}")
        if data_quality_status:
            params.append(data_quality_status)
            where.append(f"data_quality_status = ${len(params)}")
        if actual_trading_date_lte:
            params.append(_coerce_iso(actual_trading_date_lte))
            where.append(f"date(actual_trading_date) <= date(${len(params)})")
        if actual_trading_date_gte:
            params.append(_coerce_iso(actual_trading_date_gte))
            where.append(f"date(actual_trading_date) >= date(${len(params)})")
        try:
            limit_value = max(1, min(int(limit or 200), 1000))
        except Exception:
            limit_value = 200
        params.append(limit_value)
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM strategy_trade_prediction_outcomes
                WHERE {" AND ".join(where)}
                ORDER BY datetime(COALESCE(actual_trading_date, calculated_at, created_at)) DESC
                LIMIT ${len(params)}
                """,
                *params,
            )
        return [self._decode_trade_prediction_outcome(dict(row)) for row in rows]

    async def summarize_strategy_trade_predictions(
        self,
        *,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        limit: int = 1000,
    ) -> dict:
        predictions = await self.list_strategy_trade_predictions(
            strategy_id=strategy_id,
            stock_code=stock_code,
            limit=limit,
        )
        outcomes = await self.list_strategy_trade_prediction_outcomes(
            strategy_id=strategy_id,
            stock_code=stock_code,
            limit=limit,
        )
        latest = _latest_by_prediction(outcomes)
        prediction_status_counts: dict[str, int] = {}
        score_status_counts: dict[str, int] = {}
        score_version_counts: dict[str, int] = {}
        data_quality_counts: dict[str, int] = {}
        score_buckets: dict[str, int] = {}
        score_values: list[float] = []
        evaluated_prediction_ids = set()
        partial_statuses = {
            "partial_daily_only",
            "partial_intraday_missing",
            "insufficient_samples",
            "post_hoc_rejected",
        }
        partial_count = 0

        for prediction in predictions:
            _increment(prediction_status_counts, prediction.get("prediction_status"))
        for outcome in outcomes:
            _increment(score_status_counts, outcome.get("score_status"))
            _increment(score_version_counts, outcome.get("score_version"))
            _increment(data_quality_counts, outcome.get("data_quality_status"))
            _increment(score_buckets, _bucket_score(outcome.get("trade_prediction_score")))
            score = _coerce_float(outcome.get("trade_prediction_score"))
            if score is not None:
                score_values.append(score)
            if _string(outcome.get("score_status")) in partial_statuses:
                partial_count += 1
            prediction_id = _string(outcome.get("prediction_id"))
            if prediction_id:
                evaluated_prediction_ids.add(prediction_id)

        latest_score_status_counts: dict[str, int] = {}
        latest_data_quality_counts: dict[str, int] = {}
        for outcome in latest.values():
            _increment(latest_score_status_counts, outcome.get("score_status"))
            _increment(latest_data_quality_counts, outcome.get("data_quality_status"))

        return {
            "object": "trade_prediction.status",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "prediction_count": len(predictions),
            "outcome_count": len(outcomes),
            "sample_n": len(evaluated_prediction_ids),
            "pending_count": max(0, len(predictions) - len(evaluated_prediction_ids)),
            "evaluated_count": len(evaluated_prediction_ids),
            "partial_count": partial_count,
            "prediction_status_counts": prediction_status_counts,
            "score_status_counts": score_status_counts,
            "latest_score_status_counts": latest_score_status_counts,
            "score_version_counts": score_version_counts,
            "data_quality_status_counts": data_quality_counts,
            "latest_data_quality_status_counts": latest_data_quality_counts,
            "score_distribution": score_buckets,
            "score_summary": {
                "avg": round(sum(score_values) / len(score_values), 6) if score_values else None,
                "min": min(score_values) if score_values else None,
                "max": max(score_values) if score_values else None,
            },
        }

    async def aggregate_trade_prediction_matrix(
        self,
        *,
        strategy_id: str | None = None,
        stock_code: str | None = None,
        score_version: str | None = None,
        dimensions: list[str] | tuple[str, ...] | None = None,
        limit: int = 1000,
    ) -> dict:
        dimensions = list(dimensions or ["family", "stage", "regime", "event", "factor"])
        predictions = await self.list_strategy_trade_predictions(
            strategy_id=strategy_id,
            stock_code=stock_code,
            limit=limit,
        )
        outcomes = await self.list_strategy_trade_prediction_outcomes(
            strategy_id=strategy_id,
            stock_code=stock_code,
            score_version=score_version,
            limit=limit,
        )
        predictions_by_id = {_string(item.get("prediction_id")): item for item in predictions}
        cells: dict[tuple[str, str], dict] = {}
        for outcome in outcomes:
            prediction = predictions_by_id.get(_string(outcome.get("prediction_id"))) or {}
            for dimension in dimensions:
                for value in _dimension_values(prediction, outcome, dimension):
                    key = (dimension, value)
                    cell = cells.setdefault(
                        key,
                        {
                            "dimension": dimension,
                            "value": value,
                            "sample_n": 0,
                            "score_sum": 0.0,
                            "score_n": 0,
                            "direction_hit_n": 0,
                            "target_touch_n": 0,
                            "status_counts": {},
                            "data_quality_status_counts": {},
                        },
                    )
                    cell["sample_n"] += 1
                    score = _coerce_float(outcome.get("trade_prediction_score"))
                    if score is not None:
                        cell["score_sum"] += score
                        cell["score_n"] += 1
                    outcome_json = outcome.get("outcome_json") or {}
                    if bool(outcome_json.get("direction_hit")):
                        cell["direction_hit_n"] += 1
                    if bool(outcome_json.get("target_touch")):
                        cell["target_touch_n"] += 1
                    _increment(cell["status_counts"], outcome.get("score_status"))
                    _increment(cell["data_quality_status_counts"], outcome.get("data_quality_status"))

        rows: list[dict] = []
        for cell in cells.values():
            sample_n = _safe_int(cell.get("sample_n"))
            score_n = _safe_int(cell.get("score_n"))
            score_avg = cell["score_sum"] / score_n if score_n else None
            hit_rate = cell["direction_hit_n"] / sample_n if sample_n else None
            lcb = None
            if score_avg is not None and sample_n > 0:
                lcb = max(0.0, score_avg - 1.96 * ((score_avg * (1.0 - score_avg)) / sample_n) ** 0.5)
            rows.append(
                {
                    "dimension": cell["dimension"],
                    "value": cell["value"],
                    "sample_n": sample_n,
                    "score_avg": round(score_avg, 6) if score_avg is not None else None,
                    "score_lcb_95": round(lcb, 6) if lcb is not None else None,
                    "direction_hit_rate": round(hit_rate, 6) if hit_rate is not None else None,
                    "target_touch_rate": round(cell["target_touch_n"] / sample_n, 6) if sample_n else None,
                    "score_status_counts": dict(cell["status_counts"]),
                    "data_quality_status_counts": dict(cell["data_quality_status_counts"]),
                }
            )
        rows.sort(key=lambda item: (item["dimension"], -item["sample_n"], item["value"]))
        return {
            "object": "trade_prediction.matrix",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "score_version": score_version,
            "dimensions": dimensions,
            "rows": rows,
            "row_count": len(rows),
        }
