"""搜索工具"""

from copy import deepcopy
import time
from typing import Any, Optional
from ..storage import get_db
from ..utils import normalize_code
from ..data_source import data_source
from .manager_protocol import fail_with_meta, ok_with_meta
from .tool_catalog import (
    STANDARD_ENVELOPE_OUTPUT_SCHEMA,
    get_tool_contract as get_catalog_tool_contract,
)


_CATEGORY_DESCRIPTIONS = {
    "alerts": "预警与告警",
    "backtest": "回测与策略验证",
    "basic_data": "基础资料与静态数据",
    "compliance": "合规校验",
    "data_sync": "数据同步与预热",
    "decision": "投资决策与建议",
    "execution": "执行规划与拆单",
    "factor": "因子与量化研究",
    "finance": "财务与估值数据",
    "fund_flow": "资金流与席位资金",
    "industry_chain": "产业链与主题关联",
    "macro": "宏观与市场环境",
    "market": "行情、K线与盘口",
    "news": "新闻、公告与研报",
    "options": "期权与衍生品",
    "paper_trading": "模拟交易",
    "performance": "绩效与归因",
    "portfolio": "组合与持仓管理",
    "quant": "技术分析与量化工具",
    "research": "研究与洞察",
    "risk": "风险分析与压力测试",
    "screening": "选股与筛选",
    "search": "搜索与工具发现",
    "sector": "板块与行业分析",
    "semantic": "语义分析与日报",
    "sentiment": "情绪与舆情",
    "skills": "内置技能编排",
    "strategy": "策略工厂与策略超市",
    "technical": "技术指标与形态",
    "user": "用户画像与偏好",
    "vector": "向量检索与相似度分析",
    "watchlist": "自选股与分组",
}

_MODULE_CATEGORY_PREFIXES = (
    ("akshare_mcp.tools.market.", "market"),
    ("akshare_mcp.tools.news.", "news"),
    ("akshare_mcp.tools.semantic.", "semantic"),
)

_MODULE_CATEGORY_MAP = {
    "akshare_mcp.tools.alerts": "alerts",
    "akshare_mcp.tools.backtest": "backtest",
    "akshare_mcp.tools.basic_data": "basic_data",
    "akshare_mcp.tools.data_sync": "data_sync",
    "akshare_mcp.tools.data_warmup": "data_sync",
    "akshare_mcp.tools.decision": "decision",
    "akshare_mcp.tools.factor_profile": "factor",
    "akshare_mcp.tools.finance": "finance",
    "akshare_mcp.tools.fund_flow": "fund_flow",
    "akshare_mcp.tools.macro": "macro",
    "akshare_mcp.tools.market_blocks": "sector",
    "akshare_mcp.tools.options": "options",
    "akshare_mcp.tools.portfolio": "portfolio",
    "akshare_mcp.tools.quant": "quant",
    "akshare_mcp.tools.search": "search",
    "akshare_mcp.tools.sentiment": "sentiment",
    "akshare_mcp.tools.skills": "skills",
    "akshare_mcp.tools.technical": "technical",
    "akshare_mcp.tools.valuation": "finance",
    "akshare_mcp.tools.vector": "vector",
}

_MANAGER_CATEGORY_MAP = {
    "alerts_manager": "alerts",
    "backtest_manager": "backtest",
    "benchmark_manager": "backtest",
    "compliance_manager": "compliance",
    "comprehensive_manager": "research",
    "data_sync_manager": "data_sync",
    "decision_manager": "decision",
    "event_manager": "news",
    "execution_manager": "execution",
    "fundamental_analysis_manager": "finance",
    "industry_chain_manager": "industry_chain",
    "insight_manager": "research",
    "limit_up_manager": "market",
    "macro_manager": "macro",
    "market_insight_manager": "market",
    "options_manager": "options",
    "paper_trading_manager": "paper_trading",
    "performance_manager": "performance",
    "portfolio_manager": "portfolio",
    "quant_manager": "quant",
    "research_manager": "research",
    "risk_manager": "risk",
    "screener_manager": "screening",
    "sector_manager": "sector",
    "sentiment_manager": "sentiment",
    "strategy_manager": "strategy",
    "technical_analysis_manager": "technical",
    "trading_data_manager": "fund_flow",
    "user_manager": "user",
    "vector_search_manager": "vector",
    "watchlist_manager": "watchlist",
}

_TOOL_CATEGORY_MAP = {
    "available_tools": "search",
    "get_available_categories": "search",
    "get_market_blocks": "sector",
    "get_block_stocks": "sector",
    "get_block_trades": "fund_flow",
    "get_concept_fund_flow": "fund_flow",
    "get_dragon_tiger": "fund_flow",
    "get_margin_data": "risk",
    "get_margin_ranking": "risk",
    "get_north_fund": "fund_flow",
    "get_north_fund_holding": "fund_flow",
    "get_north_fund_top": "fund_flow",
    "get_sector_fund_flow": "fund_flow",
}

_FRESHNESS_BY_CATEGORY = {
    "alerts": "runtime_alert_state",
    "backtest": "latest_backtest_or_runtime_state",
    "basic_data": "latest_reference_snapshot",
    "compliance": "latest_compliance_rules_or_runtime_state",
    "data_sync": "latest_sync_or_cache_state",
    "decision": "latest_market_and_context_snapshot",
    "execution": "execution_runtime_state",
    "factor": "latest_factor_research_snapshot",
    "finance": "latest_financial_or_valuation_snapshot",
    "fund_flow": "intraday_or_latest_fund_flow_snapshot",
    "industry_chain": "latest_industry_relationship_snapshot",
    "macro": "latest_macro_snapshot",
    "market": "intraday_or_latest_market_snapshot",
    "news": "latest_news_and_notice_snapshot",
    "options": "intraday_or_latest_options_snapshot",
    "paper_trading": "latest_paper_trading_state",
    "performance": "latest_performance_snapshot",
    "portfolio": "latest_portfolio_snapshot",
    "quant": "latest_quant_snapshot",
    "research": "latest_research_snapshot",
    "risk": "latest_risk_snapshot",
    "screening": "latest_screening_snapshot",
    "search": "runtime_tool_registry",
    "sector": "latest_sector_snapshot",
    "semantic": "latest_semantic_snapshot",
    "sentiment": "latest_sentiment_snapshot",
    "skills": "runtime_skill_registry",
    "strategy": "latest_strategy_snapshot",
    "technical": "latest_technical_snapshot",
    "user": "latest_user_profile_snapshot",
    "vector": "latest_vector_snapshot",
    "watchlist": "latest_watchlist_snapshot",
}


def _search_stocks_tushare_fallback(keyword: str, limit: int) -> list:
    """DB 无数据时用 Tushare Pro 股票列表按名称/代码/行业筛选"""
    pro = data_source.get_tushare_pro()
    if not pro:
        return []
    keyword_stripped = keyword.strip()
    if not keyword_stripped:
        return []
    keyword_lower = keyword_stripped.lower()
    try:
        df = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,industry",
        )
    except Exception:
        return []
    if df is None or df.empty:
        return []
    results = []
    for _, row in df.iterrows():
        ts_code = str(row.get("ts_code", "") or "")
        symbol = str(row.get("symbol", "") or "")
        name = str(row.get("name", "") or "")
        industry = str(row.get("industry", "") or "")
        if (
            keyword_lower in ts_code.lower()
            or keyword_lower in symbol.lower()
            or keyword_lower in name
            or keyword_lower in industry.lower()
        ):
            results.append({
                "code": normalize_code(symbol) if symbol else ts_code,
                "name": name,
                "industry": industry or None,
                "market_cap": None,
            })
            if len(results) >= limit:
                break
    return results


def _iter_registered_tools(mcp):
    """获取已注册的工具列表 - 兼容 FastMCP (多版本)"""
    import inspect

    # FastMCP internal _tool_manager._tools (stable across versions)
    tool_manager = getattr(mcp, '_tool_manager', None)
    if tool_manager:
        tools_dict = getattr(tool_manager, '_tools', None)
        if isinstance(tools_dict, dict):
            return list(tools_dict.items())

    # Fallback: public API mcp.list_tools() — may be sync or async
    list_tools = getattr(mcp, 'list_tools', None)
    if callable(list_tools):
        try:
            result_or_coro = list_tools()
            if inspect.isawaitable(result_or_coro):
                return []
            tools = result_or_coro
            if tools and hasattr(tools, '__iter__'):
                result = []
                for t in tools:
                    name = getattr(t, 'name', None) or str(t)
                    result.append((name, t))
                if result:
                    return result
        except Exception:
            pass

    return []


def _lookup_registered_tool(mcp, tool_name: str):
    target_name = str(tool_name or "").strip()
    if not target_name:
        return None
    for name, tool in _iter_registered_tools(mcp):
        if str(name or "").strip() == target_name:
            return tool
    return None


def _infer_tool_category(name: str, tool) -> str:
    explicit = getattr(tool, "category", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()

    if name in _TOOL_CATEGORY_MAP:
        return _TOOL_CATEGORY_MAP[name]

    fn = getattr(tool, "fn", None)
    module_name = str(getattr(fn, "__module__", "") or "")
    if module_name.startswith("akshare_mcp.tools.managers."):
        manager_name = module_name.rsplit(".", 1)[-1]
        if manager_name in _MANAGER_CATEGORY_MAP:
            return _MANAGER_CATEGORY_MAP[manager_name]

    for prefix, category in _MODULE_CATEGORY_PREFIXES:
        if module_name.startswith(prefix):
            return category

    mapped = _MODULE_CATEGORY_MAP.get(module_name)
    if mapped:
        return mapped

    if name.startswith("get_factor_") or name.startswith("validate_factor_") or name.startswith("backtest_factor"):
        return "factor"

    return "general"


def _normalize_schema(parameters: Any) -> dict:
    if not isinstance(parameters, dict):
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
    schema = deepcopy(parameters)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("additionalProperties", True)
    return schema


def _tool_description(tool) -> str | None:
    desc = getattr(tool, "description", None)
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    fn = getattr(tool, "fn", None)
    doc = getattr(fn, "__doc__", None)
    if isinstance(doc, str) and doc.strip():
        return doc.strip()
    return None


def _infer_runtime_side_effect_level(name: str, category: str, description: str | None) -> str:
    lowered_name = str(name or "").strip().lower()
    lowered_desc = str(description or "").strip().lower()
    if lowered_name in {"generate_trade_plan"}:
        return "read_only"

    if category == "execution":
        return "trade_risk"

    trade_risk_tokens = ("submit", "cancel", "execute", "place_order", "trade")
    if any(token in lowered_name for token in trade_risk_tokens) or any(
        token in lowered_desc for token in trade_risk_tokens
    ):
        return "trade_risk"

    stateful_prefixes = ("create_", "update_", "delete_", "clear_", "sync_", "log_", "generate_")
    stateful_tokens = ("persist", "artifact", "write", "snapshot", "warmup", "schedule")
    if lowered_name.startswith(stateful_prefixes) or any(token in lowered_desc for token in stateful_tokens):
        return "stateful"

    return "read_only"


def _build_runtime_tool_contract(name: str, tool) -> dict[str, Any] | None:
    if tool is None:
        return None

    schema = _normalize_schema(getattr(tool, "parameters", None))
    required_params = [str(item) for item in schema.get("required", []) if str(item or "").strip()]
    category = _infer_tool_category(str(name), tool)
    description = _tool_description(tool) or f"Runtime-inferred contract for {name}."
    side_effect_level = _infer_runtime_side_effect_level(str(name), category, description)
    title = getattr(tool, "title", None) or str(name).replace("_", " ").title()

    return {
        "name": str(name),
        "title": title,
        "category": category,
        "description": description,
        "required_params": required_params,
        "input_schema": schema,
        "output_schema": deepcopy(STANDARD_ENVELOPE_OUTPUT_SCHEMA),
        "side_effect": {
            "level": side_effect_level,
            "confirmation_required": side_effect_level == "trade_risk",
        },
        "freshness": {
            "expectation": _FRESHNESS_BY_CATEGORY.get(category, "runtime_registered_tool"),
        },
        "examples": [],
        "tags": [category, "runtime-inferred"],
        "contract_version": "ai_tool_contract_v1",
        "inferred_from_runtime": True,
    }


def _resolve_tool_contract(name: str, tool=None) -> dict[str, Any] | None:
    contract = get_catalog_tool_contract(name)
    if contract is not None:
        return contract
    return _build_runtime_tool_contract(name, tool)


def _category_description(category: str) -> str:
    return _CATEGORY_DESCRIPTIONS.get(category, f"{category} 工具")


def _is_hidden_tool(name: str, tool) -> bool:
    return False


def register(mcp):
    """注册搜索工具"""

    def _read_only_meta(
        *,
        status: str,
        target: str,
        degraded: bool = False,
        extra_quality: dict | None = None,
    ) -> dict:
        quality = {"status": status}
        if isinstance(extra_quality, dict):
            quality.update(extra_quality)
        return {
            "quality": quality,
            "side_effect": {
                "level": "read_only",
                "target": target,
                "confirmation_required": False,
                "idempotent": True,
            },
            "degraded": degraded,
        }
    
    @mcp.tool()
    async def search_stocks(
        keyword: str,
        limit: int = 20
    ):
        """
        搜索股票
        
        Args:
            keyword: 关键词（代码或名称，支持中文）
            limit: 返回数量
        """
        started_at = time.perf_counter()
        source_chain = ["search.search_stocks", "db.search_stocks"]
        try:
            db = get_db()
            results = []
            if hasattr(db, "search_stocks"):
                results = await db.search_stocks(keyword, limit=limit)
            
            fallback_used = False
            if not results:
                results = _search_stocks_tushare_fallback(keyword, limit)
                fallback_used = bool(results)
                if fallback_used:
                    source_chain.append("tushare_pro.stock_basic")

            # P2-4.5.10 fix: 中文 stock_name LIKE 兜底(诊断报告 §4.5.10)
            # P3-B8 fix: 修正列名 + 去空格规范化(对话式复测发现)
            # 历史问题:
            #   1. SELECT code FROM stocks 报 OperationalError(实际列名 stock_code)
            #   2. db 中 stock_name 含全/半角空格(如 "五 粮 液"),用户查 "五粮液" 0 命中
            # 修复:
            #   1. 列名 code -> stock_code,SELECT 加 alias 保持下游兼容
            #   2. SQL 端用 REPLACE 去空格,query 同步去空格
            #   3. 异常详细日志而非 silent pass
            if not results and keyword and keyword.strip():
                try:
                    import re as _re_search
                    keyword_clean = keyword.strip()
                    # 去除中文/英文/全角空格,匹配 db 中含空格的存储
                    keyword_no_space = _re_search.sub(r"[\s\u3000]+", "", keyword_clean)
                    async with db.acquire() as conn:
                        like_rows = await conn.fetch(
                            """SELECT stock_code AS code,
                                      stock_name,
                                      industry,
                                      market_cap,
                                      pe_ratio,
                                      pb_ratio
                               FROM stocks
                               WHERE REPLACE(REPLACE(stock_name, ' ', ''), '　', '') LIKE $1
                                  OR stock_name LIKE $2
                                  OR stock_code LIKE $3
                                  OR industry LIKE $4
                               LIMIT $5""",
                            f"%{keyword_no_space}%",
                            f"%{keyword_clean}%",
                            f"%{keyword_clean}%",
                            f"%{keyword_clean}%",
                            int(limit),
                        )
                    if like_rows:
                        results = [dict(r) for r in like_rows]
                        fallback_used = True
                        source_chain.append("db.stocks.like_search_normalized")
                except Exception as exc_search:
                    # P3-B8: 让失败可观测,而非吞掉
                    try:
                        from ..utils import safe_stderr_print
                        safe_stderr_print(f"[search_stocks] db like fallback failed: {type(exc_search).__name__}: {exc_search}")
                    except Exception:
                        pass

            # FIX-11: 行业语义兜底 — 关键词检索仍 0 命中时回退到 semantic_stock_search
            # 历史问题: search_stocks('白酒')=0(仅代码/名称/industry LIKE), 而行业别名
            #   (如 "白酒"->酿酒/食品饮料) 需语义匹配。回退确保行业词不再 0 命中。
            if not results and keyword and keyword.strip():
                try:
                    from . import _vector_search_semantic as _sem_mod
                    sem = await _sem_mod.semantic_stock_search(keyword.strip(), limit=limit)
                    sem_results = (sem or {}).get("data", {}).get("results") or []
                    if sem_results:
                        results = [
                            {
                                "code": r.get("code"),
                                "name": r.get("name"),
                                "industry": r.get("industry"),
                                "market_cap": r.get("market_cap"),
                                "match_type": r.get("match_type"),
                            }
                            for r in sem_results
                        ]
                        fallback_used = True
                        source_chain.append("semantic_stock_search")
                except Exception as exc_sem:
                    try:
                        from ..utils import safe_stderr_print
                        safe_stderr_print(f"[search_stocks] semantic fallback failed: {type(exc_sem).__name__}: {exc_sem}")
                    except Exception:
                        pass
            
            return ok_with_meta(
                {
                    'keyword': keyword,
                    'results': results,
                    'count': len(results),
                },
                tool_name="search_stocks",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_read_only_meta(
                    status="available" if results else "not_found",
                    target=keyword.strip() or "stock_search",
                    degraded=fallback_used,
                    extra_quality={
                        "result_count": len(results),
                        "fallback_used": fallback_used,
                    },
                ),
            )
        
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="search_stocks",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_read_only_meta(
                    status="failed",
                    target=keyword.strip() or "stock_search",
                    degraded=True,
                ),
            )
    
    @mcp.tool()
    def available_tools(category: str | None = None, include_contracts: bool = True):
        """获取所有可用工具列表"""
        started_at = time.perf_counter()
        tools = []
        requested_category = str(category or "").strip().lower()
        contract_count = 0
        for name, tool in _iter_registered_tools(mcp):
            if not name:
                continue
            if _is_hidden_tool(name, tool):
                continue
            # FastMCP Tool 对象有 description 属性
            desc = getattr(tool, "description", None)
            if not desc:
                # 兜底：尝试从原始函数获取
                fn = getattr(tool, "fn", None)
                if fn:
                    desc = getattr(fn, "__doc__", None)
            
            inferred_category = _infer_tool_category(str(name), tool)
            if requested_category and inferred_category != requested_category:
                continue
            row = {
                'name': str(name),
                'category': inferred_category,
                'description': desc.strip() if isinstance(desc, str) else None,
            }
            if include_contracts:
                contract = _resolve_tool_contract(str(name), tool=tool)
                if contract is not None:
                    contract_count += 1
                    row.update(
                        {
                            "title": contract.get("title"),
                            "required_params": contract.get("required_params"),
                            "side_effect": contract.get("side_effect"),
                            "freshness": contract.get("freshness"),
                            "source_policy": contract.get("source_policy"),
                            "examples": contract.get("examples"),
                            "input_schema": contract.get("input_schema"),
                            "output_schema": contract.get("output_schema"),
                            "tags": contract.get("tags"),
                            "contract_version": contract.get("contract_version"),
                            "contract_source": contract.get("contract_source"),
                            "standard_model": contract.get("standard_model"),
                            "provider_choices": contract.get("provider_choices"),
                            "provider_status": contract.get("provider_status"),
                            "quality_gate": contract.get("quality_gate"),
                            "reconciliation": contract.get("reconciliation"),
                            "form_schema": contract.get("form_schema"),
                        }
                    )
            tools.append(row)
        tools.sort(key=lambda x: x.get('name', ''))

        coverage = 1.0 if not tools else round(contract_count / len(tools), 4)
        return ok_with_meta(
            {'tools': tools, 'count': len(tools), 'category': requested_category or None, 'include_contracts': bool(include_contracts)},
            tool_name="available_tools",
            action="list",
            started_at=started_at,
            source_chain=["search.available_tools", "tool_registry", "tool_catalog"],
            extra_meta=_read_only_meta(
                status="available",
                target=requested_category or "tool_registry",
                extra_quality={
                    "tool_count": len(tools),
                    "contract_count": contract_count,
                    "contract_coverage": coverage,
                    "include_contracts": bool(include_contracts),
                },
            ),
        )

    @mcp.tool()
    def get_tool_contract(tool_name: str):
        """获取单个工具的 AI 调用契约。"""
        started_at = time.perf_counter()
        tool = _lookup_registered_tool(mcp, tool_name)
        contract = _resolve_tool_contract(tool_name, tool=tool)
        if contract is None:
            return fail_with_meta(
                f"tool contract not found: {tool_name}",
                tool_name="get_tool_contract",
                action="get",
                started_at=started_at,
                source_chain=["search.get_tool_contract", "tool_catalog"],
                error_code="NOT_FOUND",
                extra_meta=_read_only_meta(
                    status="not_found",
                    target=tool_name.strip() or "tool_catalog",
                    degraded=True,
                ),
            )
        return ok_with_meta(
            {"tool": tool_name, "contract": contract},
            tool_name="get_tool_contract",
            action="get",
            started_at=started_at,
            source_chain=[
                "search.get_tool_contract",
                "tool_catalog" if not contract.get("inferred_from_runtime") else "tool_registry.runtime_inferred_contract",
            ],
            extra_meta=_read_only_meta(
                status="available",
                target=tool_name.strip() or "tool_catalog",
                degraded=bool(contract.get("inferred_from_runtime")),
                extra_quality={
                    "contract_version": contract.get("contract_version"),
                    "contract_source": "runtime_inferred" if contract.get("inferred_from_runtime") else "catalog",
                },
            ),
        )

    @mcp.tool()
    def get_available_categories():
        """获取工具分类"""
        started_at = time.perf_counter()
        names = sorted({
            _infer_tool_category(str(name), tool)
            for name, tool in _iter_registered_tools(mcp)
            if name and not _is_hidden_tool(name, tool)
        })
        categories = [
            {'name': name, 'description': _category_description(name)}
            for name in names
        ]
        
        return ok_with_meta(
            {'categories': categories},
            tool_name="get_available_categories",
            action="list",
            started_at=started_at,
            source_chain=["search.get_available_categories", "tool_registry"],
            extra_meta=_read_only_meta(
                status="available",
                target="tool_categories",
                extra_quality={"category_count": len(categories)},
            ),
        )
