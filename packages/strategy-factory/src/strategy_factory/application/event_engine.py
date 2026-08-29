"""本地 DB 驱动的事件主题研究引擎。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .runtime import _call_optional_async

logger = logging.getLogger(__name__)


LOCAL_EVENT_ENGINE_NAME = "local_db_rule_v1"


_DEFAULT_THEME_LIBRARY: List[dict[str, Any]] = [
    {
        "theme_code": "upstream_oil_gas",
        "theme_name": "上游油气",
        "parent_theme_code": "commodities",
        "description": "油气开采、炼化与油服链条的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["石油", "油气", "原油", "炼化", "油服", "oil", "gas"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["momentum", "ma_cross", "growth_factor"],
            "factor_support": ["momentum", "growth"],
        },
    },
    {
        "theme_code": "shipping_trade",
        "theme_name": "航运贸易",
        "parent_theme_code": "global_trade",
        "description": "航运、港口与出口链条的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["航运", "港口", "物流", "集运", "shipping"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["momentum", "ma_cross", "growth_factor"],
            "factor_support": ["momentum"],
        },
    },
    {
        "theme_code": "chip_domestic",
        "theme_name": "芯片半导体",
        "parent_theme_code": "technology",
        "description": "芯片、半导体设备与算力链条的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["芯片", "半导体", "设备", "算力", "ai", "服务器"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["momentum", "ma_cross", "growth_factor"],
            "factor_support": ["growth", "momentum"],
        },
    },
    {
        "theme_code": "military_industry",
        "theme_name": "军工国防",
        "parent_theme_code": "defense",
        "description": "军工、航空航天与国防链条的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["军工", "国防", "航空", "航天", "导弹"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["momentum", "ma_cross", "growth_factor"],
            "factor_support": ["momentum", "growth"],
        },
    },
    {
        "theme_code": "high_dividend_banks",
        "theme_name": "高股息金融",
        "parent_theme_code": "defensive",
        "description": "银行、保险、公用事业等高股息防御链条的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["银行", "保险", "高股息", "运营商", "公用事业", "金融"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["quality_factor", "value_factor", "ma_cross"],
            "factor_support": ["value", "quality"],
        },
    },
    {
        "theme_code": "liquor_consumption",
        "theme_name": "消费龙头",
        "parent_theme_code": "consumer",
        "description": "白酒、家电与消费龙头的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["白酒", "消费", "家电", "食品", "饮料"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["quality_factor", "momentum", "ma_cross"],
            "factor_support": ["quality", "value"],
        },
    },
    {
        "theme_code": "new_energy_chain",
        "theme_name": "新能源链",
        "parent_theme_code": "energy_transition",
        "description": "新能源、电池、储能与整车链条的相对强弱跟踪。",
        "direction_rule": "follow_relative_strength",
        "aliases": ["新能源", "电池", "储能", "光伏", "整车", "锂电"],
        "metadata": {
            "event_type": "theme_rotation",
            "horizon": "swing_5_20d",
            "strategy_preferences": ["growth_factor", "momentum", "ma_cross"],
            "factor_support": ["growth", "momentum"],
        },
    },
]


class LocalEventDrivenResearchEngine:
    """使用本地 DB 行情与股票池生成事件主题与信号。"""

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or default)
        except Exception:
            return float(default)

    @classmethod
    def _clip_score(cls, value: Any, default: float = 0.0) -> float:
        score = cls._safe_float(value, default)
        return max(0.0, min(score, 1.0))

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _row_text(cls, row: dict) -> str:
        return " ".join(
            cls._normalize_text(row.get(key))
            for key in ("name", "industry", "sector", "market")
            if row.get(key) not in (None, "")
        )

    @classmethod
    def _alias_hits(cls, row: dict, aliases: List[str]) -> List[str]:
        text = cls._row_text(row)
        hits: List[str] = []
        for alias in list(aliases or []):
            normalized = cls._normalize_text(alias)
            if normalized and normalized in text and normalized not in hits:
                hits.append(normalized)
        return hits

    @classmethod
    def _infer_sector_like_group(cls, row: dict) -> str:
        explicit = str(row.get("sector") or row.get("industry") or "").strip()
        if explicit:
            return explicit
        for theme in list(_DEFAULT_THEME_LIBRARY or []):
            if cls._alias_hits(row, list(theme.get("aliases") or [])):
                return str(theme.get("theme_name") or theme.get("theme_code") or "").strip()
        return ""

    @classmethod
    def _summarize_klines(cls, klines: List[dict]) -> dict[str, Any]:
        closes = [
            cls._safe_float(item.get("close"))
            for item in list(klines or [])
            if item.get("close") is not None
        ]
        volumes = [
            cls._safe_float(item.get("volume"))
            for item in list(klines or [])
            if item.get("volume") is not None
        ]
        if len(closes) < 5:
            return {
                "close": closes[-1] if closes else 0.0,
                "return_5d": 0.0,
                "return_20d": 0.0,
                "price_ma20_gap": 0.0,
                "volume_ratio": 1.0,
                "trend_state": "flat",
            }
        last_close = closes[-1]
        base_5 = closes[-6] if len(closes) >= 6 else closes[0]
        base_20 = closes[-21] if len(closes) >= 21 else closes[0]
        ma20_sample = closes[-20:] if len(closes) >= 20 else closes
        ma20 = sum(ma20_sample) / max(len(ma20_sample), 1)
        return_5d = (last_close - base_5) / base_5 if base_5 else 0.0
        return_20d = (last_close - base_20) / base_20 if base_20 else 0.0
        price_ma20_gap = (last_close - ma20) / ma20 if ma20 else 0.0
        last_volume = volumes[-1] if volumes else 0.0
        mean_volume = (sum(volumes[-20:]) / max(len(volumes[-20:]), 1)) if volumes else 0.0
        volume_ratio = last_volume / mean_volume if mean_volume else 1.0
        trend_state = (
            "uptrend"
            if return_20d > 0.03 and price_ma20_gap > 0
            else ("downtrend" if return_20d < -0.03 and price_ma20_gap < 0 else "flat")
        )
        return {
            "close": round(last_close, 4),
            "return_5d": round(return_5d, 6),
            "return_20d": round(return_20d, 6),
            "price_ma20_gap": round(price_ma20_gap, 6),
            "volume_ratio": round(volume_ratio, 4),
            "trend_state": trend_state,
        }

    @classmethod
    def _aggregate_groups(cls, rows: List[dict], key: str) -> Dict[str, dict]:
        groups: Dict[str, dict] = {}
        for row in list(rows or []):
            if key == "sector":
                name = cls._infer_sector_like_group(row)
            else:
                name = str(row.get(key) or "").strip()
            if not name:
                continue
            bucket = groups.setdefault(name, {"group": name, "rows": []})
            bucket["rows"].append(row)
        for name, bucket in groups.items():
            items = bucket["rows"]
            avg_return_5d = sum(cls._safe_float(item.get("return_5d")) for item in items) / max(len(items), 1)
            avg_return_20d = sum(cls._safe_float(item.get("return_20d")) for item in items) / max(len(items), 1)
            avg_gap = sum(cls._safe_float(item.get("price_ma20_gap")) for item in items) / max(len(items), 1)
            score = avg_return_20d * 0.6 + avg_return_5d * 0.25 + avg_gap * 0.15
            bucket.update(
                {
                    "count": len(items),
                    "avg_return_5d": round(avg_return_5d, 6),
                    "avg_return_20d": round(avg_return_20d, 6),
                    "avg_gap": round(avg_gap, 6),
                    "score": round(score, 6),
                }
            )
        return groups

    @classmethod
    def _build_market_internals(cls, rows: List[dict], rotation: dict) -> dict[str, Any]:
        symbol_count = len(list(rows or []))
        trend_up_count = len([item for item in list(rows or []) if str(item.get("trend_state") or "") == "uptrend"])
        trend_down_count = len([item for item in list(rows or []) if str(item.get("trend_state") or "") == "downtrend"])
        avg_return_5d = sum(cls._safe_float(item.get("return_5d")) for item in list(rows or [])) / max(symbol_count, 1)
        avg_return_20d = sum(cls._safe_float(item.get("return_20d")) for item in list(rows or [])) / max(symbol_count, 1)
        avg_volume_ratio = sum(cls._safe_float(item.get("volume_ratio"), 1.0) for item in list(rows or [])) / max(symbol_count, 1)
        breadth = (trend_up_count - trend_down_count) / max(symbol_count, 1)
        leverage_proxy_pct = avg_return_5d * 180 + (avg_volume_ratio - 1.0) * 18 + breadth * 9
        leverage_proxy_pct = round(max(min(leverage_proxy_pct, 20.0), -20.0), 2)
        return {
            "engine": LOCAL_EVENT_ENGINE_NAME,
            "symbol_count": symbol_count,
            "trend_up_count": trend_up_count,
            "trend_down_count": trend_down_count,
            "avg_return_5d": round(avg_return_5d, 6),
            "avg_return_20d": round(avg_return_20d, 6),
            "avg_volume_ratio": round(avg_volume_ratio, 4),
            "breadth_score": round(breadth, 6),
            "margin_proxy_5d_change_pct": leverage_proxy_pct,
            "hot_sectors": list(rotation.get("hot_sectors") or []),
            "cold_sectors": list(rotation.get("cold_sectors") or []),
        }

    @classmethod
    def _derive_sector_rotation(cls, snapshot: dict, sector_scores: Dict[str, dict]) -> dict[str, List[str]]:
        ranked = sorted(
            [value for value in sector_scores.values() if int(value.get("count") or 0) > 0],
            key=lambda item: (cls._safe_float(item.get("score")), int(item.get("count") or 0)),
            reverse=True,
        )
        hot = [item.get("group") for item in ranked if cls._safe_float(item.get("score")) > 0.015][:5]
        cold = [item.get("group") for item in reversed(ranked) if cls._safe_float(item.get("score")) < -0.015][:5]
        if not hot and ranked:
            hot = [item.get("group") for item in ranked[: min(2, len(ranked))] if item.get("group")]
        if not cold and ranked:
            cold = [item.get("group") for item in list(reversed(ranked[: min(2, len(ranked))])) if item.get("group")]
        if not snapshot.get("hot_sectors"):
            snapshot["hot_sectors"] = [item for item in hot if item]
        if not snapshot.get("cold_sectors"):
            snapshot["cold_sectors"] = [item for item in cold if item]
        return {
            "hot_sectors": [str(item).strip() for item in list(snapshot.get("hot_sectors") or []) if str(item).strip()],
            "cold_sectors": [str(item).strip() for item in list(snapshot.get("cold_sectors") or []) if str(item).strip()],
        }

    @classmethod
    def _factor_support(cls, theme: dict, snapshot: dict, direction: str) -> tuple[float, List[str]]:
        metadata = dict(theme.get("metadata") or {})
        factor_trend = dict(snapshot.get("factor_ic_trend") or {})
        bonus = 0.0
        reasons: List[str] = []
        for factor in list(metadata.get("factor_support") or []):
            trend = str(factor_trend.get(factor) or "").strip().lower()
            if trend == "rising" and direction == "positive":
                bonus += 0.04
                reasons.append(f"{factor} 因子 IC 走强")
            elif trend == "falling" and direction == "negative":
                bonus += 0.04
                reasons.append(f"{factor} 因子 IC 转弱")
        return min(bonus, 0.12), reasons

    @classmethod
    def _determine_direction(cls, rows: List[dict], rotation: dict) -> str:
        if not rows:
            return "neutral"
        avg_return = sum(cls._safe_float(item.get("return_20d")) for item in rows) / max(len(rows), 1)
        sectors = {
            str(item.get("sector") or item.get("industry") or "").strip()
            for item in rows
            if str(item.get("sector") or item.get("industry") or "").strip()
        }
        hot = set(rotation.get("hot_sectors") or [])
        cold = set(rotation.get("cold_sectors") or [])
        if sectors & hot and avg_return >= -0.01:
            return "positive"
        if sectors & cold and avg_return <= 0.01:
            return "negative"
        if avg_return >= 0.03:
            return "positive"
        if avg_return <= -0.03:
            return "negative"
        return "neutral"

    @classmethod
    def _fundamental_score(cls, row: dict, direction: str) -> float:
        pe_ratio = cls._safe_float(row.get("pe_ratio"), default=0.0)
        pb_ratio = cls._safe_float(row.get("pb_ratio"), default=0.0)
        score = 0.45
        if direction == "positive":
            if 0 < pe_ratio <= 25:
                score += 0.12
            if 0 < pb_ratio <= 3.5:
                score += 0.12
        else:
            if pe_ratio == 0 or pe_ratio <= 12:
                score += 0.08
            if pb_ratio == 0 or pb_ratio <= 1.5:
                score += 0.08
        return cls._clip_score(score, default=0.45)

    @classmethod
    def _flow_confirm_score(
        cls,
        row: dict,
        rotation: dict,
        sector_scores: Dict[str, dict],
        direction: str,
    ) -> float:
        sector = str(row.get("sector") or row.get("industry") or "").strip()
        score = 0.35
        if sector and sector in set(rotation.get("hot_sectors") or []) and direction == "positive":
            score += 0.35
        if sector and sector in set(rotation.get("cold_sectors") or []) and direction == "negative":
            score += 0.35
        sector_strength = cls._safe_float((sector_scores.get(sector) or {}).get("score"))
        if direction == "positive":
            score += min(max(sector_strength * 6, -0.15), 0.2)
        else:
            score += min(max(-sector_strength * 6, -0.15), 0.2)
        return cls._clip_score(score, default=0.35)

    @classmethod
    def _price_confirm_score(cls, row: dict, direction: str) -> float:
        return_5d = cls._safe_float(row.get("return_5d"))
        return_20d = cls._safe_float(row.get("return_20d"))
        gap = cls._safe_float(row.get("price_ma20_gap"))
        volume_ratio = cls._safe_float(row.get("volume_ratio"), 1.0)
        signal = return_20d * 4.5 + return_5d * 2.0 + gap * 3.5 + max(volume_ratio - 1.0, 0.0) * 0.25
        if direction == "negative":
            signal = (-return_20d) * 4.5 + (-return_5d) * 2.0 + (-gap) * 3.5 + max(volume_ratio - 1.0, 0.0) * 0.15
        return cls._clip_score(0.45 + signal, default=0.45)

    @classmethod
    def _score_theme_symbol(
        cls,
        theme: dict,
        row: dict,
        hits: List[str],
        direction: str,
        rank: int,
        total: int,
        rotation: dict,
        sector_scores: Dict[str, dict],
        factor_bonus: float,
    ) -> dict:
        rank_factor = 1.0 - ((rank - 1) / max(total - 1, 1)) if total > 1 else 1.0
        theme_score = cls._clip_score(
            0.4
            + min(len(hits), 3) * 0.15
            + (0.1 if str(row.get("industry") or row.get("sector") or "").strip() else 0.0),
            default=0.5,
        )
        exposure_score = cls._clip_score(0.45 + rank_factor * 0.35 + min(len(hits), 2) * 0.08, default=0.45)
        price_confirm_score = cls._price_confirm_score(row, direction)
        flow_confirm_score = cls._flow_confirm_score(row, rotation, sector_scores, direction)
        fundamental_confirm_score = cls._fundamental_score(row, direction)
        final_score = cls._clip_score(
            theme_score * 0.28
            + exposure_score * 0.24
            + price_confirm_score * 0.24
            + flow_confirm_score * 0.16
            + fundamental_confirm_score * 0.08
            + factor_bonus,
            default=0.0,
        )
        rationale_parts = [
            f"主题命中 {len(hits)} 个关键词",
            f"20日收益 {cls._safe_float(row.get('return_20d')):.1%}",
            f"价格偏离 MA20 {cls._safe_float(row.get('price_ma20_gap')):.1%}",
        ]
        return {
            "symbol": str(row.get("code") or "").strip(),
            "direction": direction,
            "theme_score": round(theme_score, 4),
            "exposure_score": round(exposure_score, 4),
            "price_confirm_score": round(price_confirm_score, 4),
            "flow_confirm_score": round(flow_confirm_score, 4),
            "fundamental_confirm_score": round(fundamental_confirm_score, 4),
            "final_score": round(final_score, 4),
            "rationale": "；".join(rationale_parts),
            "evidence": {
                "engine": LOCAL_EVENT_ENGINE_NAME,
                "theme_code": theme.get("theme_code"),
                "theme_name": theme.get("theme_name"),
                "alias_hits": hits,
                "direction": direction,
                "metrics": {
                    "return_5d": row.get("return_5d"),
                    "return_20d": row.get("return_20d"),
                    "price_ma20_gap": row.get("price_ma20_gap"),
                    "volume_ratio": row.get("volume_ratio"),
                    "trend_state": row.get("trend_state"),
                },
                "company": {
                    "code": row.get("code"),
                    "name": row.get("name"),
                    "industry": row.get("industry"),
                    "sector": row.get("sector"),
                    "market": row.get("market"),
                    "market_cap": row.get("market_cap"),
                },
            },
        }

    @classmethod
    def _build_event_payload(
        cls,
        theme: dict,
        direction: str,
        rows: List[dict],
        scored_signals: List[dict],
        rotation: dict,
        factor_reasons: List[str],
    ) -> Optional[dict]:
        if not scored_signals:
            return None
        top_signals = sorted(scored_signals, key=lambda item: cls._safe_float(item.get("final_score")), reverse=True)
        avg_final_score = sum(cls._safe_float(item.get("final_score")) for item in top_signals) / max(len(top_signals), 1)
        max_final_score = max(cls._safe_float(item.get("final_score")) for item in top_signals)
        if len(top_signals) < 2 and max_final_score < 0.72:
            return None
        if avg_final_score < 0.52 and max_final_score < 0.68:
            return None
        avg_return = sum(cls._safe_float(item.get("return_20d")) for item in rows) / max(len(rows), 1)
        intensity = cls._clip_score(abs(avg_return) * 8 + avg_final_score * 0.25, default=avg_final_score)
        confidence = cls._clip_score(avg_final_score * 0.75 + max_final_score * 0.25, default=avg_final_score)
        event_id = f"local_theme_{theme.get('theme_code')}_{direction}"
        top_symbols = [item.get("symbol") for item in top_signals[:5] if item.get("symbol")]
        supporting_reasons = [
            f"主题股票 20 日均值收益 {avg_return:.1%}",
            f"Top 信号均值 {avg_final_score:.2f}，最高 {max_final_score:.2f}",
        ]
        sectors = sorted(
            {
                str(item.get("sector") or item.get("industry") or "").strip()
                for item in rows
                if str(item.get("sector") or item.get("industry") or "").strip()
            }
        )
        if direction == "positive" and rotation.get("hot_sectors"):
            supporting_reasons.append(
                "热点行业匹配: "
                + ", ".join([item for item in sectors if item in set(rotation.get("hot_sectors") or [])][:3])
            )
        if direction == "negative" and rotation.get("cold_sectors"):
            supporting_reasons.append(
                "承压行业匹配: "
                + ", ".join([item for item in sectors if item in set(rotation.get("cold_sectors") or [])][:3])
            )
        supporting_reasons.extend([reason for reason in factor_reasons if reason])
        supporting_reasons = [item for item in supporting_reasons if item and not item.endswith(": ")][:4]
        theme_payload = {
            "theme_code": theme.get("theme_code"),
            "theme_name": theme.get("theme_name"),
            "direction": direction,
            "horizon": ((theme.get("metadata") or {}).get("horizon") or "swing_5_20d"),
            "signal_count": len(top_signals),
            "target_symbols": top_symbols,
            "preferred_strategy_types": list(((theme.get("metadata") or {}).get("strategy_preferences") or [])),
            "strategy_preferences": list(((theme.get("metadata") or {}).get("strategy_preferences") or [])),
            "preference_strength": str(((theme.get("metadata") or {}).get("preference_strength") or "medium")),
            "preference_reason": " / ".join(supporting_reasons[:2]) or f"theme:{theme.get('theme_code')}",
            "supporting_reasons": supporting_reasons,
            "score_summary": {
                "avg_final_score": round(avg_final_score, 4),
                "max_final_score": round(max_final_score, 4),
                "top_symbols": top_symbols,
            },
        }
        return {
            "event_id": event_id,
            "event_type": str(((theme.get("metadata") or {}).get("event_type") or "theme_rotation")),
            "event_name": f"{theme.get('theme_name')}相对强势" if direction == "positive" else f"{theme.get('theme_name')}相对承压",
            "event_scope": "market",
            "summary": (
                f"本地 DB 相对强弱规则显示 {theme.get('theme_name')} "
                f"{'走强' if direction == 'positive' else '走弱'}，"
                f"候选股票围绕 {', '.join(top_symbols[:3])}。"
            ),
            "direction": direction,
            "intensity": round(intensity, 4),
            "confidence": round(confidence, 4),
            "horizon": ((theme.get("metadata") or {}).get("horizon") or "swing_5_20d"),
            "source_count": len(top_signals),
            "source_types": [LOCAL_EVENT_ENGINE_NAME, "price_relative_strength"],
            "entities": top_symbols,
            "commodities": [],
            "regions": [],
            "themes": [theme_payload],
            "evidence": {
                "engine": LOCAL_EVENT_ENGINE_NAME,
                "rotation": rotation,
                "supporting_reasons": supporting_reasons,
            },
            "status": "active",
        }

    async def _ensure_theme_definitions(self, db) -> int:
        if not callable(getattr(db, "save_factory_theme_definition", None)):
            return 0
        count = 0
        for item in _DEFAULT_THEME_LIBRARY:
            await _call_optional_async(db, "save_factory_theme_definition", item, default=None)
            count += 1
        return count

    async def _load_universe_state(self, db, limit: int = 120) -> List[dict]:
        """优化：并发加载股票行情（原为串行，120只×50ms=6s → 并发后~0.5s）。"""
        import asyncio as _asyncio
        rows = await _call_optional_async(db, "list_stock_universe", limit=limit, offset=0, default=[])
        if not isinstance(rows, (list, tuple)):
            rows = []
        valid_rows = [row for row in list(rows or []) if str((row or {}).get("code") or "").strip()]
        if not valid_rows:
            return []

        sem = _asyncio.Semaphore(20)  # 限制并发度避免 DB 过载

        async def _fetch_one(row: dict) -> dict:
            code = str(row.get("code") or "").strip()
            async with sem:
                try:
                    klines = await _call_optional_async(db, "get_klines", code, limit=30, default=[])
                except TypeError:
                    klines = await _call_optional_async(db, "get_klines", code, default=[])
                metrics = self._summarize_klines(list(klines or []))
                return {**dict(row or {}), **metrics}

        results = await _asyncio.gather(
            *[_fetch_one(row) for row in valid_rows],
            return_exceptions=True,
        )
        return [item for item in results if isinstance(item, dict)]

    async def _deactivate_stale_local_events(self, db, active_event_ids: set[str]) -> int:
        if not callable(getattr(db, "list_factory_event_clusters", None)) or not callable(getattr(db, "save_factory_event_cluster", None)):
            return 0
        rows = await _call_optional_async(db, "list_factory_event_clusters", status=None, limit=200, default=[])
        if not isinstance(rows, (list, tuple)):
            rows = []
        deactivated = 0
        for row in list(rows or []):
            event_id = str((row or {}).get("event_id") or "").strip()
            if not event_id.startswith("local_theme_") or event_id in active_event_ids:
                continue
            if str((row or {}).get("status") or "active") == "inactive":
                continue
            await _call_optional_async(
                db,
                "save_factory_event_cluster",
                {
                    **dict(row or {}),
                    "status": "inactive",
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                },
                default=None,
            )
            deactivated += 1
        return deactivated

    async def refresh(self, db, snapshot: Optional[dict] = None) -> dict:
        snapshot = snapshot if snapshot is not None else {}
        persistence_enabled = callable(getattr(db, "save_factory_event_cluster", None)) and callable(
            getattr(db, "save_factory_event_signal", None)
        )

        theme_definition_count = (
            await self._ensure_theme_definitions(db)
            if callable(getattr(db, "save_factory_theme_definition", None))
            else 0
        )
        universe_state = await self._load_universe_state(db)
        if not universe_state:
            return {
                "engine": LOCAL_EVENT_ENGINE_NAME,
                "enabled": False,
                "persistence_enabled": persistence_enabled,
                "theme_definition_count": theme_definition_count,
                "universe_size": 0,
                "market_internals": {
                    "engine": LOCAL_EVENT_ENGINE_NAME,
                    "symbol_count": 0,
                    "hot_sectors": [],
                    "cold_sectors": [],
                    "margin_proxy_5d_change_pct": 0.0,
                },
                "reason": "stock_universe empty",
            }

        sector_scores = self._aggregate_groups(universe_state, "sector")
        rotation = self._derive_sector_rotation(snapshot, sector_scores)
        market_internals = self._build_market_internals(universe_state, rotation)
        if callable(getattr(db, "save_factory_market_internal_snapshot", None)):
            await _call_optional_async(
                db,
                "save_factory_market_internal_snapshot",
                {
                    "snapshot_date": snapshot.get("date") or datetime.now(timezone.utc).date(),
                    **market_internals,
                    "metadata": {
                        "rotation": rotation,
                        "sector_count": len(sector_scores),
                        "theme_definition_count": theme_definition_count,
                    },
                },
                default=None,
            )
        if not persistence_enabled:
            return {
                "engine": LOCAL_EVENT_ENGINE_NAME,
                "enabled": False,
                "persistence_enabled": False,
                "theme_definition_count": theme_definition_count,
                "universe_size": len(universe_state),
                "sector_count": len(sector_scores),
                "market_internals": market_internals,
                "reason": "factory event tables unavailable",
            }
        active_event_ids: set[str] = set()
        event_count = 0
        signal_count = 0
        exposure_count = 0
        active_theme_count = 0
        now = datetime.now(timezone.utc).isoformat()

        for theme in _DEFAULT_THEME_LIBRARY:
            matched_rows: List[dict] = []
            hit_map: Dict[str, List[str]] = {}
            aliases = list(theme.get("aliases") or [])
            for row in universe_state:
                hits = self._alias_hits(row, aliases)
                code = str(row.get("code") or "").strip()
                if hits and code:
                    matched_rows.append(row)
                    hit_map[code] = hits
            if not matched_rows:
                continue

            matched_rows.sort(
                key=lambda item: (self._safe_float(item.get("market_cap")), self._safe_float(item.get("return_20d"))),
                reverse=True,
            )
            direction = self._determine_direction(matched_rows, rotation)
            if direction == "neutral":
                continue
            factor_bonus, factor_reasons = self._factor_support(theme, snapshot, direction)
            scored_signals: List[dict] = []
            for rank, row in enumerate(matched_rows, 1):
                code = str(row.get("code") or "").strip()
                scored = self._score_theme_symbol(
                    theme,
                    row,
                    hit_map.get(code, []),
                    direction,
                    rank,
                    len(matched_rows),
                    rotation,
                    sector_scores,
                    factor_bonus,
                )
                if self._safe_float(scored.get("final_score")) >= 0.45:
                    scored_signals.append(
                        {
                            **scored,
                            **{
                                key: row.get(key)
                                for key in ("name", "industry", "sector", "market_cap", "return_20d")
                            },
                        }
                    )
            event_payload = self._build_event_payload(theme, direction, matched_rows, scored_signals, rotation, factor_reasons)
            if event_payload is None:
                continue

            event_id = str(event_payload.get("event_id") or "").strip()
            active_event_ids.add(event_id)
            await _call_optional_async(
                db,
                "save_factory_event_cluster",
                {
                    **event_payload,
                    "occurred_at": now,
                    "last_seen_at": now,
                },
                default=None,
            )
            active_theme_count += 1
            event_count += 1

            top_signals = sorted(scored_signals, key=lambda item: self._safe_float(item.get("final_score")), reverse=True)[:8]
            for signal in top_signals:
                signal_count += 1
                if callable(getattr(db, "save_factory_company_theme_exposure", None)):
                    await _call_optional_async(
                        db,
                        "save_factory_company_theme_exposure",
                        {
                            "symbol": signal.get("symbol"),
                            "theme_code": theme.get("theme_code"),
                            "exposure_type": "keyword_match",
                            "direction": direction,
                            "exposure_score": signal.get("exposure_score"),
                            "evidence": signal.get("evidence") or {},
                        },
                        default=None,
                    )
                    exposure_count += 1
                await _call_optional_async(
                    db,
                    "save_factory_event_signal",
                    {
                        "event_id": event_id,
                        "symbol": signal.get("symbol"),
                        "theme_code": theme.get("theme_code"),
                        "direction": direction,
                        "theme_score": signal.get("theme_score"),
                        "exposure_score": signal.get("exposure_score"),
                        "price_confirm_score": signal.get("price_confirm_score"),
                        "flow_confirm_score": signal.get("flow_confirm_score"),
                        "fundamental_confirm_score": signal.get("fundamental_confirm_score"),
                        "final_score": signal.get("final_score"),
                        "rationale": signal.get("rationale"),
                        "evidence": signal.get("evidence") or {},
                        "observed_at": now,
                    },
                    default=None,
                )

        stale_count = await self._deactivate_stale_local_events(db, active_event_ids)
        return {
            "engine": LOCAL_EVENT_ENGINE_NAME,
            "enabled": True,
            "persistence_enabled": True,
            "theme_definition_count": theme_definition_count,
            "universe_size": len(universe_state),
            "sector_count": len(sector_scores),
            "hot_sectors": rotation.get("hot_sectors") or [],
            "cold_sectors": rotation.get("cold_sectors") or [],
            "market_internals": market_internals,
            "active_theme_count": active_theme_count,
            "event_count": event_count,
            "signal_count": signal_count,
            "exposure_count": exposure_count,
            "deactivated_event_count": stale_count,
        }


_local_event_engine: Optional[LocalEventDrivenResearchEngine] = None


def get_local_event_engine() -> LocalEventDrivenResearchEngine:
    global _local_event_engine
    if _local_event_engine is None:
        _local_event_engine = LocalEventDrivenResearchEngine()
    return _local_event_engine


__all__ = [
    "LOCAL_EVENT_ENGINE_NAME",
    "LocalEventDrivenResearchEngine",
    "get_local_event_engine",
]
