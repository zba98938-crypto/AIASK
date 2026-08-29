"""Theme-response regression model (PR-7, §7).

Fits cross-theme response regressions to estimate direction_sign,
magnitude_factor, lag_days, and confidence for theme graph edges.

The model answers: "When theme S has a shock, what happens to theme T
at T+k days?" using historical K-line data.

Designed to run weekly (off-hours cron). By default it emits a report with
recommended edge updates; callers must pass ``apply_updates=True`` to write
changes back to strategy_factory_theme_edges.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class EdgeRegressionResult:
    """Result of fitting one (source, target, horizon) edge."""

    source_theme: str
    target_theme: str
    best_horizon: int = 5
    beta: float = 0.0
    r_squared: float = 0.0
    p_value: float = 1.0
    n_samples: int = 0
    direction_sign: int = 0
    magnitude_factor: float = 0.0
    confidence: float = 0.0
    sign_conflict: bool = False
    status: str = "pending"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_theme": self.source_theme,
            "target_theme": self.target_theme,
            "best_horizon": self.best_horizon,
            "beta": round(self.beta, 6),
            "r_squared": round(self.r_squared, 4),
            "p_value": round(self.p_value, 4),
            "n_samples": self.n_samples,
            "direction_sign": self.direction_sign,
            "magnitude_factor": round(self.magnitude_factor, 4),
            "confidence": round(self.confidence, 4),
            "sign_conflict": self.sign_conflict,
            "status": self.status,
            "error": self.error,
        }


class ThemeResponseRegression:
    """Fits event-response regressions for theme graph edges.

    Usage:
        model = ThemeResponseRegression()
        report = await model.run_full_update(db)
    """

    MIN_SAMPLES = 20
    Z_THRESHOLD = 2.0
    HORIZONS = [1, 3, 5, 10, 20]

    async def build_theme_returns(
        self,
        db: Any,
        theme_code: str,
        *,
        lookback_days: int = 750,
    ) -> pd.Series:
        """Build equal-weight daily returns for a theme's constituent stocks.

        Uses local K-line data (no external API calls).
        """
        # Get theme exposure to find constituent stocks
        constituents = []
        if hasattr(db, "list_theme_exposure"):
            try:
                rows = await db.list_theme_exposure(
                    theme_code=theme_code,
                    min_exposure=0.3,
                    limit=30,
                )
                constituents = [str(r.get("symbol") or "").strip() for r in rows if r.get("symbol")]
            except Exception:
                pass

        if not constituents:
            return pd.Series(dtype=float)

        # Load K-lines for each constituent
        all_returns = []
        for symbol in constituents[:20]:  # Cap at 20 for performance
            try:
                klines = await db.get_klines(symbol, limit=lookback_days)
                if not klines or len(klines) < 60:
                    continue
                closes = pd.Series([float(k.get("close") or 0) for k in klines])
                returns = closes.pct_change().dropna()
                returns.index = range(len(returns))
                all_returns.append(returns)
            except Exception:
                continue

        if not all_returns:
            return pd.Series(dtype=float)

        # Equal-weight average
        df = pd.DataFrame(all_returns).T
        theme_returns = df.mean(axis=1)
        return theme_returns

    def detect_shocks(
        self,
        returns: pd.Series,
        z_threshold: float = 2.0,
        window: int = 20,
    ) -> pd.DataFrame:
        """Detect shock days using rolling z-score.

        Returns DataFrame with columns: [index, magnitude, direction_sign]
        """
        if len(returns) < window + 10:
            return pd.DataFrame(columns=["idx", "magnitude", "direction_sign"])

        rolling_mean = returns.rolling(window).mean()
        rolling_std = returns.rolling(window).std()

        # Avoid division by zero
        rolling_std = rolling_std.replace(0, np.nan)

        z_scores = (returns - rolling_mean) / rolling_std
        z_scores = z_scores.dropna()

        # Find shock days
        shock_mask = z_scores.abs() > z_threshold
        shock_indices = z_scores[shock_mask].index.tolist()

        records = []
        for idx in shock_indices:
            mag = min(abs(float(z_scores.loc[idx])), 5.0)  # Cap at 5
            direction = 1 if z_scores.loc[idx] > 0 else -1
            records.append({"idx": idx, "magnitude": mag, "direction_sign": direction})

        return pd.DataFrame(records)

    def fit_edge(
        self,
        source_returns: pd.Series,
        target_returns: pd.Series,
        shocks: pd.DataFrame,
        horizons: list[int] | None = None,
    ) -> EdgeRegressionResult:
        """Fit regression for one edge across multiple horizons.

        y_{T,t,k} = alpha + beta * direction_S * magnitude_S + delta * r_{T,t} + epsilon
        """
        if horizons is None:
            horizons = self.HORIZONS

        if shocks.empty or len(source_returns) < 60:
            return EdgeRegressionResult(
                source_theme="", target_theme="",
                status="insufficient_data",
                n_samples=len(shocks),
            )

        best_result = None
        best_abs_beta = 0.0

        for k in horizons:
            try:
                # Build regression data
                X_list = []
                y_list = []

                for _, shock in shocks.iterrows():
                    idx = int(shock["idx"])
                    end_idx = idx + k

                    if end_idx >= len(target_returns):
                        continue

                    # y: cumulative excess return of target over horizon k
                    y = float(target_returns.iloc[idx + 1:end_idx + 1].sum())

                    # X: direction * magnitude
                    x = float(shock["direction_sign"]) * float(shock["magnitude"])

                    # Control: target's same-day return (§7.3 correction)
                    if idx < len(target_returns):
                        control = float(target_returns.iloc[idx])
                    else:
                        control = 0.0

                    X_list.append([x, control])
                    y_list.append(y)

                if len(y_list) < self.MIN_SAMPLES:
                    continue

                X = np.array(X_list)
                y = np.array(y_list)

                # Add intercept
                X_with_intercept = np.column_stack([np.ones(len(X)), X])

                # OLS regression
                try:
                    beta_hat, residuals, rank, sv = np.linalg.lstsq(X_with_intercept, y, rcond=None)
                except np.linalg.LinAlgError:
                    continue

                beta = float(beta_hat[1])  # Coefficient on direction*magnitude

                # R-squared
                y_mean = y.mean()
                ss_tot = float(((y - y_mean) ** 2).sum())
                y_pred = X_with_intercept @ beta_hat
                ss_res = float(((y - y_pred) ** 2).sum())
                r_squared = 1.0 - (ss_res / max(ss_tot, 1e-10))
                r_squared = max(0.0, min(1.0, r_squared))

                # Approximate p-value (t-test on beta)
                n = len(y)
                p = X_with_intercept.shape[1]
                if n > p:
                    mse = ss_res / (n - p)
                    try:
                        var_beta = mse * np.linalg.inv(X_with_intercept.T @ X_with_intercept)
                        se_beta = math.sqrt(max(0, float(var_beta[1, 1])))
                        t_stat = beta / max(se_beta, 1e-10)
                        # Approximate p-value from t-distribution (two-tailed)
                        p_value = 2.0 * (1.0 - min(0.9999, abs(t_stat) / (abs(t_stat) + 1.0)))
                    except Exception:
                        p_value = 1.0
                else:
                    p_value = 1.0

                if abs(beta) > best_abs_beta:
                    best_abs_beta = abs(beta)
                    best_result = EdgeRegressionResult(
                        source_theme="",
                        target_theme="",
                        best_horizon=k,
                        beta=beta,
                        r_squared=r_squared,
                        p_value=p_value,
                        n_samples=len(y_list),
                        direction_sign=1 if beta > 0 else -1,
                        status="fitted",
                    )

            except Exception as exc:
                logger.debug("fit_edge horizon=%d error: %s", k, exc)
                continue

        if best_result is None:
            return EdgeRegressionResult(
                source_theme="", target_theme="",
                status="no_significant_horizon",
                n_samples=len(shocks),
            )

        # Compute magnitude_factor and confidence
        # magnitude_factor: normalized |beta| (relative to typical values)
        best_result.magnitude_factor = min(1.0, abs(best_result.beta) / 0.05)

        # confidence: combination of R², p-value, and sample size
        sample_factor = min(1.0, best_result.n_samples / 100.0)
        best_result.confidence = min(
            0.95,
            best_result.r_squared * 0.4
            + (1.0 - best_result.p_value) * 0.4
            + sample_factor * 0.2,
        )

        return best_result

    async def run_full_update(self, db: Any, *, apply_updates: bool = False) -> dict[str, Any]:
        """Run regression for all active edges and return update suggestions.

        By default this does not mutate the theme graph. When
        ``apply_updates=True``, only updates edges where:
        - n_samples >= MIN_SAMPLES
        - |beta| >= 0.01
        - p_value <= 0.1
        - manual_locked = 0
        - no sign conflict with the current manual direction
        """
        import time
        start = time.time()

        # Load all active edges
        edges = []
        if hasattr(db, "list_theme_edges"):
            edges = await db.list_theme_edges(is_active=True, limit=200)

        if not edges:
            return {"status": "skipped", "reason": "no_active_edges"}

        results: list[dict[str, Any]] = []
        updated_count = 0
        recommendation_count = 0
        sign_conflicts = 0
        skipped_locked = 0

        for edge in edges:
            source = str(edge.get("source_theme_code") or "").strip()
            target = str(edge.get("target_theme_code") or "").strip()
            manual_locked = int(edge.get("manual_locked") or 0)

            if manual_locked:
                skipped_locked += 1
                continue

            try:
                # Build returns
                source_returns = await self.build_theme_returns(db, source)
                target_returns = await self.build_theme_returns(db, target)

                if source_returns.empty or target_returns.empty:
                    results.append({
                        "source": source, "target": target,
                        "status": "insufficient_data",
                    })
                    continue

                # Align lengths
                min_len = min(len(source_returns), len(target_returns))
                source_returns = source_returns.iloc[:min_len]
                target_returns = target_returns.iloc[:min_len]

                # Detect shocks in source
                shocks = self.detect_shocks(source_returns, z_threshold=self.Z_THRESHOLD)

                # Fit regression
                result = self.fit_edge(source_returns, target_returns, shocks)
                result.source_theme = source
                result.target_theme = target

                # Check for sign conflict with manual value
                manual_direction = int(edge.get("direction_sign") or 0)
                if result.direction_sign != 0 and manual_direction != 0:
                    if result.direction_sign != manual_direction:
                        result.sign_conflict = True
                        sign_conflicts += 1

                # Significant non-conflicting fits are recommendations by
                # default; callers opt into applying them.
                recommended_update = (
                    result.status == "fitted"
                    and result.n_samples >= self.MIN_SAMPLES
                    and abs(result.beta) >= 0.01
                    and result.p_value <= 0.1
                    and not result.sign_conflict
                )
                suggestion = None
                if recommended_update:
                    suggestion = {
                        "source_theme_code": source,
                        "target_theme_code": target,
                        "relation_type": edge.get("relation_type"),
                        "direction_sign": result.direction_sign,
                        "magnitude_factor": result.magnitude_factor,
                        "lag_days": result.best_horizon,
                        "confidence": result.confidence,
                        "confidence_source": "regression",
                        "manual_confidence_backup": float(edge.get("confidence") or 0),
                        "manual_magnitude_backup": float(edge.get("magnitude_factor") or 0),
                        "evidence": {
                            "beta": result.beta,
                            "r_squared": result.r_squared,
                            "p_value": result.p_value,
                            "n_samples": result.n_samples,
                            "best_horizon": result.best_horizon,
                        },
                    }
                    recommendation_count += 1

                update_applied = False
                if apply_updates and suggestion is not None and hasattr(db, "upsert_theme_edge"):
                    await db.upsert_theme_edge(suggestion)
                    updated_count += 1
                    update_applied = True

                item = result.to_dict()
                item["recommended_update"] = suggestion
                item["update_applied"] = update_applied
                results.append(item)

            except Exception as exc:
                logger.warning("ThemeResponseRegression: edge %s→%s failed: %s", source, target, exc)
                results.append({
                    "source": source, "target": target,
                    "status": "error", "error": str(exc),
                })

        elapsed = time.time() - start
        return {
            "status": "completed",
            "mode": "apply_updates" if apply_updates else "report_only",
            "apply_updates": bool(apply_updates),
            "total_edges": len(edges),
            "fitted_count": sum(1 for r in results if r.get("status") == "fitted"),
            "recommendation_count": recommendation_count,
            "updated_count": updated_count,
            "sign_conflicts": sign_conflicts,
            "skipped_locked": skipped_locked,
            "elapsed_seconds": round(elapsed, 2),
            "results": results,
        }


__all__ = [
    "EdgeRegressionResult",
    "ThemeResponseRegression",
]
