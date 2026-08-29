"""Versioned spawn policy registry for StrategySpawner."""

from __future__ import annotations


SPAWN_POLICY_VERSION_V1_COMPAT = "v1_compat"

EVENT_FOCUS_TARGETS_BY_KEYWORD_V1_COMPAT = {
    "高股息": ("601318", "600036", "601166", "601288", "601398"),
    "红利": ("601318", "600036", "601166", "601288", "601398"),
    "金融": ("601318", "600036", "601166", "601288", "601398"),
    "银行": ("600036", "601166", "601288", "601398", "600000"),
    "保险": ("601318", "601601", "601628", "601336"),
    "消费": ("600519", "000858", "600809", "603288", "000333"),
    "半导体": ("688981", "603986", "688111", "600584", "300661"),
    "算力": ("300308", "603019", "002261", "000977", "601138"),
    "ai": ("300308", "603019", "002261", "000977", "688111"),
    "通信": ("000063", "601138", "300394", "603083"),
    "电子": ("002371", "002475", "603986", "688981", "300661"),
    "新能源": ("300750", "002594", "601012", "300274", "002460"),
    "医药": ("300015", "600276", "603259", "300122", "688271"),
    "油气": ("600938", "600028", "601857", "600256"),
    "上游": ("600938", "600028", "601857", "600256"),
}

EVENT_READY_SOURCE_WEIGHTS_V1_COMPAT = {
    True: {
        "event_driven": 1.0,
        "fear_greed": 0.75,
        "factor_ic": 1.0,
        "volatility": 0.70,
        "fund_flow": 0.80,
    },
    False: {
        "event_driven": 1.0,
        "fear_greed": 0.45,
        "factor_ic": 1.0,
        "volatility": 0.40,
        "fund_flow": 0.50,
    },
}


def get_spawn_policy_version() -> str:
    return SPAWN_POLICY_VERSION_V1_COMPAT


def get_event_focus_targets_by_keyword() -> dict[str, tuple[str, ...]]:
    return dict(EVENT_FOCUS_TARGETS_BY_KEYWORD_V1_COMPAT)


def get_event_ready_source_weights(*, event_ready_supplemental: bool) -> dict[str, float]:
    return dict(EVENT_READY_SOURCE_WEIGHTS_V1_COMPAT[bool(event_ready_supplemental)])


__all__ = [
    "EVENT_FOCUS_TARGETS_BY_KEYWORD_V1_COMPAT",
    "EVENT_READY_SOURCE_WEIGHTS_V1_COMPAT",
    "SPAWN_POLICY_VERSION_V1_COMPAT",
    "get_event_focus_targets_by_keyword",
    "get_event_ready_source_weights",
    "get_spawn_policy_version",
]
