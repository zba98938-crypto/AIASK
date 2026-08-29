"""Parametrised execution slippage and fill simulation models.

Three composable models are provided:

VolumeShareSlippageModel
    Estimates slippage cost from volume participation rate.
    Based on the linear + sqrt market-impact decomposition used in practice.

MarketImpactModel
    Estimates permanent price impact from order size and average daily volume.
    Implements a simplified Almgren-Chriss style impact model.

PartialFillSimulator
    Simulates partial fill probability and expected filled quantity given
    available bid/ask volume, order size, and participation rate.

All models are pure-function / dataclass based, with no external dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float) -> float:
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = 0.0
    if not math.isfinite(fallback):
        fallback = 0.0
    try:
        parsed = float(value if value is not None else fallback)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _clamp(value: float, lo: float, hi: float) -> float:
    safe_lo = _safe_float(lo, 0.0)
    safe_hi = _safe_float(hi, safe_lo)
    if safe_hi < safe_lo:
        safe_lo, safe_hi = safe_hi, safe_lo
    return max(safe_lo, min(safe_hi, _safe_float(value, safe_lo)))


def _bps_to_ratio(bps: float) -> float:
    return float(bps) / 10_000.0


def _ratio_to_bps(ratio: float) -> float:
    return float(ratio) * 10_000.0


# ---------------------------------------------------------------------------
# Model 1 — Volume Share Slippage
# ---------------------------------------------------------------------------

@dataclass
class VolumeShareSlippageModel:
    """Estimate slippage cost from participation rate (volume share).

    Linear component (spread-driven) + sqrt component (temporary impact).

    slippage_bps = spread_bps * 0.5
                 + linear_coeff * participation_rate
                 + sqrt_coeff * sqrt(participation_rate)

    Parameters
    ----------
    spread_bps : float
        Half-spread in basis points (default 5 bps for liquid A-shares).
    linear_coeff : float
        Coefficient for linear impact term (default 10 bps at 100% participation).
    sqrt_coeff : float
        Coefficient for square-root impact term (default 20 bps at 100% participation).
    max_participation_rate : float
        Cap on participation rate accepted as input (default 0.25 = 25%).
    """

    spread_bps: float = 5.0
    linear_coeff: float = 10.0
    sqrt_coeff: float = 20.0
    max_participation_rate: float = 0.25

    def estimate(
        self,
        *,
        order_shares: int,
        avg_minute_volume: float,
        duration_minutes: int,
        reference_price: float,
    ) -> dict[str, Any]:
        """Return a slippage estimate dict.

        Parameters
        ----------
        order_shares : int
            Total shares to execute.
        avg_minute_volume : float
            Average traded volume per minute (shares).
        duration_minutes : int
            Planned execution window in minutes.
        reference_price : float
            Reference price (e.g. mid-price or last price) in CNY.
        """
        resolved_duration = max(1, int(_safe_float(duration_minutes, 1.0)))
        resolved_avg_vol = max(1.0, _safe_float(avg_minute_volume, 1.0))
        resolved_order = max(1, int(_safe_float(order_shares, 1.0)))
        resolved_price = max(0.01, _safe_float(reference_price, 0.01))

        total_available_volume = resolved_avg_vol * resolved_duration
        participation_rate = _clamp(
            resolved_order / total_available_volume,
            0.0,
            _safe_float(self.max_participation_rate, 0.25),
        )

        spread_cost_bps = _safe_float(self.spread_bps, 5.0) * 0.5
        linear_impact_bps = _safe_float(self.linear_coeff, 10.0) * participation_rate
        sqrt_impact_bps = _safe_float(self.sqrt_coeff, 20.0) * math.sqrt(participation_rate)
        total_slippage_bps = spread_cost_bps + linear_impact_bps + sqrt_impact_bps

        slippage_ratio = _bps_to_ratio(total_slippage_bps)
        slippage_per_share = resolved_price * slippage_ratio
        total_slippage_cny = slippage_per_share * resolved_order

        return {
            "model": "volume_share_slippage",
            "participation_rate": round(participation_rate, 6),
            "spread_cost_bps": round(spread_cost_bps, 4),
            "linear_impact_bps": round(linear_impact_bps, 4),
            "sqrt_impact_bps": round(sqrt_impact_bps, 4),
            "total_slippage_bps": round(total_slippage_bps, 4),
            "slippage_ratio": round(slippage_ratio, 8),
            "slippage_per_share_cny": round(slippage_per_share, 6),
            "total_slippage_cny": round(total_slippage_cny, 2),
            "inputs": {
                "order_shares": resolved_order,
                "avg_minute_volume": round(resolved_avg_vol, 2),
                "duration_minutes": resolved_duration,
                "reference_price": round(resolved_price, 4),
            },
        }


# ---------------------------------------------------------------------------
# Model 2 — Market Impact (permanent price shift)
# ---------------------------------------------------------------------------

@dataclass
class MarketImpactModel:
    """Estimate permanent price impact from order relative to average daily volume.

    Based on simplified Almgren-Chriss linear permanent impact:

        impact_bps = eta * (order_shares / ADV) * sqrt(ADV / 1e6)

    where eta is a calibrated market-impact coefficient.

    Parameters
    ----------
    eta : float
        Market impact coefficient. Typical values for A-shares: 50–150 bps.
    min_adv_shares : int
        Floor for ADV (avoid division by zero for illiquid stocks).
    max_impact_bps : float
        Cap on reported impact (avoids extreme extrapolation).
    """

    eta: float = 80.0
    min_adv_shares: int = 100_000
    max_impact_bps: float = 200.0

    def estimate(
        self,
        *,
        order_shares: int,
        adv_shares: float,
        reference_price: float,
    ) -> dict[str, Any]:
        """Return a market impact estimate dict.

        Parameters
        ----------
        order_shares : int
            Total shares to execute.
        adv_shares : float
            Average daily volume in shares.
        reference_price : float
            Reference price in CNY.
        """
        min_adv = max(1.0, _safe_float(self.min_adv_shares, 100_000.0))
        resolved_order = max(1, int(_safe_float(order_shares, 1.0)))
        resolved_adv = max(min_adv, _safe_float(adv_shares, min_adv))
        resolved_price = max(0.01, _safe_float(reference_price, 0.01))

        order_fraction = resolved_order / resolved_adv
        # Scale factor reduces impact for larger, more liquid stocks
        scale_factor = math.sqrt(resolved_adv / 1_000_000.0)
        impact_bps = _clamp(
            _safe_float(self.eta, 80.0) * order_fraction * max(scale_factor, 0.1),
            0.0,
            _safe_float(self.max_impact_bps, 200.0),
        )

        impact_ratio = _bps_to_ratio(impact_bps)
        impact_per_share_cny = resolved_price * impact_ratio
        total_impact_cny = impact_per_share_cny * resolved_order

        return {
            "model": "market_impact",
            "order_fraction_of_adv": round(order_fraction, 6),
            "impact_bps": round(impact_bps, 4),
            "impact_ratio": round(impact_ratio, 8),
            "impact_per_share_cny": round(impact_per_share_cny, 6),
            "total_impact_cny": round(total_impact_cny, 2),
            "liquidity_tier": (
                "high" if resolved_adv >= 5_000_000
                else ("medium" if resolved_adv >= 500_000 else "low")
            ),
            "inputs": {
                "order_shares": resolved_order,
                "adv_shares": round(resolved_adv, 0),
                "reference_price": round(resolved_price, 4),
            },
        }


# ---------------------------------------------------------------------------
# Model 3 — Partial Fill Simulator
# ---------------------------------------------------------------------------

@dataclass
class PartialFillSimulator:
    """Simulate partial fill probability and expected filled quantity.

    Models the scenario where insufficient market volume prevents full execution.

    Parameters
    ----------
    fill_probability_threshold : float
        Participation rate above which partial fill risk increases (default 0.15 = 15%).
    queue_discount : float
        Fraction of top-of-book volume realistically accessible (accounts for
        queue position and competing orders). Default 0.7.
    """

    fill_probability_threshold: float = 0.15
    queue_discount: float = 0.70

    def simulate(
        self,
        *,
        order_shares: int,
        available_volume: float,
        participation_rate: float,
        slices: int = 1,
    ) -> dict[str, Any]:
        """Simulate fill outcome.

        Parameters
        ----------
        order_shares : int
            Total shares to execute.
        available_volume : float
            Total market volume available over the execution window (shares).
        participation_rate : float
            Expected fraction of available volume captured (0–1).
        slices : int
            Number of execution slices (TWAP/VWAP sub-orders).
        """
        resolved_order = max(1, int(_safe_float(order_shares, 1.0)))
        resolved_avol = max(1.0, _safe_float(available_volume, 1.0))
        resolved_part = _clamp(_safe_float(participation_rate, 0.0), 0.0, 1.0)
        resolved_slices = max(1, int(_safe_float(slices, 1.0)))

        # Expected filled volume accounting for queue discount
        queue_discount = _clamp(_safe_float(self.queue_discount, 0.70), 0.0, 1.0)
        accessible_volume = resolved_avol * resolved_part * queue_discount
        expected_filled = min(float(resolved_order), accessible_volume)
        fill_ratio = expected_filled / max(float(resolved_order), 1.0)

        # Probability of full fill
        fill_probability_threshold = _clamp(_safe_float(self.fill_probability_threshold, 0.15), 0.0, 1.0)
        if resolved_part <= fill_probability_threshold:
            full_fill_probability = 0.97
        else:
            # Logistic decay as participation increases
            excess = resolved_part - fill_probability_threshold
            full_fill_probability = _clamp(0.97 * math.exp(-excess * 4.0), 0.10, 0.97)

        # Partial fill scenario
        is_partial_fill_risk = full_fill_probability < 0.90
        shares_at_risk = max(0.0, float(resolved_order) - expected_filled)

        # Per-slice analysis
        shares_per_slice = resolved_order / resolved_slices
        volume_per_slice = resolved_avol / resolved_slices
        slice_fill_ratio = min(1.0, (volume_per_slice * resolved_part * queue_discount) / max(shares_per_slice, 1.0))

        return {
            "model": "partial_fill_simulation",
            "expected_filled_shares": round(expected_filled, 0),
            "expected_unfilled_shares": round(max(0.0, resolved_order - expected_filled), 0),
            "fill_ratio": round(fill_ratio, 4),
            "full_fill_probability": round(full_fill_probability, 4),
            "is_partial_fill_risk": bool(is_partial_fill_risk),
            "shares_at_risk": round(shares_at_risk, 0),
            "queue_discount_applied": queue_discount,
            "per_slice": {
                "shares_per_slice": round(shares_per_slice, 0),
                "volume_per_slice": round(volume_per_slice, 0),
                "slice_fill_ratio": round(slice_fill_ratio, 4),
            },
            "inputs": {
                "order_shares": resolved_order,
                "available_volume": round(resolved_avol, 0),
                "participation_rate": round(resolved_part, 4),
                "slices": resolved_slices,
            },
        }


# ---------------------------------------------------------------------------
# Composite: full slippage simulation
# ---------------------------------------------------------------------------

@dataclass
class ExecutionSlippageBundle:
    """Combines all three models into a single execution simulation call.

    Parameters are forwarded to individual models unchanged.
    """

    volume_share_model: VolumeShareSlippageModel = field(default_factory=VolumeShareSlippageModel)
    market_impact_model: MarketImpactModel = field(default_factory=MarketImpactModel)
    partial_fill_simulator: PartialFillSimulator = field(default_factory=PartialFillSimulator)

    def simulate(
        self,
        *,
        order_shares: int,
        avg_minute_volume: float,
        adv_shares: float,
        duration_minutes: int,
        reference_price: float,
        slices: int = 1,
    ) -> dict[str, Any]:
        """Run all three models and return a unified slippage simulation report."""
        # Volume share slippage
        vs = self.volume_share_model.estimate(
            order_shares=order_shares,
            avg_minute_volume=avg_minute_volume,
            duration_minutes=duration_minutes,
            reference_price=reference_price,
        )
        # Market impact
        mi = self.market_impact_model.estimate(
            order_shares=order_shares,
            adv_shares=adv_shares,
            reference_price=reference_price,
        )
        # Partial fill
        total_window_volume = _safe_float(avg_minute_volume, 1.0) * max(1, int(_safe_float(duration_minutes, 1.0)))
        pf = self.partial_fill_simulator.simulate(
            order_shares=order_shares,
            available_volume=total_window_volume,
            participation_rate=vs["participation_rate"],
            slices=slices,
        )

        # Total cost estimate (volume-share + market-impact)
        total_bps = vs["total_slippage_bps"] + mi["impact_bps"]
        total_cost_cny = vs["total_slippage_cny"] + mi["total_impact_cny"]

        # Execution quality tier
        if total_bps <= 10.0 and pf["full_fill_probability"] >= 0.90:
            execution_quality = "good"
        elif total_bps <= 30.0 and pf["full_fill_probability"] >= 0.75:
            execution_quality = "acceptable"
        else:
            execution_quality = "poor"

        return {
            "slippage_simulation": {
                "total_cost_bps": round(total_bps, 4),
                "total_cost_cny": round(total_cost_cny, 2),
                "total_cost_ratio": round(_bps_to_ratio(total_bps), 8),
                "execution_quality": execution_quality,
                "full_fill_probability": pf["full_fill_probability"],
                "is_partial_fill_risk": pf["is_partial_fill_risk"],
                "participation_rate": vs["participation_rate"],
            },
            "volume_share_slippage": vs,
            "market_impact": mi,
            "partial_fill": pf,
        }


# ---------------------------------------------------------------------------
# Module-level default bundle
# ---------------------------------------------------------------------------

default_slippage_bundle = ExecutionSlippageBundle()


def estimate_execution_slippage(
    *,
    order_shares: int,
    avg_minute_volume: float,
    adv_shares: float,
    duration_minutes: int,
    reference_price: float,
    slices: int = 1,
    spread_bps: float = 5.0,
    eta: float = 80.0,
) -> dict[str, Any]:
    """Convenience function: run the full simulation with custom model params."""
    bundle = ExecutionSlippageBundle(
        volume_share_model=VolumeShareSlippageModel(spread_bps=spread_bps),
        market_impact_model=MarketImpactModel(eta=eta),
    )
    return bundle.simulate(
        order_shares=order_shares,
        avg_minute_volume=avg_minute_volume,
        adv_shares=adv_shares,
        duration_minutes=duration_minutes,
        reference_price=reference_price,
        slices=slices,
    )
