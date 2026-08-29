from ._decision_common import *


async def _resolve_decision_contexts(
    *,
    code: str,
    user_id: str | None,
    stock_context: dict | None,
    quant_context: dict | None,
    event_context: dict | None,
    user_context: dict | None,
):
    tasks = {}
    resolved = {
        'stock': stock_context if isinstance(stock_context, dict) else None,
        'quant': quant_context if isinstance(quant_context, dict) else None,
        'event': event_context if isinstance(event_context, dict) else None,
        'user': user_context if isinstance(user_context, dict) else None,
    }

    if resolved['stock'] is None:
        tasks['stock'] = _build_stock_context(code)
    if resolved['quant'] is None:
        tasks['quant'] = _build_quant_context(code)
    if resolved['event'] is None:
        tasks['event'] = _build_event_context(code)
    if resolved['user'] is None:
        tasks['user'] = _build_user_context(user_id)

    if tasks:
        results = await asyncio.gather(*tasks.values())
        for key, value in zip(tasks.keys(), results):
            resolved[key] = value

    return resolved['stock'], resolved['quant'], resolved['event'], resolved['user']


async def run_decision_gate(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_context: dict | None = None,
    quant_context: dict | None = None,
    event_context: dict | None = None,
    user_context: dict | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """运行统一决策规则闸门，可传入现成上下文，也可按代码自动构建。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        built_stock, built_quant, built_event, built_user = await _resolve_decision_contexts(
            code=code,
            user_id=user_id,
            stock_context=stock_context,
            quant_context=quant_context,
            event_context=event_context,
            user_context=user_context,
        )
        gate = _build_rule_gates(
            code=code,
            investment_style=investment_style,
            stock_context=built_stock,
            quant_context=built_quant,
            event_context=built_event,
            user_context=built_user,
        )
        return ok(gate)
    except Exception as e:
        return fail(str(e))

async def fuse_decision_payload(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_context: dict | None = None,
    quant_context: dict | None = None,
    event_context: dict | None = None,
    user_context: dict | None = None,
    gate: dict | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """融合统一决策上下文，输出 action/summary/weights/raw_ai_output。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        built_stock, built_quant, built_event, built_user = await _resolve_decision_contexts(
            code=code,
            user_id=user_id,
            stock_context=stock_context,
            quant_context=quant_context,
            event_context=event_context,
            user_context=user_context,
        )
        built_gate = gate if isinstance(gate, dict) else _build_rule_gates(
            code=code,
            investment_style=investment_style,
            stock_context=built_stock,
            quant_context=built_quant,
            event_context=built_event,
            user_context=built_user,
        )
        return ok(
            _fuse_unified_decision(
                stock_context=built_stock,
                quant_context=built_quant,
                event_context=built_event,
                user_context=built_user,
                gate=built_gate,
            )
        )
    except Exception as e:
        return fail(str(e))

async def get_unified_decision_summary(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """统一决策摘要：输出前端友好的 summary 卡片。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        payload = await get_unified_decision_summary_payload(
            code=code,
            investment_style=investment_style,
            user_id=user_id,
        )
        return ok(payload)
    except Exception as e:
        return fail(str(e))

async def get_unified_decision_details(
    code: str | None = None,
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """统一决策详情：输出 summary + 全量 details 证据。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        payload = await get_unified_decision_details_payload(
            code=code,
            investment_style=investment_style,
            user_id=user_id,
        )
        return ok(payload)
    except Exception as e:
        return fail(str(e))

async def get_unified_decision(
    code: str | None = None,
    detail_level: str = 'summary',
    investment_style: str = 'balanced',
    user_id: str | None = None,
    stock_code: str | None = None,
    symbol: str | None = None,
    ticker: str | None = None,
):
    """统一决策兼容包装器：按 detail_level 返回 summary 或 details。"""
    try:
        code, error_response = await _resolve_existing_stock_code_or_fail(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error_response is not None:
            return error_response
        if str(detail_level or 'summary').strip().lower() == 'details':
            payload = await get_unified_decision_details_payload(
                code=code,
                investment_style=investment_style,
                user_id=user_id,
            )
        else:
            payload = await get_unified_decision_summary_payload(
                code=code,
                investment_style=investment_style,
                user_id=user_id,
            )
        return ok(payload)
    except Exception as e:
        return fail(str(e))
