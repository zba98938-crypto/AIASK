"""Shared sector taxonomy helpers for strategy-factory scoring and projection."""

from __future__ import annotations

from typing import Any, Iterable


_SECTOR_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "key": "high_dividend_finance",
        "canonical": "高股息金融",
        "parent": "defensive",
        "aliases": (
            "高股息金融",
        ),
        "intrinsic_families": ("quality_factor", "value_factor", "ma_cross"),
        "hot_families": ("quality_factor", "value_factor", "ma_cross"),
        "cold_families": ("rsi", "quality_factor", "ma_cross"),
    },
    {
        "key": "bank_dividend",
        "canonical": "银行红利",
        "parent": "defensive",
        "aliases": ("银行",),
        "intrinsic_families": ("value_factor", "mean_reversion_short", "quality_factor"),
        "hot_families": ("value_factor", "mean_reversion_short", "quality_factor"),
        "cold_families": ("rsi", "quality_factor", "ma_cross"),
    },
    {
        "key": "insurance_dividend",
        "canonical": "保险保障",
        "parent": "defensive",
        "aliases": ("保险",),
        "intrinsic_families": ("quality_factor", "value_factor", "ma_cross"),
        "hot_families": ("quality_factor", "value_factor", "ma_cross"),
        "cold_families": ("rsi", "quality_factor", "ma_cross"),
    },
    {
        "key": "telecom_dividend",
        "canonical": "运营商红利",
        "parent": "defensive",
        "aliases": ("运营商", "电信运营"),
        "intrinsic_families": ("quality_factor", "ma_cross", "momentum"),
        "hot_families": ("quality_factor", "ma_cross", "momentum"),
        "cold_families": ("value_factor", "rsi", "ma_cross"),
    },
    {
        "key": "utility_dividend",
        "canonical": "公用红利",
        "parent": "defensive",
        "aliases": ("公用事业", "电力", "高速公路"),
        "intrinsic_families": ("value_factor", "quality_factor", "ma_cross"),
        "hot_families": ("value_factor", "quality_factor", "ma_cross"),
        "cold_families": ("rsi", "value_factor", "ma_cross"),
    },
    {
        "key": "chip_semiconductor",
        "canonical": "芯片半导体",
        "parent": "technology",
        "aliases": (
            "芯片半导体",
            "半导体",
            "芯片",
            "元器件",
            "电子元件",
            "集成电路",
            "电子",
        ),
        "intrinsic_families": ("growth_factor", "volatility_breakout", "momentum"),
        "hot_families": ("growth_factor", "momentum", "volatility_breakout"),
        "cold_families": ("rsi", "quality_factor", "ma_cross"),
    },
    {
        "key": "communication_equipment",
        "canonical": "通信设备",
        "parent": "technology",
        "aliases": (
            "通信设备",
            "通信",
        ),
        "intrinsic_families": ("momentum", "volatility_breakout", "growth_factor"),
        "hot_families": ("momentum", "volatility_breakout", "growth_factor"),
        "cold_families": ("quality_factor", "rsi", "ma_cross"),
    },
    {
        "key": "internet_platform",
        "canonical": "互联网平台",
        "parent": "technology",
        "aliases": (
            "互联网",
            "传媒",
        ),
        "intrinsic_families": ("ma_cross", "momentum", "multi_factor"),
        "hot_families": ("ma_cross", "momentum", "growth_factor"),
        "cold_families": ("quality_factor", "rsi", "ma_cross"),
    },
    {
        "key": "software_services",
        "canonical": "软件服务",
        "parent": "technology",
        "aliases": (
            "软件",
            "软件服务",
            "计算机",
            "服务器",
            "算力",
            "云计算",
        ),
        "intrinsic_families": ("growth_factor", "quality_factor", "ma_cross"),
        "hot_families": ("growth_factor", "momentum", "quality_factor"),
        "cold_families": ("quality_factor", "rsi", "ma_cross"),
    },
    {
        "key": "new_energy_equipment",
        "canonical": "新能源装备",
        "parent": "industrial_growth",
        "aliases": (
            "新能源装备",
            "新能源",
            "电气设备",
            "电池",
            "储能",
            "光伏",
            "风电",
            "整车",
            "汽车零部件",
        ),
        "intrinsic_families": ("growth_factor", "ma_cross", "quality_factor"),
        "hot_families": ("growth_factor", "momentum", "ma_cross"),
        "cold_families": ("quality_factor", "value_factor", "ma_cross"),
    },
    {
        "key": "upstream_oil_gas",
        "canonical": "上游油气",
        "parent": "commodities",
        "aliases": (
            "上游油气",
            "石油",
            "油气",
            "原油",
            "炼化",
            "油服",
            "煤炭",
        ),
        "intrinsic_families": ("sector_rotation", "momentum", "ma_cross"),
        "hot_families": ("sector_rotation", "momentum", "ma_cross"),
        "cold_families": ("value_factor", "quality_factor", "rsi"),
    },
    {
        "key": "resource_metals",
        "canonical": "资源周期",
        "parent": "commodities",
        "aliases": (
            "资源周期",
            "有色",
            "小金属",
            "稀土",
            "铜",
            "铝",
            "黄金",
            "矿业",
            "钢铁",
            "化工",
            "化工原料",
        ),
        "intrinsic_families": ("sector_rotation", "momentum", "value_factor"),
        "hot_families": ("sector_rotation", "momentum", "value_factor"),
        "cold_families": ("value_factor", "quality_factor", "rsi"),
    },
    {
        "key": "consumer_quality",
        "canonical": "消费龙头",
        "parent": "consumer",
        "aliases": (
            "消费龙头",
            "白酒",
            "消费",
            "家电",
            "食品",
            "饮料",
            "商贸零售",
        ),
        "intrinsic_families": ("quality_factor", "momentum", "ma_cross"),
        "hot_families": ("quality_factor", "momentum", "ma_cross"),
        "cold_families": ("value_factor", "rsi", "quality_factor"),
    },
    {
        "key": "healthcare_innovation",
        "canonical": "医药创新",
        "parent": "healthcare",
        "aliases": (
            "医药创新",
            "医药",
            "医疗",
            "创新药",
            "生物",
            "器械",
        ),
        "intrinsic_families": ("growth_factor", "quality_factor", "momentum"),
        "hot_families": ("growth_factor", "momentum", "quality_factor"),
        "cold_families": ("quality_factor", "value_factor", "rsi"),
    },
)


def _normalize_sector_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return ""
    for char in (" ", "_", "-", "/", "\\", "(", ")", "[", "]", "{", "}", "（", "）", "、", ",", "，", "."):
        token = token.replace(char, "")
    return token


def sector_profiles_for_label(value: Any) -> list[dict[str, Any]]:
    normalized = _normalize_sector_token(value)
    if not normalized:
        return []

    matched: list[dict[str, Any]] = []
    for profile in _SECTOR_PROFILES:
        aliases = [
            _normalize_sector_token(item)
            for item in [profile.get("canonical"), *(profile.get("aliases") or ())]
            if _normalize_sector_token(item)
        ]
        if not aliases:
            continue
        if normalized in aliases:
            matched.append(profile)
            continue
        if any(
            len(alias) >= 2 and (alias in normalized or normalized in alias)
            for alias in aliases
        ):
            matched.append(profile)
    return matched


def canonical_sector_label(value: Any) -> str | None:
    profiles = sector_profiles_for_label(value)
    if profiles:
        return str(profiles[0].get("canonical") or "").strip() or None
    token = str(value or "").strip()
    return token or None


def normalize_sector_labels(values: Iterable[Any] | None, *, limit: int | None = None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        label = canonical_sector_label(raw)
        if not label or label in seen:
            continue
        seen.add(label)
        normalized.append(label)
        if limit and len(normalized) >= limit:
            break
    return normalized


def sector_family_biases(value: Any, *, mode: str = "intrinsic") -> list[str]:
    biases: list[str] = []
    field_name = {
        "intrinsic": "intrinsic_families",
        "hot": "hot_families",
        "cold": "cold_families",
    }.get(str(mode or "intrinsic").strip().lower(), "intrinsic_families")
    for profile in sector_profiles_for_label(value):
        for family in list(profile.get(field_name) or []):
            normalized_family = str(family or "").strip().lower()
            if normalized_family and normalized_family not in biases:
                biases.append(normalized_family)
    return biases


def sector_match_strength(industry: Any, sector_labels: Iterable[Any] | None) -> float:
    industry_token = _normalize_sector_token(industry)
    if not industry_token:
        return 0.0

    industry_profiles = sector_profiles_for_label(industry)
    industry_keys = {str(profile.get("key") or "").strip() for profile in industry_profiles if str(profile.get("key") or "").strip()}
    industry_parents = {str(profile.get("parent") or "").strip() for profile in industry_profiles if str(profile.get("parent") or "").strip()}
    best = 0.0
    for raw_label in list(sector_labels or []):
        label_token = _normalize_sector_token(raw_label)
        if not label_token:
            continue
        if label_token == industry_token:
            best = max(best, 1.0)
            continue
        label_profiles = sector_profiles_for_label(raw_label)
        label_keys = {str(profile.get("key") or "").strip() for profile in label_profiles if str(profile.get("key") or "").strip()}
        label_parents = {str(profile.get("parent") or "").strip() for profile in label_profiles if str(profile.get("parent") or "").strip()}
        if industry_keys and label_keys and industry_keys & label_keys:
            best = max(best, 1.0)
            continue
        if industry_parents and label_parents and industry_parents & label_parents:
            best = max(best, 0.5)
            continue
        if len(label_token) >= 2 and (label_token in industry_token or industry_token in label_token):
            best = max(best, 0.75)
    return round(best, 4)
