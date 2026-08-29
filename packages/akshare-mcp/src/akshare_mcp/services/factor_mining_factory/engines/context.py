"""DB-backed mining context for factor search engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


MIN_VALIDATION_UNIVERSE_SIZE = 120
TARGET_VALIDATION_UNIVERSE_SIZE = 300
MIN_UNIVERSE_BARS = 500
MAX_LATEST_DATE_LAG_DAYS = 3
MAX_MISSING_ROW_RATIO = 0.05


@dataclass
class MiningContext:
    """Context required by factor mining engines."""

    codes: list[str] = field(default_factory=list)
    validation_codes: list[str] = field(default_factory=list)
    active_pool_size: int = 0
    active_pool_families: dict[str, int] = field(default_factory=dict)
    pool_decay_rate: float = 0.0
    seed_factors: list[dict[str, Any]] = field(default_factory=list)
    success_patterns: list[dict[str, Any]] = field(default_factory=list)
    failure_patterns: list[dict[str, Any]] = field(default_factory=list)
    market_regime: str = "unknown"
    state_features: dict[str, float] = field(default_factory=dict)
    data_source: str = "sqlite"
    data_warnings: list[str] = field(default_factory=list)
    validation_universe_health: dict[str, Any] = field(default_factory=dict)
    alpha_blueprints: list[dict[str, Any]] = field(default_factory=list)
    failed_pattern_memory: list[dict[str, Any]] = field(default_factory=list)
    successful_pattern_memory: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    async def build(
        cls,
        *,
        db: Any,
        codes: list[str] | None = None,
        active_pool: Optional[Any] = None,
    ) -> "MiningContext":
        """Build context from SQLite only."""
        from ...factor_candidate_seed import _EXPRESSION_BY_FACTOR

        warnings: list[str] = []
        requested_codes = [
            str(code).strip()
            for code in list(codes or [])
            if str(code).strip()
        ]
        universe_rows, health = await cls._load_validation_universe(
            db,
            requested_codes=requested_codes,
        )
        if not universe_rows:
            warnings.append("healthy_validation_universe_empty")

        resolved_codes = list(
            dict.fromkeys(
                str((row or {}).get("code") or "").strip()
                for row in universe_rows
                if str((row or {}).get("code") or "").strip()
            )
        )
        if not resolved_codes:
            warnings.append("no_stock_universe_in_db")

        validation_codes = cls._stratified_sample(universe_rows)
        if len(validation_codes) < MIN_VALIDATION_UNIVERSE_SIZE:
            warnings.append("validation_universe_insufficient")
        health["selected_validation_count"] = len(validation_codes)

        seed_factors = []
        for name, (expr, inputs) in list(_EXPRESSION_BY_FACTOR.items())[:10]:
            seed_factors.append(
                {
                    "name": name,
                    "expression_dsl": expr,
                    "inputs": inputs,
                    "family": name.split("_")[0] if "_" in name else name,
                }
            )

        pool_size = 0
        pool_families: dict[str, int] = {}
        decay_rate = 0.0
        if active_pool is not None:
            pool_size = active_pool.size
            pool_families = active_pool.family_distribution
            decay_rate = active_pool.avg_decay_rate

        from ..blueprints import AlphaBlueprintLibrary

        alpha_blueprints = AlphaBlueprintLibrary().build_context_blueprints()

        return cls(
            codes=resolved_codes,
            validation_codes=validation_codes,
            active_pool_size=pool_size,
            active_pool_families=pool_families,
            pool_decay_rate=decay_rate,
            seed_factors=seed_factors,
            market_regime="unknown",
            data_warnings=warnings,
            validation_universe_health=health,
            alpha_blueprints=alpha_blueprints,
        )

    @classmethod
    async def _load_validation_universe(
        cls,
        db: Any,
        *,
        requested_codes: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = await cls._fetch_kline_health_rows(db)
        if not rows:
            return [], {
                "eligible_count": 0,
                "latest_date": None,
                "avg_bars": 0.0,
                "reject_reasons": {"health_query_unavailable": 1},
            }

        main_date = cls._main_trade_date(rows)
        requested = set(requested_codes)
        seen_codes = {str(row.get("code") or "").strip() for row in rows if row.get("code")}
        eligible: list[dict[str, Any]] = []
        reject_reasons: dict[str, int] = {}
        total_bars = 0
        total_missing_ratio = 0.0
        latest_coverage_count = 0

        if requested:
            missing_requested = len([code for code in requested if code not in seen_codes])
            if missing_requested:
                reject_reasons["missing_kline_history"] = missing_requested

        for row in rows:
            code = str(row.get("code") or "").strip()
            if requested and code not in requested:
                continue
            bars = cls._safe_int(row.get("bars"))
            latest = cls._parse_date(row.get("latest_date"))
            missing_ratio = cls._safe_float(row.get("missing_ratio"))
            reason = ""
            if bars < MIN_UNIVERSE_BARS:
                reason = "bars_below_min"
            elif not latest or not main_date:
                reason = "latest_date_missing"
            elif abs((main_date - latest).days) > MAX_LATEST_DATE_LAG_DAYS:
                reason = "latest_date_stale"
            elif missing_ratio > MAX_MISSING_ROW_RATIO:
                reason = "field_missing_ratio_high"

            if reason:
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
                continue

            total_bars += bars
            total_missing_ratio += missing_ratio
            if latest and main_date and latest == main_date:
                latest_coverage_count += 1
            eligible.append(
                {
                    "code": code,
                    "industry": row.get("industry") or "",
                    "market": row.get("market") or "",
                    "market_cap": cls._safe_float(row.get("market_cap")),
                    "bars": bars,
                    "latest_date": latest.isoformat(),
                    "missing_ratio": missing_ratio,
                }
            )

        eligible.sort(
            key=lambda item: (
                str(item.get("industry") or ""),
                str(item.get("market") or ""),
                -float(item.get("market_cap") or 0.0),
                str(item.get("code") or ""),
            )
        )
        health = {
            "eligible_count": len(eligible),
            "latest_date": main_date.isoformat() if main_date else None,
            "latest_date_coverage": round(
                latest_coverage_count / max(1, len(eligible)),
                4,
            ) if eligible else 0.0,
            "avg_bars": round(total_bars / len(eligible), 4) if eligible else 0.0,
            "avg_field_coverage": round(
                1.0 - (total_missing_ratio / len(eligible)),
                4,
            ) if eligible else 0.0,
            "reject_reasons": reject_reasons,
            "total_codes_seen": len(rows),
            "requested_count": len(requested),
            "min_required": MIN_VALIDATION_UNIVERSE_SIZE,
            "target_count": TARGET_VALIDATION_UNIVERSE_SIZE,
            "min_bars": MIN_UNIVERSE_BARS,
        }
        return eligible, health

    @classmethod
    async def _fetch_kline_health_rows(cls, db: Any) -> list[dict[str, Any]]:
        sql = """
            SELECT
                k.code AS code,
                COUNT(*) AS bars,
                MIN(substr(k.time, 1, 10)) AS first_date,
                MAX(substr(k.time, 1, 10)) AS latest_date,
                AVG(
                    CASE
                        WHEN k.open IS NULL OR k.high IS NULL OR k.low IS NULL
                          OR k.close IS NULL OR k.volume IS NULL OR k.amount IS NULL
                        THEN 1.0 ELSE 0.0
                    END
                ) AS missing_ratio,
                MAX(s.industry) AS industry,
                MAX(s.market) AS market,
                MAX(s.market_cap) AS market_cap
            FROM kline_1d k
            LEFT JOIN stocks s ON s.stock_code = k.code
            WHERE k.code NOT LIKE '920%' AND k.code NOT LIKE '200%'
              AND k.code NOT LIKE '900%' AND k.code NOT LIKE '8%'
            GROUP BY k.code
        """
        try:
            if hasattr(db, "acquire"):
                async with db.acquire() as conn:
                    rows = await conn.fetch(sql)
                return [dict(row) for row in rows or []]
            raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
            if raw_conn is not None:
                cursor = raw_conn.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception:
            return []
        return []

    @staticmethod
    def _main_trade_date(rows: list[dict[str, Any]]) -> date | None:
        counts: dict[date, int] = {}
        for row in rows:
            dt = MiningContext._parse_date(row.get("latest_date"))
            if dt:
                counts[dt] = counts.get(dt, 0) + 1
        if not counts:
            return None
        broad_dates = [dt for dt, count in counts.items() if count >= 1000]
        return max(broad_dates or counts.keys())

    @staticmethod
    def _stratified_sample(rows: list[dict[str, Any]]) -> list[str]:
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (str(row.get("industry") or ""), str(row.get("market") or ""))
            buckets.setdefault(key, []).append(row)
        for items in buckets.values():
            items.sort(
                key=lambda item: (
                    -float(item.get("market_cap") or 0.0),
                    str(item.get("code") or ""),
                )
            )
        selected: list[str] = []
        bucket_items = sorted(buckets.items(), key=lambda item: item[0])
        while len(selected) < TARGET_VALIDATION_UNIVERSE_SIZE:
            progressed = False
            for _, items in bucket_items:
                if not items:
                    continue
                code = str(items.pop(0).get("code") or "").strip()
                if code and code not in selected:
                    selected.append(code)
                    progressed = True
                    if len(selected) >= TARGET_VALIDATION_UNIVERSE_SIZE:
                        break
            if not progressed:
                break
        return selected

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text[:10]).date()
        except Exception:
            return None

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
