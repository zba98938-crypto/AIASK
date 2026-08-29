"""Quant manager: factor analysis and workflow orchestration.

Two distinct IC analysis paths coexist — do NOT confuse them:

1. **Classic factor IC** (``factor_ic`` / ``batch_compute_factors``):
   Operates on SUPPORTED_FACTORS (predefined names like momentum, value, quality).
   ``run_factor_ic_analysis`` computes dual IC (Pearson + Rank IC) with bootstrap CI
   on a single stock's time-series.  ``batch_compute_factors`` computes cross-sectional
   IC / Rank IC across the universe and persists results.

2. **LLM candidate validation** (``validate_factor_candidate``):
   Operates on DSL-compiled candidate expressions produced by ``llm_factor_mining``.
   ``validate_factor_candidate_pipeline`` returns OOS validation, robustness, turnover,
   cost-capacity, and a composite rating.

The two paths differ in input granularity (pre-defined name vs DSL expression),
universe scope, and output schema.  Consumers must pick the right path.
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

from ...services import (
    get_artifact_async,
    get_factor_llm_provider,
    get_factor_research_memory_service,
    register_artifact,
    register_artifact_async,
    validate_factor_candidate_pipeline,
)
from ...services.factor_candidate_compiler import compile_factor_candidate
from ...services.factor_prompt_builder import build_factor_mining_prompt
from ...services.llm_alpha import LLMAlphaMiner
from ...storage import get_db
from ...utils import fail, ok
from ..manager_protocol import build_manager_meta, ok_with_meta, fail_with_meta
from ..quant import run_factor_oos_validation

# Re-exported helpers from sub-modules
from .quant_mgr_helpers import (
    _as_code_list,
    _clip,
    _sort_klines_ascending,
)
from .quant_mgr_automl import (
    _build_automl_dataset,
    _fit_automl_model,
    _select_anchor_factor,
)
from .quant_mgr_artifacts import (
    handle_champion_challenger,
    handle_factor_candidate_registry,
    handle_factor_ic_history,
    handle_factor_research_memory,
    handle_feature_store,
    handle_model_registry,
    handle_replay_experiment,
    handle_replay_factor_episode,
    handle_scheduler_run_now,
    handle_scheduler_status,
)
from .quant_mgr_automl_actions import handle_automl_discovery
from .quant_mgr_classic import (
    handle_alternative_factors,
    handle_backtest_factor,
    handle_batch_compute_factors,
    handle_calculate_factors,
    handle_factor_ic,
    handle_multi_factor_score,
)
from .quant_mgr_generation import handle_llm_factor_mining
from .quant_mgr_validation import handle_validate_factor_candidate
from .data_sync_manager import run_runtime_data_warmup

_QUANT_MANAGER_IMPL = None
_MARKET_CODE_PATTERN = re.compile(r"^\d{6}$")


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_bool(name: str, default: bool) -> bool:
    return _coerce_bool(os.getenv(name), default)


def _filter_market_codes(values) -> list[str]:
    items = []
    seen: set[str] = set()
    for code in _as_code_list(values):
        token = str(code or "").strip().split(".", 1)[0].strip()
        if not token or token in seen or not _MARKET_CODE_PATTERN.fullmatch(token):
            continue
        seen.add(token)
        items.append(token)
    return items


def register_quant_manager(mcp):
    """Register quant manager tool."""

    @mcp.tool()
    async def quant_manager(
        action: str,
        code: Optional[str] = None,
        kwargs: Any = None,
        params: Any = None,
    ) -> dict:
        """Quant manager with unified action + kwargs protocol."""
        try:
            start_time = time.perf_counter()
            tool_version = "v1.2"
            db = get_db()

            tool_kwargs = {}
            if kwargs is not None:
                tool_kwargs["kwargs"] = kwargs
            if params is not None:
                tool_kwargs["params"] = params

            if isinstance(tool_kwargs.get("params"), dict):
                tool_kwargs = {**tool_kwargs, **tool_kwargs.get("params")}

            if tool_kwargs.get("params") and isinstance(tool_kwargs.get("params"), str):
                try:
                    extra = json.loads(tool_kwargs.get("params") or "{}")
                    if isinstance(extra, dict):
                        tool_kwargs = {**tool_kwargs, **extra}
                except Exception:
                    pass

            if tool_kwargs.get("kwargs") and isinstance(tool_kwargs.get("kwargs"), str):
                try:
                    extra = json.loads(tool_kwargs.get("kwargs") or "{}")
                    if isinstance(extra, dict):
                        tool_kwargs = {**tool_kwargs, **extra}
                except Exception:
                    pass

            if isinstance(tool_kwargs.get("kwargs"), dict):
                tool_kwargs = {**tool_kwargs, **tool_kwargs.get("kwargs")}

            _kw = tool_kwargs

            as_of = _kw.get("as_of", "")
            adjust = _kw.get("adjust", "")
            price_source_policy = _kw.get("price_source_policy", "auto")
            explain = _kw.get("explain", True)
            strict_mode = _kw.get("strict_mode", False)
            code = code or _kw.get("code") or _kw.get("Code") or _kw.get("stock_code") or _kw.get("symbol")

            # P0-4b / P1-1b: dry_run and as_of propagation
            _dry_run = _coerce_bool(_kw.get("dry_run"), False)
            if _dry_run:
                _kw = dict(_kw)
                _kw["_dry_run"] = True
                _kw["_requested_persist_artifact"] = _kw.get("persist_artifact")
                _kw["_requested_write_memory"] = _kw.get("write_memory")
                _kw["persist_artifact"] = False
                _kw["write_memory"] = False
            if as_of and not _kw.get("as_of"):
                _kw = dict(_kw) if not _dry_run else _kw
                _kw["as_of"] = as_of

            def _with_meta(resp: dict, source_chain=None, data_timestamp: Optional[str] = None):
                if not isinstance(resp, dict):
                    return resp
                resp["meta"] = build_manager_meta(
                    tool_name="quant_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain or ["quant_manager"],
                    data_timestamp=data_timestamp,
                    tool_version=tool_version,
                    extra={
                        "as_of": as_of,
                        "adjust": adjust,
                        "price_source_policy": price_source_policy,
                        "explain": explain,
                        "strict_mode": strict_mode,
                        "dry_run": _dry_run,
                        "pit": {"as_of": as_of or None, "pit_passed": bool(as_of)},
                    },
                )
                return resp

            def _ok(data: dict, source_chain=None, data_timestamp: Optional[str] = None):
                return _with_meta(ok(data), source_chain, data_timestamp)

            def _fail(message: str, source_chain=None, data_timestamp: Optional[str] = None):
                return _with_meta(fail(message), source_chain, data_timestamp)

            supported_actions = {
                "calculate_factors": "计算因子（需要 code）",
                "alternative_factors": "P2 另类数据因子化（新闻/公告/研报/资金流）",
                "factor_ic": "因子 IC 分析（需要 codes, factor）",
                "backtest_factor": "因子分组回测（需要 codes, factor）",
                "multi_factor_score": "多因子评分（需要 code）",
                "llm_factor_mining": "P0 LLM 因子候选生成（真实模型调用 + schema 校验 + fallback）",
                "validate_factor_candidate": "P1 候选因子验证（DSL 编译 + 横截面 IC 检验，可直接吃 artifact）",
                "factor_research_memory": "P2 研究记忆查询（list|get|recall|stats）",
                "factor_candidate_registry": "P2 治理后候选池注册表（list|get|summary|active_pool）",
                "champion_challenger": "P2 champion/challenger 评审与注册表写入",
                "model_registry": "P2 模型注册表、生命周期扫描与重训练治理",
                "replay_factor_episode": "P2 因子研究 episode 回放/查询（run|get|list|summary）",
                "automl_discovery": "P2 AutoML 因子发现（特征筛选+集成+OOS锚点验证）",
                "feature_store": "P2 特征快照/实验追踪（snapshot|get|list）",
                "replay_experiment": "P2 结果回放（基于 artifact_id 复跑并输出漂移）",
                "batch_compute_factors": "Compute and optionally persist supported factor values for a code universe.",
                "factor_ic_history": "Query persisted factor IC history.",
                "scheduler_status": "Inspect factor scheduler status and any run-now background job.",
                "scheduler_run_now": "Trigger one factor scheduler run; defaults to async job return.",
                "factory_pool_status": "Inspect factor mining factory pool status.",
                "factory_mining_cycle": "Run factor mining factory cycle.",
                "factory_pool_factors": "List active factor mining factory factors.",
                "factory_maintenance": "Run factor mining factory maintenance.",
                "help": "显示帮助信息",
            }

            async def _handle_help():
                return _ok({"supported_actions": supported_actions})

            async def _handle_llm_factor_mining():
                return await handle_llm_factor_mining(
                    kw=_kw,
                    code=code,
                    db=db,
                    ok=_ok,
                    fail=_fail,
                    get_factor_llm_provider_fn=get_factor_llm_provider,
                    memory_service_factory=get_factor_research_memory_service,
                    build_factor_mining_prompt_fn=build_factor_mining_prompt,
                    run_runtime_data_warmup_fn=run_runtime_data_warmup,
                    register_artifact_async_fn=register_artifact_async,
                    missing_kline_fn=lambda *args, **kwargs: [],
                    compile_factor_candidate_fn=compile_factor_candidate,
                    sort_klines_ascending_fn=_sort_klines_ascending,
                    coerce_bool_fn=_coerce_bool,
                    env_bool_fn=_env_bool,
                    llm_alpha_miner_factory=LLMAlphaMiner,
                )

            async def _handle_validate_factor_candidate():
                return await handle_validate_factor_candidate(
                    kw=_kw,
                    code=code,
                    db=db,
                    ok=_ok,
                    fail=_fail,
                    get_artifact_async_fn=get_artifact_async,
                    validate_factor_candidate_pipeline_fn=validate_factor_candidate_pipeline,
                    register_artifact_async_fn=register_artifact_async,
                    memory_service_factory=get_factor_research_memory_service,
                )

            async def _handle_factor_research_memory():
                return await handle_factor_research_memory(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    memory_service_factory=get_factor_research_memory_service,
                )

            async def _handle_factor_candidate_registry():
                return await handle_factor_candidate_registry(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    filter_market_codes=_filter_market_codes,
                )

            async def _handle_champion_challenger():
                return await handle_champion_challenger(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    filter_market_codes=_filter_market_codes,
                )

            async def _handle_model_registry():
                return await handle_model_registry(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    db=db,
                    quant_manager_call=quant_manager,
                    filter_market_codes=_filter_market_codes,
                )

            async def _handle_replay_factor_episode():
                return await handle_replay_factor_episode(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    quant_manager_call=quant_manager,
                )

            async def _handle_calculate_factors():
                return await handle_calculate_factors(
                    kw=_kw,
                    code=code,
                    db=db,
                    ok=_ok,
                    fail=_fail,
                )

            async def _handle_factor_ic():
                return await handle_factor_ic(
                    kw=_kw,
                    fail=_fail,
                    with_meta=_with_meta,
                )

            async def _handle_backtest_factor():
                return await handle_backtest_factor(
                    kw=_kw,
                    fail=_fail,
                    with_meta=_with_meta,
                )

            async def _handle_multi_factor_score():
                return await handle_multi_factor_score(
                    kw=_kw,
                    code=code,
                    ok=_ok,
                    fail=_fail,
                    quant_manager_call=quant_manager,
                )

            async def _handle_alternative_factors():
                return await handle_alternative_factors(
                    kw=_kw,
                    code=code,
                    db=db,
                    ok=_ok,
                    fail=_fail,
                )

            async def _handle_automl_discovery():
                return await handle_automl_discovery(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    build_automl_dataset_fn=_build_automl_dataset,
                    fit_automl_model_fn=_fit_automl_model,
                    select_anchor_factor_fn=_select_anchor_factor,
                    run_factor_oos_validation_fn=run_factor_oos_validation,
                    register_artifact_fn=register_artifact,
                    clip_fn=_clip,
                    db=db,
                )

            async def _handle_feature_store():
                return await handle_feature_store(
                    kw=_kw,
                    code=code,
                    ok=_ok,
                    fail=_fail,
                    quant_manager_call=quant_manager,
                )

            async def _handle_replay_experiment():
                return await handle_replay_experiment(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    quant_manager_call=quant_manager,
                )

            async def _handle_batch_compute_factors():
                return await handle_batch_compute_factors(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    get_db_fn=get_db,
                )

            async def _handle_factor_ic_history():
                return await handle_factor_ic_history(
                    kw=_kw,
                    ok=_ok,
                    fail=_fail,
                    get_db_fn=get_db,
                )

            async def _handle_scheduler_status():
                return await handle_scheduler_status(ok=_ok)

            async def _handle_scheduler_run_now():
                return await handle_scheduler_run_now(kw=_kw, ok=_ok)

            async def _handle_factory_pool_status():
                from ...services.factor_mining_factory.api import get_factor_pool_gateway
                gw = get_factor_pool_gateway()
                return _ok(await gw.get_pool_status())

            async def _handle_factory_mining_cycle():
                from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
                from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

                ensure_default_runtime_services()
                factory = get_factor_mining_runtime()
                engines = _kw.get("engines")
                codes = _kw.get("codes")
                candidate_count = int(_kw.get("candidate_count", 30) or 30)
                result = await factory.run_once(
                    trigger="quant_manager",
                    engines=engines,
                    candidate_count=candidate_count,
                    codes=codes,
                )
                return _ok(result) if result.get("success") else _fail(result.get("error", "mining cycle failed"))

            async def _handle_factory_pool_factors():
                from ...services.factor_mining_factory.api import get_factor_pool_gateway
                gw = get_factor_pool_gateway()
                families = _kw.get("families")
                limit = int(_kw.get("limit", 50) or 50)
                factors = await gw.get_active_factors(families=families, limit=limit)
                return _ok({"factors": factors, "count": len(factors)})

            async def _handle_factory_maintenance():
                from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
                from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

                ensure_default_runtime_services()
                factory = get_factor_mining_runtime()
                result = await factory.run_maintenance()
                return _ok(result)

            action_handlers = {
                "help": _handle_help,
                "llm_factor_mining": _handle_llm_factor_mining,
                "validate_factor_candidate": _handle_validate_factor_candidate,
                "factor_research_memory": _handle_factor_research_memory,
                "factor_candidate_registry": _handle_factor_candidate_registry,
                "champion_challenger": _handle_champion_challenger,
                "model_registry": _handle_model_registry,
                "replay_factor_episode": _handle_replay_factor_episode,
                "calculate_factors": _handle_calculate_factors,
                "factor_ic": _handle_factor_ic,
                "backtest_factor": _handle_backtest_factor,
                "multi_factor_score": _handle_multi_factor_score,
                "alternative_factors": _handle_alternative_factors,
                "automl_discovery": _handle_automl_discovery,
                "feature_store": _handle_feature_store,
                "replay_experiment": _handle_replay_experiment,
                "batch_compute_factors": _handle_batch_compute_factors,
                "factor_ic_history": _handle_factor_ic_history,
                "scheduler_status": _handle_scheduler_status,
                "scheduler_run_now": _handle_scheduler_run_now,
                "factory_pool_status": _handle_factory_pool_status,
                "factory_mining_cycle": _handle_factory_mining_cycle,
                "factory_pool_factors": _handle_factory_pool_factors,
                "factory_maintenance": _handle_factory_maintenance,
            }

            handler = action_handlers.get(action)
            if handler is not None:
                return await handler()

            return _fail(
                "Unknown action: {action}. Supported: help, calculate_factors, alternative_factors, "
                "factor_ic, backtest_factor, multi_factor_score, llm_factor_mining, validate_factor_candidate, factor_research_memory, factor_candidate_registry, champion_challenger, model_registry, replay_factor_episode, automl_discovery, feature_store, "
                "replay_experiment, batch_compute_factors, factor_ic_history, scheduler_status, scheduler_run_now, "
                "factory_pool_status, factory_mining_cycle, factory_pool_factors, factory_maintenance"
                .format(action=action)
            )
        except Exception as e:
            return _fail(str(e))


class _QuantManagerProbeMCP:
    """Minimal MCP stub used to expose the registered quant_manager as a module-level callable."""

    def __init__(self):
        self.fn = None

    def tool(self, **kwargs):
        def _decorator(fn):
            self.fn = fn
            return fn

        return _decorator


def _get_quant_manager_impl():
    global _QUANT_MANAGER_IMPL
    if _QUANT_MANAGER_IMPL is None:
        probe = _QuantManagerProbeMCP()
        register_quant_manager(probe)
        _QUANT_MANAGER_IMPL = probe.fn
    return _QUANT_MANAGER_IMPL


async def quant_manager(
    action: str,
    code: Optional[str] = None,
    kwargs: Any = None,
    params: Any = None,
    **extra_kwargs,
):
    """Module-level wrapper so internal services can import and call quant_manager directly."""

    impl = _get_quant_manager_impl()
    if impl is None:
        raise RuntimeError("quant_manager implementation is unavailable")
    merged_params = dict(params) if isinstance(params, dict) else {}
    if extra_kwargs:
        merged_params = {**extra_kwargs, **merged_params}
    resolved_params = merged_params if merged_params else params
    return await impl(action=action, code=code, kwargs=kwargs, params=resolved_params)
