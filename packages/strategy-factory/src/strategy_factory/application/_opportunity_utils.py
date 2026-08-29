"""Shared helpers for market opportunity scanning."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from ..domain.constants import (
    AUTONOMY_CANDIDATES_PER_TASK,
    AUTONOMY_MAX_RESEARCH_TASKS,
    EVENT_SNAPSHOT_MIX_MAX,
    EVENT_TASK_GENERATION_LIMIT_MAX,
    OPPORTUNITY_MAX_PER_INDUSTRY,
    OPPORTUNITY_TARGET_SYMBOLS_PER_TASK,
)
from ..domain.targets import _normalize_research_task_contract


class _MarketOpportunityScannerUtilityMixin:
    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _clip_score(value: Any, default: float = 0.0) -> float:
        try:
            score = float(value)
        except Exception:
            score = float(default)
        return max(0.0, min(score, 1.0))

    @staticmethod
    def _normalize_codes(values: Any, limit: int = 5) -> List[str]:
        from ..domain.targets import _normalize_target_codes

        return _normalize_target_codes(values, limit=limit)

    @classmethod
    def _summarize_symbol(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": row.get("code"),
            "name": row.get("name"),
            "industry": row.get("industry"),
            "sector": row.get("sector") or row.get("industry"),
            "market": row.get("market"),
            "market_cap": cls._safe_float(row.get("market_cap")),
            "pe_ratio": row.get("pe_ratio"),
            "pb_ratio": row.get("pb_ratio"),
        }

    @staticmethod
    def _deduplicate_tasks(tasks: List[dict]) -> List[dict]:
        unique: List[dict] = []
        seen: set[str] = set()
        for item in tasks:
            key = str(item.get("task_key") or item.get("task_id") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @staticmethod
    def _build_task_source_counts(tasks: List[dict]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in list(tasks or []):
            source = str(item.get("task_source") or "unknown").strip() or "unknown"
            counts[source] = counts.get(source, 0) + 1
        return counts

    @classmethod
    def _task_target_overlap(cls, left: dict, right: dict) -> float:
        left_codes = set(cls._normalize_codes(left.get("target_symbols"), limit=8))
        right_codes = set(cls._normalize_codes(right.get("target_symbols"), limit=8))
        if not left_codes or not right_codes:
            return 0.0
        base = min(len(left_codes), len(right_codes))
        if base <= 0:
            return 0.0
        return round(len(left_codes & right_codes) / base, 4)

    @staticmethod
    def _task_industries(task: dict) -> set[str]:
        return {
            str(item or "").strip()
            for item in list(task.get("focus_industries") or [])
            if str(item or "").strip()
        }

    @classmethod
    def _tasks_are_redundant_for_mix(cls, left: dict, right: dict) -> bool:
        if str(left.get("opportunity_type") or "").strip() != str(right.get("opportunity_type") or "").strip():
            return False
        if cls._task_target_overlap(left, right) >= 0.6:
            return True
        return bool(cls._task_industries(left) & cls._task_industries(right))

    @staticmethod
    def _snapshot_generation_limit(base_limit: Any, *, priority: int = 0) -> int:
        resolved = max(2, int(base_limit or AUTONOMY_CANDIDATES_PER_TASK))
        upper_bound = max(resolved, min(EVENT_TASK_GENERATION_LIMIT_MAX, AUTONOMY_CANDIDATES_PER_TASK + 2))
        bonus = 0
        if priority >= 85:
            bonus += 1
        if priority >= 100:
            bonus += 1
        return min(upper_bound, resolved + bonus)

    @staticmethod
    def _task_sort_key(item: dict) -> tuple[int, int, int]:
        source = str(item.get("task_source") or "").strip()
        return (
            1 if source == "event_driven" else 0,
            int(item.get("priority") or 0),
            int(item.get("generation_limit") or 0),
        )

    @staticmethod
    def _finalize_task(task: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_research_task_contract(task)
        if normalized.get("target_symbols") and not normalized.get("stock_pool"):
            normalized["stock_pool"] = {
                "selection_mode": "explicit",
                "symbols": list(normalized.get("target_symbols") or []),
            }
        return normalized

    @classmethod
    def _select_snapshot_tasks_for_event_mix(
        cls,
        event_tasks: List[dict],
        snapshot_tasks: List[dict],
    ) -> List[dict]:
        remaining = max(0, AUTONOMY_MAX_RESEARCH_TASKS - len(event_tasks))
        if remaining <= 0 or not snapshot_tasks:
            return []

        snapshot_limit = min(EVENT_SNAPSHOT_MIX_MAX, remaining)
        selected: List[dict] = []
        seen_themes = {
            str(item.get("theme") or "").strip()
            for item in list(event_tasks or [])
            if str(item.get("theme") or "").strip()
        }
        reference_tasks = list(event_tasks or [])

        for task in sorted(cls._deduplicate_tasks(snapshot_tasks), key=cls._task_sort_key, reverse=True):
            theme = str(task.get("theme") or "").strip()
            if theme and theme in seen_themes:
                continue
            if any(cls._tasks_are_redundant_for_mix(task, existing) for existing in reference_tasks):
                continue
            selected.append(task)
            if theme:
                seen_themes.add(theme)
            reference_tasks.append(task)
            if len(selected) >= snapshot_limit:
                break
        return selected

    @staticmethod
    def _match_rows(rows: List[dict], keywords: List[str]) -> List[dict]:
        normalized = [str(item or "").strip() for item in keywords if str(item or "").strip()]
        if not normalized:
            return []
        matched: List[dict] = []
        for row in rows:
            haystack = " ".join(
                [
                    str((row or {}).get("industry") or ""),
                    str((row or {}).get("sector") or ""),
                    str((row or {}).get("name") or ""),
                    str((row or {}).get("code") or ""),
                ]
            )
            if any(token in haystack for token in normalized):
                matched.append(row)
        return matched

    @staticmethod
    def _top_codes(rows: List[dict], limit: int = 5) -> List[str]:
        """按市值降序选股（基础方法，仅在无快照上下文时使用）。"""
        ranked = sorted(
            [dict(item or {}) for item in rows],
            key=lambda item: (float(item.get("market_cap") or 0.0), str(item.get("code") or "")),
            reverse=True,
        )
        codes: List[str] = []
        for row in ranked:
            code = str((row or {}).get("code") or "").strip()
            if code and code not in codes:
                codes.append(code)
            if len(codes) >= limit:
                break
        return codes

    @classmethod
    def _factor_scored_top_codes(
        cls,
        rows: List[dict],
        *,
        limit: int = 0,
        snapshot: dict | None = None,
        hot_sectors: List[str] | None = None,
        cold_sectors: List[str] | None = None,
        max_per_industry: int = 0,
        industry_diverse: bool = True,
        selection_pressure: Dict[str, float] | None = None,
        task_seed: Any = None,
        candidate_pool_limit: int = 0,
    ) -> List[str]:
        """
        综合因子+技术面+行业分散的动态选股方法，替代纯市值的 _top_codes。

        评分维度：
        - 市值（基础权重，取 log 归一化，避免大市值垄断）
        - 热点行业（热点行业额外加分）
        - 冷门行业（降权）
        - 因子 IC 趋势（来自 snapshot 的 factor_ic_trend）
        - 行业分散（每个行业最多选 max_per_industry 只）
        """
        _limit = limit if limit > 0 else OPPORTUNITY_TARGET_SYMBOLS_PER_TASK
        _max_per_industry = max_per_industry if max_per_industry > 0 else OPPORTUNITY_MAX_PER_INDUSTRY
        _hot = set(str(s).strip() for s in (hot_sectors or []) if str(s).strip())
        _cold = set(str(s).strip() for s in (cold_sectors or []) if str(s).strip())

        # 从 snapshot 提取因子 IC 趋势（rising 加分）
        factor_ic_trend: dict = {}
        if snapshot:
            factor_ic_trend = dict(snapshot.get("factor_ic_trend") or {})

        # 行业因子信号映射：哪些因子 rising 对哪些行业有利
        rising_factors = {
            k for k, v in factor_ic_trend.items() if str(v).lower() == "rising"
        }

        import math

        def _score_row(row: dict) -> float:
            score = 0.0
            # 1. 市值得分（log 归一化，避免龙头垄断）
            mc = float(row.get("market_cap") or 0.0)
            if mc > 0:
                score += min(math.log10(mc / 1e8 + 1) * 10, 40.0)  # 上限 40 分

            # 2. 行业热度得分
            industry = str(row.get("industry") or row.get("sector") or "").strip()
            if industry and industry in _hot:
                score += 25.0
            elif industry and industry in _cold:
                score -= 15.0

            # 3. 因子信号得分（基于 PE/PB 估值）
            pe = row.get("pe_ratio")
            pb = row.get("pb_ratio")
            # 价值因子 rising → 低估值加分
            if "value" in rising_factors or "reversal" in rising_factors:
                if pe and 0 < float(pe or 0) < 15:
                    score += 12.0
                if pb and 0 < float(pb or 0) < 1.5:
                    score += 8.0
            # 成长/动量因子 rising → 高估值不惩罚
            if "momentum" in rising_factors or "growth" in rising_factors:
                if pe and 15 < float(pe or 0) < 50:
                    score += 8.0

            # 4. 随机扰动（避免完全确定性，引入探索性）
            import hashlib
            code = str(row.get("code") or "").strip()
            if code:
                h = int(hashlib.md5(code.encode()).hexdigest()[:4], 16) % 100
                score += h * 0.05  # 最多 5 分的随机偏移

            return round(score, 4)

        cleaned_rows = [dict(item or {}) for item in rows if str((item or {}).get("code") or "").strip()]
        scored = [
            (cls._safe_float(_score_row(row)), row)
            for row in cleaned_rows
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        pressure = {
            str(code).strip(): float(value or 0.0)
            for code, value in dict(selection_pressure or {}).items()
            if str(code).strip()
        }
        pool_limit = max(
            _limit,
            int(candidate_pool_limit or 0),
            min(len(scored), max(_limit * 4, 24)),
        )
        candidate_pool = list(scored[:pool_limit]) if scored else []

        def _task_bias(code: str) -> float:
            seed_text = str(task_seed or "").strip()
            if not seed_text:
                return 0.0
            digest = hashlib.md5(f"{seed_text}|{code}".encode("utf-8")).hexdigest()
            return (int(digest[:6], 16) % 1000) / 1000.0

        codes: List[str] = []
        industry_count: dict[str, int] = {}

        def _pick_code(items: List[tuple[float, dict]], *, allow_industry_overflow: bool) -> str | None:
            best_code: str | None = None
            best_key: tuple[Any, ...] | None = None
            for score_val, row in items:
                code = str(row.get("code") or "").strip()
                if not code or code in codes:
                    continue
                industry = str(row.get("industry") or row.get("sector") or "未知").strip() or "未知"
                if (
                    industry_diverse
                    and not allow_industry_overflow
                    and industry_count.get(industry, 0) >= _max_per_industry
                ):
                    continue
                key = (
                    round(float(pressure.get(code, 0.0)), 4),
                    -(float(score_val) + _task_bias(code)),
                    code,
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best_code = code
            return best_code

        while len(codes) < _limit and candidate_pool:
            code = _pick_code(candidate_pool, allow_industry_overflow=False)
            if code is None:
                code = _pick_code(candidate_pool, allow_industry_overflow=True)
            if code is None:
                break
            row = next((item for _score, item in candidate_pool if str(item.get("code") or "").strip() == code), {})
            industry = str(row.get("industry") or row.get("sector") or "未知").strip() or "未知"
            codes.append(code)
            industry_count[industry] = industry_count.get(industry, 0) + 1

        # 若候选池过小或行业约束过强，补充剩余股票（不限行业）
        if len(codes) < _limit:
            for _score_val, row in scored:
                code = str(row.get("code") or "").strip()
                if code and code not in codes:
                    codes.append(code)
                if len(codes) >= _limit:
                    break

        return codes[:_limit]

    @staticmethod
    def _rows_by_code(rows: List[dict]) -> Dict[str, dict]:
        return {
            str((row or {}).get("code") or "").strip(): dict(row or {})
            for row in rows
            if str((row or {}).get("code") or "").strip()
        }

    @staticmethod
    def _compact_text(value: Any, limit: int = 120) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @classmethod
    def _focus_rows_for_family(
        cls,
        rows: List[dict],
        *,
        family_name: str,
        hot_sectors: List[str],
        cold_sectors: List[str],
    ) -> List[dict]:
        lowered = str(family_name or "").strip().lower()
        keywords: List[str] = []
        if any(
            token in lowered
            for token in ["momentum", "growth", "sentiment", "capital_flow", "event", "liquidity"]
        ):
            keywords = list(hot_sectors[:2])
        elif any(token in lowered for token in ["value", "quality", "reversal", "volatility"]):
            keywords = list(cold_sectors[:2] or hot_sectors[:1])
        elif hot_sectors:
            keywords = list(hot_sectors[:1])
        matched = cls._match_rows(rows, keywords)
        return matched or list(rows or [])

    @classmethod
    def _focus_industries_from_rows(cls, rows: List[dict], limit: int = 3) -> List[str]:
        items: List[str] = []
        for row in list(rows or []):
            industry = str((row or {}).get("industry") or (row or {}).get("sector") or "").strip()
            if industry and industry not in items:
                items.append(industry)
            if len(items) >= limit:
                break
        return items
