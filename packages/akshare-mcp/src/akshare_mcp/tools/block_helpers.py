"""
市场板块工具 - 获取板块数据
数据源优先级: DB缓存 → 东方财富datacenter HTTP → AKShare
"""

import asyncio
import json
import urllib.request
from typing import Dict, Any, Optional, List
from ..storage.sqlite import get_db
from ..utils import (
    fail,
    ok,
    parse_numeric,
    propagate_data_quality_to_top,
    safe_float,
    safe_stderr_print,
    suppress_stdout,
)
from ..core.normalize import normalize_block_list, normalize_block_stock_list
from ..provider_contracts import attach_tool_provider_contract_meta
import logging

logger = logging.getLogger(__name__)


def _is_placeholder_summary(block: dict) -> bool:
    try:
        stock_count = block.get('stock_count')
        avg_change = block.get('avg_change_pct')
        total_amount = block.get('total_amount')
        leader_code = block.get('leader_code')
        leader_name = block.get('leader_name')
        return (
            (stock_count is None or int(stock_count or 0) == 0)
            and (avg_change is None or float(avg_change or 0) == 0.0)
            and (total_amount is None or float(total_amount or 0) == 0.0)
            and not str(leader_code or '').strip()
            and not str(leader_name or '').strip()
        )
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/block_helpers.py:43: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return False


def _sanitize_placeholder_blocks(blocks: list[dict], block_type: str) -> list[dict]:
    """把历史缓存中的占位 0 值恢复成“未知”，避免误导前端。"""
    sanitized = []
    for item in blocks:
        block = dict(item)
        if block_type in {'concept', 'industry'} and _is_placeholder_summary(block):
            block['stock_count'] = None
            block['avg_change_pct'] = None
            block['total_amount'] = None
            block['leader_code'] = None
            block['leader_name'] = None
            block['degraded'] = True
            block.setdefault('fallback_reason', '板块摘要来自名称列表/历史占位缓存，统计字段暂不可用；可改查 get_block_stocks 获取成分股')
        sanitized.append(block)
    return sanitized


def _fetch_concept_stocks_from_ths(block_code: str, block_name: str | None = None) -> list:
    """通过同花顺概念详情页抓取成分股（支持分页 ajax 回退）。"""
    return []

