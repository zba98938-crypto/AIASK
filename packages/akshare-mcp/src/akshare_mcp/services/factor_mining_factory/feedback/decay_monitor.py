"""Factor decay monitoring for the active factor pool."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class DecayMonitor:
    """Measure active factors against persisted IC history and emit alerts."""

    ALERT_THRESHOLD = 0.3
    RETIRE_THRESHOLD = 0.5

    def __init__(self) -> None:
        from ..pool.decay_tracker import DecayTracker

        self._tracker = DecayTracker()

    async def daily_check(self, pool: Any, db: Any | None = None) -> dict[str, Any]:
        """Run the daily decay check."""
        if pool is None or pool.size == 0:
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "pool_size": 0,
                "decaying_count": 0,
                "alerts": [],
                "measurements": [],
                "updated_records": [],
            }

        alerts: list[dict[str, Any]] = []
        measurements: list[dict[str, Any]] = []
        updated_records: list[dict[str, Any]] = []
        factor_loader = getattr(pool, "get_decay_monitored_factors", None)
        if callable(factor_loader):
            factors = await factor_loader(limit=200)
        else:
            factors = await pool.get_active_factors(limit=200)

        for factor in factors:
            measurement = await self._measure_factor(db, dict(factor or {}))
            if measurement:
                measurements.append(measurement)
                decay_rate = float(measurement.get("decay_rate") or 0.0)
                update_decay = getattr(pool, "update_decay", None)
                if callable(update_decay):
                    try:
                        update_result = await update_decay(
                            measurement.get("factor_id"),
                            decay_rate,
                            current_ic=measurement.get("current_ic"),
                        )
                        record = dict((update_result or {}).get("record") or {})
                        if record:
                            updated_records.append(record)
                    except Exception as exc:
                        logger.debug(
                            "DecayMonitor: update_decay failed for %s: %s",
                            measurement.get("factor_id"),
                            exc,
                        )
            else:
                decay_rate = float(factor.get("decay_rate") or 0.0)

            if decay_rate > self.RETIRE_THRESHOLD:
                alerts.append(
                    {
                        "factor_id": factor.get("factor_id"),
                        "name": factor.get("name"),
                        "decay_rate": decay_rate,
                        "severity": "critical",
                        "action": "retire",
                    }
                )
            elif decay_rate > self.ALERT_THRESHOLD:
                alerts.append(
                    {
                        "factor_id": factor.get("factor_id"),
                        "name": factor.get("name"),
                        "decay_rate": decay_rate,
                        "severity": "warning",
                        "action": "monitor",
                    }
                )

        if alerts:
            logger.warning(
                "DecayMonitor: %d factors decaying (critical=%d)",
                len(alerts),
                sum(1 for item in alerts if item["severity"] == "critical"),
            )

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "pool_size": pool.size,
            "decaying_count": len(alerts),
            "alerts": alerts,
            "measurements": measurements,
            "updated_records": updated_records,
        }

    async def _measure_factor(
        self,
        db: Any | None,
        factor: dict[str, Any],
    ) -> dict[str, Any] | None:
        if db is None:
            return None

        factor_id = str(factor.get("factor_id") or "").strip()
        factor_name = str(
            factor.get("name")
            or factor.get("factor_name")
            or factor_id
        ).strip()
        if not factor_id or not factor_name:
            return None

        rows: list[dict[str, Any]] = []
        resolved_period = None
        for period in self._candidate_periods(factor):
            rows = await self._load_ic_history(db, factor_name, period=period)
            if not rows and factor_id != factor_name:
                rows = await self._load_ic_history(db, factor_id, period=period)
            if rows:
                resolved_period = period
                break
        values = self._ic_values(rows)
        if not values:
            return None

        current_ic = self._ema(values[:20])
        admission_ic = self._safe_float(factor.get("admission_ic"))
        if admission_ic is None:
            admission_ic = values[-1]
        measurement = self._tracker.measure_decay(
            factor_id=factor_id,
            admission_ic=float(admission_ic),
            current_ic=float(current_ic),
            days_since_admission=self._days_since_admission(factor, rows),
        ).to_dict()
        measurement.update(
            {
                "factor_name": factor_name,
                "period": resolved_period,
                "rolling_ic_20d": self._mean(values[:20]),
                "rolling_ic_60d": self._mean(values[:60]),
            }
        )
        return measurement

    @classmethod
    def _candidate_periods(cls, factor: dict[str, Any]) -> list[str]:
        periods: list[str] = []
        for value in (
            factor.get("expected_holding_period"),
            factor.get("holding_period"),
            factor.get("period"),
            "10",
            "20",
            "5",
            "60",
        ):
            try:
                text = str(int(value)).strip()
            except (TypeError, ValueError):
                text = str(value or "").strip()
            if text and text not in periods:
                periods.append(text)
        return periods or ["10", "20"]

    async def _load_ic_history(
        self,
        db: Any,
        factor_name: str,
        *,
        period: str = "20",
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        method = getattr(db, "get_factor_ic_history", None)
        if callable(method):
            try:
                return [dict(row) for row in await method(factor_name, period, limit)]
            except Exception as exc:
                logger.debug("DecayMonitor: get_factor_ic_history failed: %s", exc)

        fetch = getattr(db, "fetch", None)
        if callable(fetch):
            try:
                rows = await fetch(
                    """
                    SELECT * FROM factor_ic_history
                    WHERE factor_name = $1 AND period = $2
                    ORDER BY ic_date DESC LIMIT $3
                    """,
                    factor_name,
                    period,
                    limit,
                )
                return [dict(row) for row in rows or []]
            except Exception as exc:
                logger.debug("DecayMonitor: factor_ic_history fetch failed: %s", exc)

        acquire = getattr(db, "acquire", None)
        if callable(acquire):
            try:
                async with acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM factor_ic_history
                        WHERE factor_name = $1 AND period = $2
                        ORDER BY ic_date DESC LIMIT $3
                        """,
                        factor_name,
                        period,
                        limit,
                    )
                return [dict(row) for row in rows or []]
            except Exception as exc:
                logger.debug("DecayMonitor: factor_ic_history acquire failed: %s", exc)

        return []

    @classmethod
    def _ic_values(cls, rows: list[dict[str, Any]]) -> list[float]:
        values: list[float] = []
        for row in list(rows or []):
            value = cls._safe_float(row.get("rank_ic"))
            if value is None:
                value = cls._safe_float(row.get("ic_value"))
            if value is not None:
                values.append(value)
        return values

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _mean(values: list[float]) -> float:
        clean = [float(value) for value in values if value is not None]
        return sum(clean) / len(clean) if clean else 0.0

    @staticmethod
    def _ema(values: list[float], *, alpha: float = 0.35) -> float:
        chronological = list(reversed([float(value) for value in values]))
        if not chronological:
            return 0.0
        ema = chronological[0]
        for value in chronological[1:]:
            ema = alpha * value + (1.0 - alpha) * ema
        return ema

    @classmethod
    def _days_since_admission(
        cls,
        factor: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> int:
        started = cls._parse_date(
            factor.get("admission_date")
            or factor.get("created_at")
            or ((rows[-1] or {}).get("ic_date") if rows else None)
        )
        if started is None:
            return 0
        today = datetime.now(timezone.utc).date()
        return max(0, (today - started).days)

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
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").date()
            except ValueError:
                return None
