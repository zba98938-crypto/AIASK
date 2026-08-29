"""告警工具 — P3 增强：RSI/MACD/volume 指标评估 + combo 告警 + DB 持久化"""

import json
import logging
import time
from typing import List, Dict, Any
import numpy as np
from ..utils import fail, normalize_code, resolve_existing_security_code_sync
from ..services.market_data_access import FALLBACK_DB_ONLY, get_quote_snapshot_response
from .manager_protocol import fail_with_meta, ok_with_meta

logger = logging.getLogger(__name__)

# 进程内告警存储（供 alerts.py 与 alerts_manager.py 共享）
_alerts_store: Dict[str, Dict[str, Any]] = {}

# 比较运算符
_COMPARE_OPS = {
    '>': lambda p, v: p > v,
    '<': lambda p, v: p < v,
    '>=': lambda p, v: p >= v,
    '<=': lambda p, v: p <= v,
    '==': lambda p, v: abs(p - v) < 1e-6,
}
_SUPPORTED_INDICATORS = {'price', 'change_pct', 'rsi', 'macd', 'ma5', 'ma20', 'volume'}
_VALID_COMBO_LOGICS = {'AND', 'OR'}


def _validate_indicator_alert_payload(code: str, indicator: str, condition: str) -> tuple[str | None, str | None]:
    normalized_code, _, code_error = resolve_existing_security_code_sync(stock_code=code)
    if code_error:
        return None, code_error
    if str(indicator or "").strip().lower() not in _SUPPORTED_INDICATORS:
        return None, f"indicator 无效: {indicator}. 支持: {', '.join(sorted(_SUPPORTED_INDICATORS))}"
    if condition not in _COMPARE_OPS:
        return None, f"condition 无效: {condition}. 支持: {', '.join(_COMPARE_OPS.keys())}"
    return normalized_code, None


async def _indicator_alert_exists(code: str, indicator: str, condition: str, *, user_id: str = "default") -> bool:
    alert_id = f'alert_{code}_{indicator}_{condition}'
    if alert_id in _alerts_store:
        return True
    try:
        from ..storage import get_db

        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM alerts
                WHERE user_id = $1 AND code = $2 AND indicator = $3 AND condition = $4
                LIMIT 1
                """,
                user_id,
                code,
                indicator,
                condition,
            )
        return row is not None
    except Exception:
        return False


def _indicator_row_to_alert(row: Any) -> dict[str, Any] | None:
    code = normalize_code(str(row.get('code', '') or ''))
    indicator = str(row.get('indicator', '') or '').strip().lower()
    condition = str(row.get('condition', '') or '').strip()
    if not code or indicator not in _SUPPORTED_INDICATORS or condition not in _COMPARE_OPS:
        return None
    return {
        'alert_id': f"alert_{code}_{indicator}_{condition}",
        'code': code,
        'indicator': indicator,
        'condition': condition,
        'value': float(row.get('value', 0) or 0),
        'active': True,
        'type': 'indicator',
        'triggered': False,
    }


def _combo_row_to_alert(row: Any) -> dict[str, Any] | None:
    logic = str(row.get('logic', 'AND') or 'AND').upper().strip()
    if logic not in _VALID_COMBO_LOGICS:
        return None
    name = str(row.get('name', '') or '').strip()
    if not name:
        return None
    conds = row.get('conditions', '[]')
    if isinstance(conds, str):
        try:
            conds = json.loads(conds)
        except Exception:
            return None
    if not isinstance(conds, list):
        return None
    return {
        'alert_id': f'combo_{name}',
        'name': name,
        'conditions': conds,
        'logic': logic,
        'active': True,
        'type': 'combo',
        'triggered': False,
    }


def _calc_rsi(closes: list, period: int = 14) -> float | None:
    """计算 RSI"""
    if len(closes) < period + 1:
        return None
    arr = np.array(closes, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _calc_macd_histogram(closes: list) -> float | None:
    """计算 MACD 柱值（DIF - DEA）"""
    if len(closes) < 35:
        return None
    arr = np.array(closes, dtype=float)
    ema12 = _ema(arr, 12)
    ema26 = _ema(arr, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    return float((dif[-1] - dea[-1]) * 2)


def _calc_moving_average(closes: list, period: int) -> float | None:
    """计算最近一个周期的简单移动平均。"""
    if len(closes) < period:
        return None
    window = np.array(closes[-period:], dtype=float)
    return float(np.mean(window))

def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均"""
    alpha = 2.0 / (period + 1)
    result = np.empty_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


async def _get_closes(code: str, limit: int = 60) -> list:
    """获取最近 K 线收盘价"""
    try:
        from ..storage import get_db
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT close FROM kline_1d WHERE code=$1 ORDER BY time DESC LIMIT $2",
                code, limit
            )
            return [float(r['close']) for r in reversed(rows)]
    except Exception:
        return []


async def _evaluate_indicator(alert: dict, quote_cache: dict) -> dict:
    """评估单个指标告警，返回带 triggered/current_value 的 alert dict"""
    item = dict(alert)
    item['triggered'] = False
    code = item.get('code', '')
    indicator = str(item.get('indicator', '')).lower()
    condition = item.get('condition')
    threshold = float(item.get('value', 0.0))

    current_value = None

    if indicator == 'price':
        if code and code not in quote_cache:
            quote_cache[code] = await get_quote_snapshot_response(code, fallback_mode=FALLBACK_DB_ONLY) if code else {}
        quote = quote_cache.get(code, {})
        quote_data = quote.get('data') or {} if isinstance(quote, dict) else {}
        current_value = quote_data.get('price')

    elif indicator == 'change_pct':
        if code and code not in quote_cache:
            quote_cache[code] = await get_quote_snapshot_response(code, fallback_mode=FALLBACK_DB_ONLY) if code else {}
        quote = quote_cache.get(code, {})
        quote_data = quote.get('data') or {} if isinstance(quote, dict) else {}
        current_value = quote_data.get('changePercent')

    elif indicator == 'rsi':
        closes = await _get_closes(code)
        current_value = _calc_rsi(closes)

    elif indicator == 'macd':
        closes = await _get_closes(code)
        current_value = _calc_macd_histogram(closes)

    elif indicator == 'ma5':
        closes = await _get_closes(code)
        current_value = _calc_moving_average(closes, 5)

    elif indicator == 'ma20':
        closes = await _get_closes(code)
        current_value = _calc_moving_average(closes, 20)

    elif indicator == 'volume':
        if code and code not in quote_cache:
            quote_cache[code] = await get_quote_snapshot_response(code, fallback_mode=FALLBACK_DB_ONLY) if code else {}
        quote = quote_cache.get(code, {})
        quote_data = quote.get('data') or {} if isinstance(quote, dict) else {}
        current_value = quote_data.get('volume')

    item['current_value'] = current_value
    if current_value is not None and condition in _COMPARE_OPS:
        item['triggered'] = bool(_COMPARE_OPS[condition](float(current_value), threshold))

    return item


async def _evaluate_combo(alert: dict, quote_cache: dict) -> dict:
    """评估组合告警"""
    item = dict(alert)
    item['triggered'] = False
    conditions = item.get('conditions', [])
    logic = str(item.get('logic', 'AND')).upper()

    results = []
    for cond in conditions:
        sub = {
            'code': cond.get('code', ''),
            'indicator': cond.get('indicator', 'price'),
            'condition': cond.get('condition', '>'),
            'value': cond.get('value', 0),
            'type': 'indicator',
        }
        evaluated = await _evaluate_indicator(sub, quote_cache)
        results.append(evaluated.get('triggered', False))

    if logic == 'AND':
        item['triggered'] = all(results) if results else False
    else:
        item['triggered'] = any(results) if results else False

    item['sub_results'] = results
    return item


def register(mcp):
    """注册告警工具"""

    @mcp.tool()
    async def create_indicator_alert(
        code: str,
        indicator: str,
        condition: str,
        value: float
    ):
        """创建指标告警

        Args:
            code: 股票代码
            indicator: 指标名称 ('price', 'rsi', 'macd', 'volume')
            condition: 条件 ('>', '<', '>=', '<=', '==')
            value: 阈值
        """
        started_at = time.perf_counter()
        try:
            code, validation_error = _validate_indicator_alert_payload(code, indicator, condition)
            if validation_error:
                return fail_with_meta(
                    validation_error,
                    tool_name="create_indicator_alert",
                    action="create",
                    started_at=started_at,
                    source_chain=["alerts.memory_store"],
                )
            indicator = str(indicator).strip().lower()
            alert_id = f'alert_{code}_{indicator}_{condition}'
            if await _indicator_alert_exists(code, indicator, condition):
                return fail_with_meta(
                    f"告警已存在: {alert_id}。如需修改请使用 update",
                    tool_name="create_indicator_alert",
                    action="create",
                    started_at=started_at,
                    source_chain=["alerts.memory_store"],
                )
            alert = {
                'alert_id': alert_id,
                'code': code,
                'indicator': indicator,
                'condition': condition,
                'value': float(value),
                'active': True,
                'type': 'indicator',
                'triggered': False,
            }
            _alerts_store[alert_id] = alert

            # 持久化到 DB
            persist_error = None
            try:
                from ..storage import get_db
                db = get_db()
                async with db.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO alerts (user_id, code, indicator, condition, value, status)
                           VALUES ('default', $1, $2, $3, $4, 'active')
                           ON CONFLICT DO NOTHING""",
                        code, indicator, condition, float(value)
                    )
            except Exception as e:
                persist_error = str(e)
                logger.warning("[Alerts] DB persist failed: %s", e)

            if persist_error:
                alert["note"] = f"DB persist failed: {persist_error}"
            return ok_with_meta(
                alert,
                tool_name="create_indicator_alert",
                action="create",
                started_at=started_at,
                source_chain=["alerts.memory_store"],
                extra_meta={
                    "degraded": bool(persist_error),
                    "quality": {"status": "degraded" if persist_error else "available"},
                },
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="create_indicator_alert",
                action="create",
                started_at=started_at,
                source_chain=["alerts.memory_store"],
            )

    @mcp.tool()
    async def create_combo_alert(
        name: str,
        conditions: List[Dict[str, Any]],
        logic: str = 'AND'
    ):
        """创建组合告警

        Args:
            name: 告警名称
            conditions: 条件列表 [{"code":"600519","indicator":"rsi","condition":">","value":70}, ...]
            logic: 逻辑关系 ('AND', 'OR')
        """
        started_at = time.perf_counter()
        try:
            logic = str(logic or 'AND').upper().strip()
            if logic not in _VALID_COMBO_LOGICS:
                return fail_with_meta(
                    f"logic 无效: {logic}. 支持: AND, OR",
                    tool_name="create_combo_alert",
                    action="create",
                    started_at=started_at,
                    source_chain=["alerts.memory_store"],
                )
            alert_id = f'combo_{name}'
            alert = {
                'alert_id': alert_id,
                'name': name,
                'conditions': conditions,
                'logic': logic,
                'active': True,
                'type': 'combo',
                'triggered': False,
            }
            _alerts_store[alert_id] = alert

            # 持久化到 DB combo_alerts 表
            persist_error = None
            try:
                from ..storage import get_db
                db = get_db()
                async with db.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO combo_alerts (name, conditions, logic, status)
                           VALUES ($1, $2, $3, 'active')
                           ON CONFLICT DO NOTHING""",
                        name, json.dumps(conditions), logic
                    )
            except Exception as e:
                persist_error = str(e)
                logger.warning("[Alerts] combo DB persist failed: %s", e)

            if persist_error:
                alert["note"] = f"DB persist failed: {persist_error}"
            return ok_with_meta(
                alert,
                tool_name="create_combo_alert",
                action="create",
                started_at=started_at,
                source_chain=["alerts.memory_store"],
                extra_meta={
                    "degraded": bool(persist_error),
                    "quality": {"status": "degraded" if persist_error else "available"},
                },
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="create_combo_alert",
                action="create",
                started_at=started_at,
                source_chain=["alerts.memory_store"],
            )

    @mcp.tool()
    async def check_all_alerts(
        status: str = 'active',
        alert_type: str = 'all'
    ):
        """检查所有告警（支持 price/rsi/macd/volume 指标 + combo 组合告警）

        Args:
            status: 状态 ('active', 'inactive', 'all')
            alert_type: 类型 ('indicator', 'combo', 'all')
        """
        started_at = time.perf_counter()
        try:
            if status not in {'active', 'inactive', 'all'}:
                return fail(f"invalid status: {status}. supported: active, inactive, all")
            if alert_type not in {'indicator', 'combo', 'all'}:
                return fail(f"invalid alert_type: {alert_type}. supported: indicator, combo, all")
            # 每次从 DB 同步告警（DB 为 source of truth）
            sync_error = None
            warnings: list[str] = []
            skipped_invalid = 0
            try:
                from ..storage import get_db
                db = get_db()
                async with db.acquire() as conn:
                    # 加载指标告警
                    rows = await conn.fetch("SELECT * FROM alerts WHERE status='active'")
                    for r in rows:
                        alert = _indicator_row_to_alert(r)
                        if alert is None:
                            skipped_invalid += 1
                            warnings.append(f"skip invalid indicator alert row id={r.get('id')}")
                            continue
                        _alerts_store[alert['alert_id']] = alert
                    # 加载组合告警
                    combo_rows = await conn.fetch("SELECT * FROM combo_alerts WHERE status='active'")
                    for r in combo_rows:
                        alert = _combo_row_to_alert(r)
                        if alert is None:
                            skipped_invalid += 1
                            warnings.append(f"skip invalid combo alert row id={r.get('id')}")
                            continue
                        _alerts_store[alert['alert_id']] = alert
            except Exception as e:
                sync_error = str(e)
                logger.warning("[Alerts] DB sync failed: %s", e)

            alerts = []
            for cached_alert in list(_alerts_store.values()):
                if cached_alert.get('type') == 'combo':
                    normalized_alert = _combo_row_to_alert(cached_alert)
                else:
                    normalized_alert = _indicator_row_to_alert(cached_alert)
                if normalized_alert is None:
                    skipped_invalid += 1
                    warnings.append(f"skip invalid cached alert alert_id={cached_alert.get('alert_id')}")
                    continue
                _alerts_store[normalized_alert['alert_id']] = normalized_alert
                alerts.append(normalized_alert)
            if status == 'active':
                alerts = [a for a in alerts if a.get('active', True)]
            elif status == 'inactive':
                alerts = [a for a in alerts if not a.get('active', True)]

            if alert_type in ('indicator', 'combo'):
                alerts = [a for a in alerts if a.get('type') == alert_type]

            quote_cache: Dict[str, Any] = {}
            triggered_count = 0
            evaluated: List[Dict[str, Any]] = []

            for alert in alerts:
                if alert.get('type') == 'combo':
                    item = await _evaluate_combo(alert, quote_cache)
                else:
                    item = await _evaluate_indicator(alert, quote_cache)

                if item.get('triggered'):
                    triggered_count += 1
                evaluated.append(item)

            payload = {
                'alerts': evaluated,
                'count': len(evaluated),
                'triggered_count': triggered_count,
                'status': status,
                'type': alert_type,
            }
            if sync_error:
                payload['note'] = f"DB sync failed: {sync_error}"
            if warnings:
                payload['warnings'] = warnings
                payload['skipped_invalid_count'] = skipped_invalid
            return ok_with_meta(
                payload,
                tool_name="check_all_alerts",
                action="check",
                started_at=started_at,
                source_chain=["alerts.memory_store"],
                extra_meta={
                    "degraded": bool(sync_error) or bool(warnings),
                    "quality": {"status": "degraded" if sync_error or warnings else "available"},
                },
            )
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="check_all_alerts",
                action="check",
                started_at=started_at,
                source_chain=["alerts.memory_store"],
            )

