"""服务层"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)
_shared_runtime_clients_closing = False


def _unavailable(name: str, exc: BaseException) -> Callable[..., Any]:
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"{name} unavailable: {exc}") from exc

    return _raise


async def _noop_close() -> None:
    return None


try:
    from .technical_analysis import technical_analysis
except Exception as exc:
    technical_analysis = _unavailable("technical_analysis", exc)
try:
    from .backtest.engine import backtest_engine
except Exception as exc:
    backtest_engine = _unavailable("backtest_engine", exc)
try:
    from .pattern_recognition import pattern_recognition
except Exception as exc:
    pattern_recognition = _unavailable("pattern_recognition", exc)
try:
    from .cost_model import build_cost_model, effective_cost_rate, resolve_cost_assumptions
except Exception as exc:
    build_cost_model = _unavailable("build_cost_model", exc)
    effective_cost_rate = _unavailable("effective_cost_rate", exc)
    resolve_cost_assumptions = _unavailable("resolve_cost_assumptions", exc)
try:
    from .artifact_registry import (
        register_artifact,
        register_artifact_async,
        get_artifact,
        list_artifacts,
        get_artifact_async,
        list_artifacts_async,
    )
except Exception as exc:
    register_artifact = _unavailable("register_artifact", exc)
    register_artifact_async = _unavailable("register_artifact_async", exc)
    get_artifact = _unavailable("get_artifact", exc)
    list_artifacts = _unavailable("list_artifacts", exc)
    get_artifact_async = _unavailable("get_artifact_async", exc)
    list_artifacts_async = _unavailable("list_artifacts_async", exc)
try:
    from .factor_candidate_storage import (
        FACTOR_MEMORY_STRATEGY,
        FACTOR_MEMORY_VERSION,
        get_factor_candidate_record,
        get_factor_candidate_record_async,
        list_factor_candidate_records_async,
        save_factor_candidate_record,
    )
except Exception as exc:
    FACTOR_MEMORY_STRATEGY = "factor_candidate_memory"
    FACTOR_MEMORY_VERSION = "unavailable"
    get_factor_candidate_record = _unavailable("get_factor_candidate_record", exc)
    get_factor_candidate_record_async = _unavailable("get_factor_candidate_record_async", exc)
    list_factor_candidate_records_async = _unavailable("list_factor_candidate_records_async", exc)
    save_factor_candidate_record = _unavailable("save_factor_candidate_record", exc)
try:
    from .signal_dsl import build_signal_definition, evaluate_signal
except Exception as exc:
    build_signal_definition = _unavailable("build_signal_definition", exc)
    evaluate_signal = _unavailable("evaluate_signal", exc)
try:
    from .factor_llm_provider import (
        FactorLLMConfig,
        FactorLLMProvider,
        FactorLLMRequestError,
        close_factor_llm_provider,
        get_factor_llm_provider,
        get_factor_candidate_schema_path,
        load_factor_candidate_schema,
        validate_factor_generation_payload,
    )
except Exception as exc:
    _factor_llm_provider_error = exc

    class FactorLLMRequestError(RuntimeError):
        pass

    class FactorLLMConfig:  # pragma: no cover - import fallback
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"FactorLLMConfig unavailable: {_factor_llm_provider_error}") from _factor_llm_provider_error

    class FactorLLMProvider:  # pragma: no cover - import fallback
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"FactorLLMProvider unavailable: {_factor_llm_provider_error}") from _factor_llm_provider_error

    close_factor_llm_provider = _noop_close
    get_factor_llm_provider = _unavailable("get_factor_llm_provider", exc)
    get_factor_candidate_schema_path = _unavailable("get_factor_candidate_schema_path", exc)
    load_factor_candidate_schema = _unavailable("load_factor_candidate_schema", exc)
    validate_factor_generation_payload = _unavailable("validate_factor_generation_payload", exc)
try:
    from .factor_candidate_compiler import (
        SUPPORTED_FACTOR_FIELDS,
        SUPPORTED_FACTOR_FUNCTIONS,
        build_factor_feature_frame,
        compile_factor_candidate,
        evaluate_compiled_factor,
    )
except Exception as exc:
    SUPPORTED_FACTOR_FIELDS = set()
    SUPPORTED_FACTOR_FUNCTIONS = set()
    build_factor_feature_frame = _unavailable("build_factor_feature_frame", exc)
    compile_factor_candidate = _unavailable("compile_factor_candidate", exc)
    evaluate_compiled_factor = _unavailable("evaluate_compiled_factor", exc)
try:
    from .factor_validation_pipeline import validate_factor_candidate_pipeline
except Exception as exc:
    validate_factor_candidate_pipeline = _unavailable("validate_factor_candidate_pipeline", exc)
try:
    from .factor_validation_bootstrap import run_factor_validation_bootstrap
except Exception as exc:
    run_factor_validation_bootstrap = _unavailable("run_factor_validation_bootstrap", exc)
try:
    from .factor_research_memory import (
        FactorResearchMemoryService,
        get_factor_research_memory_service,
    )
except Exception as exc:
    _factor_research_memory_error = exc

    class FactorResearchMemoryService:  # pragma: no cover - import fallback
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"FactorResearchMemoryService unavailable: {_factor_research_memory_error}"
            ) from _factor_research_memory_error

    get_factor_research_memory_service = _unavailable("get_factor_research_memory_service", exc)
try:
    from .factor_external_research import (
        FACTOR_EXTERNAL_RESEARCH_STRATEGY,
        FACTOR_EXTERNAL_RESEARCH_VERSION,
        collect_external_factor_research,
        ingest_external_factor_research,
    )
except Exception as exc:
    FACTOR_EXTERNAL_RESEARCH_STRATEGY = "factor_external_research_evidence"
    FACTOR_EXTERNAL_RESEARCH_VERSION = "unavailable"
    collect_external_factor_research = _unavailable("collect_external_factor_research", exc)
    ingest_external_factor_research = _unavailable("ingest_external_factor_research", exc)
try:
    from .strategy_llm_provider import close_strategy_llm_provider
except Exception:
    close_strategy_llm_provider = _noop_close

try:
    from .text_embedding import close_strategy_text_embedding_service
except Exception:
    close_strategy_text_embedding_service = _noop_close
try:
    from .validation import (
        bootstrap_ic_ci,
        deflated_sharpe_ratio,
        hansen_spa_test,
        probability_of_backtest_overfitting,
        white_reality_check,
    )
except Exception as exc:
    bootstrap_ic_ci = _unavailable("bootstrap_ic_ci", exc)
    deflated_sharpe_ratio = _unavailable("deflated_sharpe_ratio", exc)
    hansen_spa_test = _unavailable("hansen_spa_test", exc)
    probability_of_backtest_overfitting = _unavailable("probability_of_backtest_overfitting", exc)
    white_reality_check = _unavailable("white_reality_check", exc)
try:
    from .evidence_chain import (
        create_chain, add_evidence, make_evidence, set_conclusion,
        save_chain, get_chain, query_chains_by_code, query_chains_by_date,
        list_chains, summarize_chain,
    )
except Exception as exc:
    create_chain = _unavailable("create_chain", exc)
    add_evidence = _unavailable("add_evidence", exc)
    make_evidence = _unavailable("make_evidence", exc)
    set_conclusion = _unavailable("set_conclusion", exc)
    save_chain = _unavailable("save_chain", exc)
    get_chain = _unavailable("get_chain", exc)
    query_chains_by_code = _unavailable("query_chains_by_code", exc)
    query_chains_by_date = _unavailable("query_chains_by_date", exc)
    list_chains = _unavailable("list_chains", exc)
    summarize_chain = _unavailable("summarize_chain", exc)
try:
    from .background_tasks import drain_background_tasks
except Exception:
    drain_background_tasks = _noop_close


async def _best_effort_close(name: str, callback: Callable[[], Awaitable[Any] | Any]) -> None:
    try:
        result = callback()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.warning("[services] close %s failed: %s", name, exc)


def _close_market_runtime_resources() -> None:
    try:
        from ..data_source.quotes import shutdown_efinance_executor

        shutdown_efinance_executor()
    except Exception as exc:
        logger.warning("[services] close efinance executor failed: %s", exc)
    try:
        from ..tools.market.helpers import shutdown_spot_executor

        shutdown_spot_executor()
    except Exception as exc:
        logger.warning("[services] close market spot executor failed: %s", exc)
    try:
        from ..tools.managers._execution_manager_support import shutdown_realtime_quote_executor

        shutdown_realtime_quote_executor()
    except Exception as exc:
        logger.warning("[services] close execution realtime executor failed: %s", exc)
    try:
        from ..data_source.tdx_local import get_tdx_local_source

        get_tdx_local_source().reset_hq()
    except Exception as exc:
        logger.warning("[services] close TDX HQ connection failed: %s", exc)
    try:
        from ..data_source.tdx_tqcenter import reset_tq

        reset_tq()
    except Exception as exc:
        logger.warning("[services] reset TQCenter runtime failed: %s", exc)


async def close_shared_runtime_clients() -> None:
    """关闭进程级共享资源，供一次性脚本/退出钩子显式调用。"""
    global _shared_runtime_clients_closing
    if _shared_runtime_clients_closing:
        return
    _shared_runtime_clients_closing = True
    try:
        await _best_effort_close("factor LLM provider", close_factor_llm_provider)
        await _best_effort_close("strategy LLM provider", close_strategy_llm_provider)
        await _best_effort_close("strategy text embedding service", close_strategy_text_embedding_service)
        _close_market_runtime_resources()
        await _best_effort_close("background tasks", drain_background_tasks)
        try:
            from ..storage import close_db, drain_cleanup_callbacks
        except Exception:
            close_db = None
            drain_cleanup_callbacks = None
        if callable(close_db):
            await _best_effort_close("storage database", close_db)
        if callable(drain_cleanup_callbacks):
            await _best_effort_close("storage cleanup callbacks", drain_cleanup_callbacks)
    finally:
        _shared_runtime_clients_closing = False

__all__ = [
    'technical_analysis',
    'backtest_engine',
    'pattern_recognition',
    'build_cost_model',
    'effective_cost_rate',
    'resolve_cost_assumptions',
    'register_artifact',
    'register_artifact_async',
    'get_artifact',
    'list_artifacts',
    'get_artifact_async',
    'list_artifacts_async',
    'FACTOR_MEMORY_STRATEGY',
    'FACTOR_MEMORY_VERSION',
    'save_factor_candidate_record',
    'get_factor_candidate_record',
    'get_factor_candidate_record_async',
    'list_factor_candidate_records_async',
    'build_signal_definition',
    'evaluate_signal',
    'FactorLLMConfig',
    'FactorLLMProvider',
    'FactorLLMRequestError',
    'close_factor_llm_provider',
    'get_factor_llm_provider',
    'get_factor_candidate_schema_path',
    'load_factor_candidate_schema',
    'validate_factor_generation_payload',
    'SUPPORTED_FACTOR_FIELDS',
    'SUPPORTED_FACTOR_FUNCTIONS',
    'build_factor_feature_frame',
    'compile_factor_candidate',
    'evaluate_compiled_factor',
    'validate_factor_candidate_pipeline',
    'run_factor_validation_bootstrap',
    'FactorResearchMemoryService',
    'get_factor_research_memory_service',
    'FACTOR_EXTERNAL_RESEARCH_STRATEGY',
    'FACTOR_EXTERNAL_RESEARCH_VERSION',
    'collect_external_factor_research',
    'ingest_external_factor_research',
    'close_strategy_llm_provider',
    'close_strategy_text_embedding_service',
    'close_shared_runtime_clients',
    'bootstrap_ic_ci',
    'deflated_sharpe_ratio',
    'probability_of_backtest_overfitting',
    'white_reality_check',
    'hansen_spa_test',
    'create_chain',
    'add_evidence',
    'make_evidence',
    'set_conclusion',
    'save_chain',
    'get_chain',
    'query_chains_by_code',
    'query_chains_by_date',
    'list_chains',
    'summarize_chain',
]
