"""决策工具"""

from ..storage import get_db
from ..services import technical_analysis
from ..services import (
    add_evidence,
    create_chain,
    make_evidence,
    save_chain,
    set_conclusion,
    summarize_chain,
)
from ..services.decision_contracts import (
    get_unified_decision_details_payload,
    get_unified_decision_summary_payload,
)
from ..services.decision_context_builder import (
    build_stock_context as _build_stock_context,
    build_user_context as _build_user_context,
)
from ..services.decision_quant_builder import build_quant_context as _build_quant_context
from ..services.decision_event_builder import build_event_context as _build_event_context
from ..services.decision_rule_gate import build_rule_gates as _build_rule_gates
from ..services.decision_fusion import fuse_unified_decision as _fuse_unified_decision
from ..services.factor_calculator import factor_calculator
from ..utils import ok, fail, resolve_existing_security_code_async, resolve_security_code
import asyncio
import statistics
import time

from .decision_helpers import (
    _maybe_float,
    _clamp,
    _estimate_volatility,
    _calibrate_buy_probability,
    _estimate_target_price,
    _build_threshold_backtest,
    _build_probability_quality,
    _build_prediction_interval,
    _context_section,
    _derive_contextual_decision,
    _filter_klines_by_as_of,
)
from . import investment_analysis as investment_analysis_mod
from .investment_analysis import get_investment_analysis as _raw_get_investment_analysis


_monkey_patch_lock = asyncio.Lock()


async def _resolve_existing_stock_code_or_fail(
    code: str | None = None,
    *,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
) -> tuple[str | None, dict | None]:
    normalized_code, _, error = await resolve_existing_security_code_async(
        code=code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    if error:
        return None, fail(error)
    return normalized_code, None


async def get_investment_analysis(
    code: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
) -> dict:
    """兼容导出：默认将 decision.get_db 透传到 investment_analysis 模块。"""
    code, error_response = await _resolve_existing_stock_code_or_fail(
        code=code,
        stock_code=stock_code,
        symbol=symbol,
        ticker=ticker,
    )
    if error_response is not None:
        return error_response
    async with _monkey_patch_lock:
        original_get_db = getattr(investment_analysis_mod, 'get_db', None)
        investment_analysis_mod.get_db = get_db
        try:
            return await _raw_get_investment_analysis(code)
        finally:
            if original_get_db is not None:
                investment_analysis_mod.get_db = original_get_db


__all__ = [name for name in globals() if not name.startswith("__")]
