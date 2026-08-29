"""向量搜索工具 - 基于特征相似度的实现"""

from __future__ import annotations

import asyncio

from ..storage import get_db as _default_get_db
from ..services.vector_search import vector_search_engine as _default_vector_search_engine
from ..utils import propagate_data_quality_to_top
from . import _vector_common as _vector_common_mod
from . import _vector_search_kline as _vector_search_kline_mod
from . import _vector_search_semantic as _vector_search_semantic_mod
from . import _vector_search_similar as _vector_search_similar_mod

get_db = _default_get_db
technical_analysis = _vector_common_mod.technical_analysis
factor_calculator = _vector_common_mod.factor_calculator
vector_search_engine = _default_vector_search_engine

_vector_lock = asyncio.Lock()


def _sync_vector_overrides() -> None:
    """Keep vector submodules aligned with vector-level monkeypatches."""
    _vector_common_mod.get_db = get_db
    _vector_common_mod.technical_analysis = technical_analysis
    _vector_common_mod.factor_calculator = factor_calculator
    _vector_common_mod.vector_search_engine = vector_search_engine
    _vector_search_kline_mod.get_db = get_db
    _vector_search_kline_mod.vector_search_engine = vector_search_engine
    _vector_search_semantic_mod.get_db = get_db
    _vector_search_similar_mod.get_db = get_db


async def search_similar_stocks(
    code: str,
    top_n: int = 10,
    similarity_type: str = 'both',
    search_backend: str = 'db',
    allow_fallback: bool = True,
):
    """搜索相似股票 - 基于股票画像向量或基本面/技术面特征相似度"""
    async with _vector_lock:
        _sync_vector_overrides()
        return propagate_data_quality_to_top(await _vector_search_similar_mod.search_similar_stocks(
            code=code, top_n=top_n, similarity_type=similarity_type,
            search_backend=search_backend, allow_fallback=allow_fallback,
        ))


async def search_by_kline(
    code: str,
    days: int = 20,
    top_n: int = 10,
    search_backend: str = 'db',
    allow_fallback: bool = True,
):
    """基于K线形态搜索相似股票 - 使用向量搜索引擎"""
    async with _vector_lock:
        _sync_vector_overrides()
        return propagate_data_quality_to_top(await _vector_search_kline_mod.search_by_kline(
            code=code, days=days, top_n=top_n,
            search_backend=search_backend, allow_fallback=allow_fallback,
        ))


async def semantic_stock_search(
    query: str,
    limit: int = 20,
):
    """语义化股票搜索 - 基于关键词匹配（支持中文分词、行业关键词、股票代码/名称）"""
    async with _vector_lock:
        _sync_vector_overrides()
        return propagate_data_quality_to_top(await _vector_search_semantic_mod.semantic_stock_search(
            query=query, limit=limit,
        ))


def register(mcp):
    """注册向量搜索工具"""
    mcp.tool()(search_similar_stocks)
    mcp.tool()(search_by_kline)
    mcp.tool()(semantic_stock_search)
