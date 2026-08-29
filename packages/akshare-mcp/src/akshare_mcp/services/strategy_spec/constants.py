"""Constants for strategy-spec generation."""

from __future__ import annotations

import os

from ...env_loader import load_mcp_env


load_mcp_env(override=False, only_prefixes=("STRATEGY_LLM_",))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))



DEFAULT_CODES = ['000300', '600519', '000858', '601318']
RESEARCH_UNIVERSE_PAGE_SIZE = _env_int('STRATEGY_LLM_RESEARCH_PAGE_SIZE', 120, minimum=20, maximum=500)
RESEARCH_UNIVERSE_SCAN_LIMIT = _env_int('STRATEGY_LLM_RESEARCH_SCAN_LIMIT', 300, minimum=20, maximum=2000)
RESEARCH_KLINE_SCAN_LIMIT = _env_int('STRATEGY_LLM_RESEARCH_KLINE_SCAN_LIMIT', 60, minimum=10, maximum=300)
RESEARCH_SYMBOL_DETAIL_LIMIT = _env_int('STRATEGY_LLM_RESEARCH_SYMBOL_DETAIL_LIMIT', 24, minimum=4, maximum=80)
RESEARCH_CANDIDATE_POOL_LIMIT = _env_int('STRATEGY_LLM_RESEARCH_CANDIDATE_POOL_LIMIT', 12, minimum=3, maximum=40)
RESEARCH_FINANCIAL_DETAIL_LIMIT = _env_int('STRATEGY_LLM_RESEARCH_FINANCIAL_DETAIL_LIMIT', 8, minimum=2, maximum=20)

_FACTOR_VALIDATION_TYPES = {"value_factor", "quality_factor", "growth_factor", "multi_factor"}
_TREND_EXECUTABLE_DSL_TYPES = {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"}
_PROXY_RUNTIME_FACTOR_TYPES = {"quality_factor", "value_factor", "growth_factor"}
_HIGH_VOL_BOARD_BUCKETS = {"star", "chinext", "beijing"}
_EMPTY_VALUES = (None, "", [], {})
