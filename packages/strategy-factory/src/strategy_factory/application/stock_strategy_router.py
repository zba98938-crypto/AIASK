"""SR-1：个股诊断 → 策略类型路由器（Stock-First）。

关联：开发周期计划-倒置架构与因子路由-2026-06-03.md · Phase 1 · P1-2
设计要点（透明、可单测、纯函数、无 IO）：
- 输入 StockRegimeProfile（个股状态画像，由 SR-0 / stock_profile_pipeline 提供，已挂在 row 上）。
- 输出 RouteResult：适配 family 集合 + holding_period_bucket + 置信度 + **排除项** + rationale。
- 严守同步边界：本模块**不做任何异步/网络调用**，只读已算好的画像字段（见 P1-2 修正）。
- toggle 关闭时调用方不应使用本路由器（由 STOCK_FIRST_ROUTER_ENABLED 控制）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


# 4 大类 × 周期 的族归属（与 domain.constants.FACTOR_STRATEGY_MAPPING / spawner families 对齐）
TREND_FAMILIES = ("momentum", "ma_cross", "volatility_breakout", "event_structure_breakout")
MEAN_REVERSION_FAMILIES = ("mean_reversion_short", "rsi", "gap_fill")
VALUE_QUALITY_FAMILIES = ("value_factor", "quality_factor", "growth_factor")
MULTI_FACTOR_FAMILIES = ("multi_factor",)
EVENT_ROTATION_FAMILIES = ("event_structure_breakout", "sector_rotation")

# holding_period_bucket 与族的对应（与 domain.strategy_profile 的桶口径一致）
_BUCKET_BY_GROUP = {
    "trend": "medium",
    "mean_reversion": "short",
    "value_quality": "long",
    "multi_factor": "medium",
    "event_rotation": "short",
}


@dataclass
class StockRegimeProfile:
    """个股状态画像（SR-0 契约的轻量映射，便于 SR-1 类型化消费）。

    所有字段可选；缺失时路由器走保守降级（倾向 multi_factor / 不强排除）。
    """

    code: str = ""
    trend_regime: str = "unknown"        # trend_up / trend_down / range / unknown
    vol_regime: str = "unknown"          # high_vol / normal_vol / low_vol / unknown
    sentiment_regime: str = "unknown"    # fear / greed / neutral / unknown
    momentum_score: float = 0.0          # 动量强度（dimension trend）
    reversal_score: float = 0.0          # 反转/超卖强度（dimension reversal）
    rsi: Optional[float] = None          # 0-100
    volume_ratio: float = 1.0            # 量比（>1 放量）
    valuation_score: float = 0.0         # 估值吸引力（越高越低估）
    quality_score: float = 0.0           # 质量分
    growth_score: float = 0.0            # 成长分
    multi_factor_ic_resonance: bool = False  # 多因子 IC 共振
    event_catalyst: bool = False         # 事件催化在场
    liquidity_low: bool = False          # 低流动性（排除日内/短线高频）
    profile_quality: str = "unknown"     # good / partial / low_confidence / failed / unknown

    @classmethod
    def from_profile_summary(
        cls,
        code: str,
        profile_summary: Optional[Mapping[str, Any]],
        regime: Optional[Mapping[str, Any]] = None,
        extras: Optional[Mapping[str, Any]] = None,
    ) -> "StockRegimeProfile":
        """从 stock_profile_pipeline 的 profile_summary + regime 字典构造画像。"""
        summary = dict(profile_summary or {})
        dims = dict(summary.get("factor_dimension_scores") or {})
        regime = dict(regime or {})
        extras = dict(extras or {})

        def _f(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        rsi_val = extras.get("rsi")
        return cls(
            code=str(code or "").strip(),
            trend_regime=str(regime.get("trend_regime") or "unknown").strip().lower() or "unknown",
            vol_regime=str(regime.get("vol_regime") or "unknown").strip().lower() or "unknown",
            sentiment_regime=str(regime.get("sentiment_regime") or "unknown").strip().lower() or "unknown",
            momentum_score=_f(dims.get("trend")),
            reversal_score=_f(dims.get("reversal")),
            rsi=None if rsi_val is None else _f(rsi_val, 50.0),
            volume_ratio=_f(extras.get("volume_ratio"), 1.0),
            valuation_score=_f(dims.get("valuation")),
            quality_score=_f(dims.get("quality")),
            growth_score=_f(dims.get("growth")),
            multi_factor_ic_resonance=bool(extras.get("multi_factor_ic_resonance")),
            event_catalyst=bool(extras.get("event_catalyst")),
            liquidity_low=bool(extras.get("liquidity_low")),
            profile_quality=str(summary.get("profile_quality") or "unknown").strip().lower() or "unknown",
        )


@dataclass
class RouteResult:
    families: list[str] = field(default_factory=list)
    holding_period_bucket: str = "medium"
    confidence: float = 0.0
    exclusions: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "families": list(self.families),
            "holding_period_bucket": self.holding_period_bucket,
            "confidence": round(float(self.confidence), 4),
            "exclusions": list(self.exclusions),
            "rationale": list(self.rationale),
        }


def route_strategies(profile: StockRegimeProfile, *, max_families: int = 6) -> RouteResult:
    """透明 if/score 路由：个股画像 → 适配 family + 周期 + 排除项。

    规则（与 SF-ROUTER §3.2 对齐）：
    - 强趋势 + 动量正 + 放量      → 趋势族 (medium)
    - 超卖 + 缩量止跌            → 均值回归族 (short)
    - 低估值 + 质量稳            → 价值/质量/成长族 (long)
    - 多因子 IC 共振            → multi_factor (medium)
    - 事件催化 + 情绪发酵        → 事件/轮动族 (short)
    **排除项**：震荡/趋势向下 → 排除趋势族；低流动性 → 排除短线高频族。
    """
    families: list[str] = []
    exclusions: list[str] = []
    rationale: list[str] = []
    group_scores: dict[str, float] = {}

    def add(items, reason: str) -> None:
        for fam in items:
            if fam not in families and fam not in exclusions:
                families.append(fam)
        rationale.append(reason)

    rsi = profile.rsi if profile.rsi is not None else 50.0
    is_uptrend = profile.trend_regime == "trend_up"
    is_downtrend = profile.trend_regime == "trend_down"
    is_range = profile.trend_regime == "range"
    is_volume_up = profile.volume_ratio >= 1.2

    # ── 排除项先行（决定哪些族不能进） ──
    if is_range or is_downtrend:
        exclusions.extend(f for f in TREND_FAMILIES if f not in exclusions)
        rationale.append(f"exclude_trend: trend_regime={profile.trend_regime}")
    if profile.liquidity_low:
        for f in ("gap_fill", "mean_reversion_short"):
            if f not in exclusions:
                exclusions.append(f)
        rationale.append("exclude_short_highfreq: low_liquidity")

    # ── 趋势族 ──
    if is_uptrend and profile.momentum_score >= 0.3:
        score = profile.momentum_score + (0.2 if is_volume_up else 0.0)
        group_scores["trend"] = score
        add([f for f in TREND_FAMILIES if f not in exclusions],
            f"trend_following: trend_up momentum={profile.momentum_score:.2f} vol_up={is_volume_up}")

    # ── 均值回归族（超卖 + 非基本面恶化） ──
    oversold = rsi <= 35 or profile.reversal_score >= 0.4
    if oversold and not is_downtrend:
        group_scores["mean_reversion"] = max(profile.reversal_score, (35.0 - rsi) / 35.0 if rsi <= 35 else 0.0)
        add([f for f in MEAN_REVERSION_FAMILIES if f not in exclusions],
            f"mean_reversion: rsi={rsi:.0f} reversal={profile.reversal_score:.2f}")

    # ── 价值/质量/成长族 ──
    if profile.valuation_score >= 0.3 and profile.quality_score >= 0.3:
        group_scores["value_quality"] = profile.valuation_score * 0.6 + profile.quality_score * 0.4
        add(VALUE_QUALITY_FAMILIES,
            f"value_quality: val={profile.valuation_score:.2f} quality={profile.quality_score:.2f}")

    # ── 多因子共振 ──
    if profile.multi_factor_ic_resonance:
        group_scores["multi_factor"] = 0.5
        add(MULTI_FACTOR_FAMILIES, "multi_factor: ic_resonance")

    # ── 事件/轮动族 ──
    if profile.event_catalyst:
        group_scores["event_rotation"] = 0.4 + (0.2 if profile.sentiment_regime == "greed" else 0.0)
        add([f for f in EVENT_ROTATION_FAMILIES if f not in exclusions],
            f"event_rotation: catalyst sentiment={profile.sentiment_regime}")

    # ── 降级：无任何规则命中（或画像质量差）→ 保守 multi_factor ──
    if not families:
        families = list(MULTI_FACTOR_FAMILIES)
        group_scores["multi_factor"] = 0.2
        rationale.append("fallback_multi_factor: no_rule_matched_or_low_confidence")

    # ── holding_period_bucket：取得分最高的 group ──
    if group_scores:
        top_group = max(group_scores.items(), key=lambda kv: kv[1])[0]
    else:
        top_group = "multi_factor"
    holding_bucket = _BUCKET_BY_GROUP.get(top_group, "medium")

    # ── 置信度：规则强度 × 画像质量折扣 ──
    base_conf = min(1.0, max(group_scores.values()) if group_scores else 0.2)
    quality_discount = {
        "good": 1.0, "partial": 0.8, "low_confidence": 0.5, "failed": 0.3, "unknown": 0.6,
    }.get(profile.profile_quality, 0.6)
    confidence = round(base_conf * quality_discount, 4)

    return RouteResult(
        families=families[:max_families],
        holding_period_bucket=holding_bucket,
        confidence=confidence,
        exclusions=exclusions,
        rationale=rationale,
    )


__all__ = ["StockRegimeProfile", "RouteResult", "route_strategies"]
