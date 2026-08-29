"""Skill tools with safe orchestrated execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List

from . import skills_advisory_workflows as _skills_advisory_workflows
from . import skills_market_workflows as _skills_market_workflows
from . import skills_portfolio_workflows as _skills_portfolio_workflows
from . import skills_registry as _skills_registry
from . import skills_quant_workflows as _skills_quant_workflows
from . import skills_support as _skills_support
from . import skills_strategy_workflows as _skills_strategy_workflows
from .skills_registry import (
    _FALLBACK_SKILLS,
    _build_skill_registry_summary as _build_skill_registry_summary_from_registry,
    _enrich_skills as _enrich_skills_from_registry,
    _find_repo_skills_root as _find_repo_skills_root_from_registry,
    _load_skills as _load_skills_from_registry,
    _load_skill_coverage_audit as _load_skill_coverage_audit_from_registry,
    _list_skill_roots as _list_skill_roots_from_registry,
    _skills_source as _skills_source_from_registry,
)
from ..services.stock_deep_analysis import run_stock_deep_analysis

_normalize_params = _skills_support._normalize_params
_skill_meta = _skills_support._skill_meta
_skill_payload = _skills_support._skill_payload
_skill_ok = _skills_support._skill_ok
_skill_fail = _skills_support._skill_fail
_step_result = _skills_support._step_result
_run_step = _skills_support._run_step
_run_step_async = _skills_support._run_step_async
_finalize_skill_result = _skills_support._finalize_skill_result
_unsupported_task_result = _skills_support._unsupported_task_result
_safe_float = _skills_support._safe_float
_safe_int = _skills_support._safe_int
_normalize_codes_input = _skills_support._normalize_codes_input
_normalize_holdings_input = _skills_support._normalize_holdings_input
_default_notice_window = _skills_support._default_notice_window
_normalize_rebalance_threshold = _skills_support._normalize_rebalance_threshold
_static_step = _skills_support._static_step
_response_data_dict = _skills_support._response_data_dict


def _available_skill_handlers() -> Dict[str, Callable[[Dict[str, Any]], Any]]:
    return dict(_SKILL_EXECUTORS)


def _find_repo_skills_root():
    return _find_repo_skills_root_from_registry()


def _list_skill_roots():
    return _list_skill_roots_from_registry()


def _load_skill_coverage_audit():
    return _load_skill_coverage_audit_from_registry()


def _load_skills() -> List[Dict[str, Any]]:
    _skills_registry._list_skill_roots = _list_skill_roots
    return _load_skills_from_registry()


def _enrich_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _enrich_skills_from_registry(skills, available_handlers=_available_skill_handlers())


def _build_skill_registry_summary(skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    _skills_registry._load_skill_coverage_audit = _load_skill_coverage_audit
    return _build_skill_registry_summary_from_registry(skills, available_handlers=_available_skill_handlers())


def _skills_source(skills: List[Dict[str, Any]]) -> str:
    return _skills_source_from_registry(skills)


async def _exec_market(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_market_workflows.exec_market(params)


async def _exec_fund_manager_pro(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_portfolio_workflows.exec_fund_manager_pro(params)


def _exec_asset_allocation(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_portfolio_workflows.exec_asset_allocation(params)


async def _exec_fee_costs(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_portfolio_workflows.exec_fee_costs(params)


async def _exec_factor_mining(params: Dict[str, Any]) -> Dict[str, Any]:
    from .managers.quant_manager import quant_manager as runtime_quant_manager

    return await _skills_quant_workflows.exec_factor_mining(
        params,
        runtime_quant_manager=runtime_quant_manager,
    )


async def _exec_strategy_factory(params: Dict[str, Any]) -> Dict[str, Any]:
    from .managers.strategy_manager import strategy_manager as runtime_strategy_manager

    return await _skills_strategy_workflows.exec_strategy_factory(
        params,
        runtime_strategy_manager=runtime_strategy_manager,
    )


async def _exec_fund_news(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_market_workflows.exec_fund_news(params)


async def _exec_fundamental(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_market_workflows.exec_fundamental(params)


def _exec_investor_protection(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_advisory_workflows.exec_investor_protection(params)


def _exec_ips_discipline(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_advisory_workflows.exec_ips_discipline(params)


async def _exec_macro_options_alerts(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_market_workflows.exec_macro_options_alerts(params)


def _exec_performance_attribution(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_portfolio_workflows.exec_performance_attribution(params)


async def _exec_portfolio(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_portfolio_workflows.exec_portfolio(params)


def _exec_portfolio_manager_core(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_portfolio_workflows.exec_portfolio_manager_core(params)


def _exec_quant(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_quant_workflows.exec_quant(params)


async def _exec_quant_data_engineering(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_quant_workflows.exec_quant_data_engineering(params)


def _exec_quant_methods_foundation(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_quant_workflows.exec_quant_methods_foundation(params)


def _exec_quant_ml_signals(params: Dict[str, Any]) -> Dict[str, Any]:
    return _skills_quant_workflows.exec_quant_ml_signals(params)


async def _exec_quant_research_process(params: Dict[str, Any]) -> Dict[str, Any]:
    return await _skills_quant_workflows.exec_quant_research_process(params)


async def _exec_stock_deep_analysis(params: Dict[str, Any]) -> Dict[str, Any]:
    task = str(params.get("task") or "deep_analysis").strip().lower() or "deep_analysis"
    return await run_stock_deep_analysis(
        code=str(params.get("code") or params.get("stock_code") or params.get("symbol") or ""),
        task=task,
        user_id=str(params.get("_triggered_by_user_id") or params.get("user_id") or "").strip() or None,
        investment_style=str(params.get("investment_style") or params.get("style") or "balanced"),
        market=str(params.get("market") or "cn"),
        run_id=str(params.get("run_id") or "").strip() or None,
    )


async def _exec_trading_decision(params: Dict[str, Any]) -> Dict[str, Any]:
    task = str(params.get("task") or "trade_plan").strip().lower() or "trade_plan"
    if task == "trade_plan":
        deep_payload = await run_stock_deep_analysis(
            code=str(params.get("code") or params.get("stock_code") or params.get("symbol") or ""),
            task="trade_plan",
            user_id=str(params.get("_triggered_by_user_id") or params.get("user_id") or "").strip() or None,
            investment_style=str(params.get("investment_style") or params.get("style") or "balanced"),
            market=str(params.get("market") or "cn"),
            run_id=str(params.get("run_id") or "").strip() or None,
        )
        return deep_payload

    return await run_stock_deep_analysis(
        code=str(params.get("code") or params.get("stock_code") or params.get("symbol") or ""),
        task=task,
        user_id=str(params.get("_triggered_by_user_id") or params.get("user_id") or "").strip() or None,
        investment_style=str(params.get("investment_style") or params.get("style") or "balanced"),
        market=str(params.get("market") or "cn"),
        run_id=str(params.get("run_id") or "").strip() or None,
    )


_SKILL_EXECUTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "akshare-asset-allocation": _exec_asset_allocation,
    "akshare-fee-costs": _exec_fee_costs,
    "akshare-factor-mining": _exec_factor_mining,
    "akshare-fund-news": _exec_fund_news,
    "akshare-fundamental": _exec_fundamental,
    "akshare-investor-protection": _exec_investor_protection,
    "akshare-ips-discipline": _exec_ips_discipline,
    "akshare-macro-options-alerts": _exec_macro_options_alerts,
    "akshare-market": _exec_market,
    "akshare-performance-attribution": _exec_performance_attribution,
    "akshare-portfolio": _exec_portfolio,
    "akshare-portfolio-manager-core": _exec_portfolio_manager_core,
    "akshare-quant": _exec_quant,
    "akshare-quant-data-engineering": _exec_quant_data_engineering,
    "akshare-quant-methods-foundation": _exec_quant_methods_foundation,
    "akshare-quant-ml-signals": _exec_quant_ml_signals,
    "akshare-quant-research-process": _exec_quant_research_process,
    "akshare-stock-deep-analysis": _exec_stock_deep_analysis,
    "akshare-strategy-factory": _exec_strategy_factory,
    "akshare-trading-decision": _exec_trading_decision,
    "akshare-fund-manager-pro": _exec_fund_manager_pro,
}


def register(mcp):
    @mcp.tool()
    def list_skills():
        """列出当前可发现的内置技能及其执行状态。

        Returns:
            dict: 标准技能响应，包含技能列表、数量、来源和注册表摘要。
            P1-3.7: 顶层显式拆分 executable_count / registered_only_count,
            让 AI Agent 看到 "总数 36 中真正能执行的只有 21" 这种关键信号。
        """
        started_at = datetime.now()
        skills = _enrich_skills(_load_skills())
        source = _skills_source(skills)
        registry_summary = _build_skill_registry_summary(skills)
        # P1-3.7 fix: 把 executable_count / registered_only_count 等关键监控字段提到顶层
        return _skill_ok(
            {
                "skills": skills,
                "count": len(skills),
                "executable_count": int(registry_summary.get("executable_count") or 0),
                "registered_only_count": int(registry_summary.get("registered_only_count") or 0),
                "deprecated_count": int(registry_summary.get("deprecated_count") or 0),
                "executor_coverage_ratio": (
                    round(int(registry_summary.get("executable_count") or 0) / max(len(skills), 1), 4)
                    if skills else None
                ),
                "source": source,
                "registry_summary": registry_summary,
            },
            backend_requested="skills_registry",
            backend_used=source,
            fallback_used=source != "skills_registry",
            fallback_reason=None if source == "skills_registry" else "skills_registry_unavailable",
            started_at=started_at,
        )

    @mcp.tool()
    def search_skills(keyword: str):
        """按关键字检索技能元数据。

        Args:
            keyword: 技能 ID、名称、分类或描述关键字；为空时返回全部技能。

        Returns:
            dict: 标准技能响应，包含匹配技能和注册表摘要。
        """
        started_at = datetime.now()
        skills = _enrich_skills(_load_skills())
        source = _skills_source(skills)
        registry_summary = _build_skill_registry_summary(skills)
        keyword_lower = (keyword or "").strip().lower()
        if not keyword_lower:
            return _skill_ok(
                {"skills": skills, "keyword": keyword, "count": len(skills), "source": source, "registry_summary": registry_summary},
                backend_requested="skills_registry",
                backend_used=source,
                fallback_used=source != "skills_registry",
                fallback_reason=None if source == "skills_registry" else "skills_registry_unavailable",
                started_at=started_at,
            )

        matched = [
            skill
            for skill in skills
            if keyword_lower in skill.get("id", "").lower()
            or keyword_lower in skill.get("name", "").lower()
            or keyword_lower in skill.get("category", "").lower()
            or keyword_lower in skill.get("description", "").lower()
        ]
        return _skill_ok(
            {"skills": matched, "keyword": keyword, "count": len(matched), "source": source, "registry_summary": registry_summary},
            backend_requested="skills_registry",
            backend_used=source,
            fallback_used=source != "skills_registry",
            fallback_reason=None if source == "skills_registry" else "skills_registry_unavailable",
            started_at=started_at,
        )

    @mcp.tool()
    async def run_skill(skill_id: str, params: dict = None):
        """执行指定技能的编排处理器。

        Args:
            skill_id: 技能唯一标识。
            params: 传递给技能执行器的参数字典，缺省时会被标准化为空字典。

        Returns:
            dict: 标准技能成功/失败响应，包含执行结果、错误码和回退元信息。
        """
        started_at = datetime.now()
        normalized_params = _normalize_params(params)
        skills = _enrich_skills(_load_skills())
        source = _skills_source(skills)
        skill = next((s for s in skills if s.get("id") == skill_id), None)
        if not skill:
            return _skill_fail(
                f"Skill {skill_id} not found",
                backend_requested="skills_registry",
                backend_used="none",
                fallback_used=True,
                fallback_reason="skill_not_found",
                started_at=started_at,
                error_code="SKILL_NOT_FOUND",
                detail={"skill_id": skill_id},
            )

        available_handlers = _available_skill_handlers()
        executor = available_handlers.get(skill_id)
        if skill.get("status") == "deprecated":
            fallback_reasons = []
            if source != "codex_registry":
                fallback_reasons.append("skills_registry_unavailable")
            fallback_reasons.append("skill_deprecated")
            return _skill_fail(
                f"Skill {skill_id} is deprecated and cannot be executed",
                backend_requested="skill_executor",
                backend_used="registry_only",
                fallback_used=True,
                fallback_reason=fallback_reasons,
                started_at=started_at,
                error_code="SKILL_DEPRECATED",
                detail={"skill": skill},
            )

        if executor is None or not skill.get("executable"):
            fallback_reasons = []
            if source != "codex_registry":
                fallback_reasons.append("skills_registry_unavailable")
            fallback_reasons.append("handler_not_implemented")
            return _skill_fail(
                f"Skill {skill_id} is registered but not executable",
                backend_requested="skill_executor",
                backend_used="registry_only",
                fallback_used=True,
                fallback_reason=fallback_reasons,
                started_at=started_at,
                error_code="SKILL_NOT_EXECUTABLE",
                detail={
                    "skill": skill,
                    "available_handlers": sorted(available_handlers.keys()),
                },
            )

        try:
            import inspect
            if inspect.iscoroutinefunction(executor):
                execution = await executor(normalized_params)
            else:
                execution = executor(normalized_params)
        except Exception as e:
            fallback_reasons = [f"executor_exception:{type(e).__name__}"]
            if source != "codex_registry":
                fallback_reasons.insert(0, "skills_registry_unavailable")
            return _skill_fail(
                f"Skill {skill_id} execution failed: {type(e).__name__}: {e}",
                backend_requested="skill_executor",
                backend_used="none",
                fallback_used=True,
                fallback_reason=fallback_reasons,
                started_at=started_at,
                error_code="SKILL_EXECUTION_FAILED",
                detail={"skill": skill},
            )

        return _skill_ok(
            {
                "skill": skill,
                "skill_id": skill_id,
                "skill_name": skill.get("name", ""),
                "params": normalized_params,
                "skill_path": skill.get("path", ""),
                "execution_mode": skill.get("execution_mode", "orchestrated"),
                # N45 payload 去重: 历史上 execution 与 result 是同一对象的两份完整副本。
                # 现仅保留 result 为权威键，execution 改为指针说明，避免 payload 翻倍。
                "result": execution,
                "execution_ref": "result",
                "message": "Skill executed via built-in orchestrator",
                "source": source,
            },
            backend_requested="skill_executor",
            backend_used="built_in_orchestrator",
            fallback_used=source != "codex_registry",
            fallback_reason=None if source == "codex_registry" else ["skills_registry_unavailable"],
            started_at=started_at,
        )
