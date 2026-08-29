"""绩效管理器 - 归因分析、绩效评估"""

from typing import Any
import time
from datetime import datetime, timezone
import numpy as np
from ...storage import get_db
from ...utils import normalize_code
from ..manager_protocol import fail_with_meta, normalize_manager_kwargs, normalize_manager_payload, ok_with_meta
from ..market import get_kline

def _normalize_kwargs(kwargs: dict) -> dict:
    """统一解析 kwargs 参数（兼容 JSON 字符串和 dict）"""
    return normalize_manager_kwargs(kwargs)

def _dedupe_chain(values: list[str]) -> list[str]:
    chain = []
    seen = set()
    for value in values:
        label = str(value or "").strip()
        if not label or label in seen:
            continue
        chain.append(label)
        seen.add(label)
    return chain

def _safe_portfolio_id(val):
    """将 portfolio_id 转为 int（SQLite schema 为 integer id）"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return val

def _extract_closes_with_dates(klines):
    """从 K 线记录中提取按时间升序排列的 (日期, 收盘价) 序列。"""
    if not klines:
        return [], np.array([])

    rows = []
    for k in klines:
        if not isinstance(k, dict):
            continue
        close = k.get('close')
        if close is None:
            continue
        try:
            close_v = float(close)
        except Exception:
            continue

        dt = (
            k.get('date')
            or k.get('datetime')
            or k.get('trade_date')
            or k.get('time')
            or ''
        )
        rows.append((str(dt)[:10], close_v))

    if len(rows) < 2:
        return [], np.array([])

    rows.sort(key=lambda x: x[0])
    dates = [r[0] for r in rows]
    closes = np.array([r[1] for r in rows], dtype=float)
    return dates, closes

def _extract_closes(klines):
    """从 K 线记录中提取按时间升序排列的收盘价序列（兼容旧调用）。"""
    _, closes = _extract_closes_with_dates(klines)
    return closes

async def _fetch_klines_raw(db, code: str, lookback_days: int):
    """获取原始 K 线数据（优先 DB，失败回退工具层）。"""
    klines = await db.get_klines(code, limit=lookback_days + 1)
    if not klines or len(klines) < 2:
        res = await get_kline(normalize_code(code), 'daily', lookback_days + 1)
        if res.get('success') and res.get('data'):
            klines = res['data']
    return klines

async def _fetch_dated_returns_for_code(db, code: str, lookback_days: int):
    """获取 (dates, returns, latest_close)，dates 与 returns 等长（日历对齐用）。"""
    klines = await _fetch_klines_raw(db, code, lookback_days)
    dates, closes = _extract_closes_with_dates(klines)
    if len(closes) < 2:
        return [], np.array([]), None
    returns = np.diff(closes) / closes[:-1]
    return dates[1:], returns, float(closes[-1])

async def _fetch_returns_for_code(db, code: str, lookback_days: int):
    """获取单只股票日收益率序列（兼容旧调用）。"""
    _, returns, latest_close = await _fetch_dated_returns_for_code(db, code, lookback_days)
    return returns, latest_close

async def _fetch_close_series_for_code(db, code: str, lookback_days: int) -> np.ndarray:
    """获取单只股票收盘价序列（兼容旧调用）。"""
    klines = await _fetch_klines_raw(db, code, lookback_days)
    return _extract_closes(klines)

def _apply_daily_fee(returns: np.ndarray, fee_disclosure: dict) -> np.ndarray:
    """从日收益率中扣减日化费用（佣金+滑点+冲击）。"""
    if returns is None or len(returns) == 0:
        return returns
    annual_fee = (
        float(fee_disclosure.get('commission_rate', 0))
        + float(fee_disclosure.get('slippage_rate', 0))
        + float(fee_disclosure.get('impact_rate', 0))
    )
    daily_fee = annual_fee / 252.0
    return returns - daily_fee

def _calendar_align_returns(dated_returns_list):
    """基于日历日期交集对齐多只股票的收益率序列。

    Args:
        dated_returns_list: [(dates_list, returns_array), ...]
    Returns:
        (common_dates, aligned_2d_array) — aligned_2d_array shape = (n_stocks, n_dates)
    """
    if not dated_returns_list:
        return [], np.array([])

    # 求所有股票日期的交集
    date_sets = [set(d) for d, _ in dated_returns_list]
    common = date_sets[0]
    for s in date_sets[1:]:
        common &= s
    if len(common) < 2:
        return [], np.array([])

    common_dates = sorted(common)
    aligned = []
    for dates, returns in dated_returns_list:
        date_to_idx = {d: i for i, d in enumerate(dates)}
        row = np.array([float(returns[date_to_idx[d]]) for d in common_dates], dtype=float)
        aligned.append(row)

    return common_dates, np.array(aligned, dtype=float)

async def _build_portfolio_daily_returns(db, holdings, lookback_days: int, fee_disclosure: dict = None):
    """基于持仓构建组合日收益率序列（日历对齐 + 可选费用扣减）。"""
    if not holdings:
        return np.array([])

    dated_list = []
    value_list = []

    for h in holdings:
        code = h.get('code')
        if not code:
            continue

        dates, returns, latest_close = await _fetch_dated_returns_for_code(db, code, lookback_days)
        if len(returns) == 0 or latest_close is None:
            continue

        shares = float(h.get('shares') or 0)
        position_value = shares * latest_close
        if position_value <= 0:
            position_value = 1.0

        dated_list.append((dates, returns))
        value_list.append(position_value)

    if not dated_list:
        return np.array([])

    common_dates, aligned = _calendar_align_returns(dated_list)
    if len(common_dates) < 2:
        return np.array([])

    values = np.array(value_list, dtype=float)
    weights = values / values.sum() if values.sum() > 0 else np.array([1.0 / len(values)] * len(values))

    portfolio_returns = np.dot(weights, aligned)

    # 费用扣减
    if fee_disclosure:
        portfolio_returns = _apply_daily_fee(portfolio_returns, fee_disclosure)

    return portfolio_returns

def _calc_max_drawdown(returns: np.ndarray) -> float:
    """根据收益率序列计算最大回撤。"""
    if returns is None or len(returns) == 0:
        return 0.0

    equity = np.cumprod(1.0 + returns)
    running_max = np.maximum.accumulate(equity)
    drawdown = equity / running_max - 1.0
    return float(abs(np.min(drawdown)))

def _calc_annualized_from_daily(returns: np.ndarray) -> tuple:
    """从日收益率序列计算年化收益和年化波动。"""
    if returns is None or len(returns) == 0:
        return 0.0, 0.0

    mean_daily = float(np.mean(returns))
    ann_return = (1.0 + mean_daily) ** 252 - 1.0

    if len(returns) > 1:
        ann_vol = float(np.std(returns, ddof=1) * np.sqrt(252))
    else:
        ann_vol = 0.0

    return float(ann_return), float(ann_vol)

def _calc_period_return(returns: np.ndarray) -> float:
    """根据收益率序列计算区间累计收益率。"""
    if returns is None or len(returns) == 0:
        return 0.0
    return float(np.prod(1.0 + returns) - 1.0)

def _safe_non_negative_float(v, default: float) -> float:
    try:
        x = float(v)
    except Exception:
        return float(default)
    if not np.isfinite(x) or x < 0:
        return float(default)
    return float(x)

def _build_fee_disclosure(kwargs: dict) -> dict:
    """构建费用口径披露字段（P1-3）。"""
    default_commission = 0.0003
    default_slippage = 0.0005
    default_impact = 0.0002

    commission_rate = _safe_non_negative_float(kwargs.get('commission_rate', default_commission), default_commission)
    slippage_rate = _safe_non_negative_float(kwargs.get('slippage_rate', default_slippage), default_slippage)
    impact_rate = _safe_non_negative_float(kwargs.get('impact_rate', default_impact), default_impact)

    assumptions_source = (
        'input'
        if any(k in kwargs for k in ('commission_rate', 'slippage_rate', 'impact_rate'))
        else 'default'
    )

    return {
        'commission_rate': float(commission_rate),
        'slippage_rate': float(slippage_rate),
        'impact_rate': float(impact_rate),
        'total_annual_fee': float(commission_rate + slippage_rate + impact_rate),
        'assumptions_source': assumptions_source,
        'deducted': True,
        'deduction_method': 'daily_pro_rata',
    }

def _calc_rolling_sharpe(returns: np.ndarray, window: int, risk_free_rate: float) -> list:
    if returns is None or len(returns) < max(2, window):
        return []

    out = []
    rf_daily = float(risk_free_rate) / 252.0
    for end_idx in range(window, len(returns) + 1):
        seg = returns[end_idx - window:end_idx]
        vol = float(np.std(seg, ddof=1)) if len(seg) > 1 else 0.0
        if vol <= 0:
            out.append(0.0)
        else:
            sr = ((float(np.mean(seg)) - rf_daily) / vol) * float(np.sqrt(252))
            out.append(float(sr))
    return out

def _calc_rolling_drawdown(returns: np.ndarray, window: int) -> list:
    if returns is None or len(returns) < max(2, window):
        return []

    out = []
    for end_idx in range(window, len(returns) + 1):
        seg = returns[end_idx - window:end_idx]
        out.append(float(_calc_max_drawdown(seg)))
    return out

def _build_window_audit(lookback_days: int, aligned_days: int, rolling_window: int) -> dict:
    return {
        'lookback_days': int(lookback_days),
        'aligned_days': int(max(0, aligned_days)),
        'rolling_window': int(max(2, rolling_window)),
        'alignment_method': 'calendar_date_intersection',
    }

def _to_pct(v: float) -> str:
    return f"{v * 100:.2f}%"

def _to_float_list(arr: np.ndarray):
    if arr is None or len(arr) == 0:
        return []
    return [float(x) for x in arr.tolist()]

def _compute_timing_component(price_matrix: np.ndarray, start_values: np.ndarray) -> dict:
    """基于价格路径计算择时贡献（真实路径收益 - 静态权重线性收益）。"""
    if (
        price_matrix is None
        or start_values is None
        or price_matrix.ndim != 2
        or len(start_values) != price_matrix.shape[0]
        or price_matrix.shape[1] < 2
    ):
        return {
            "timing_return": 0.0,
            "realized_total_return": 0.0,
            "static_total_return": 0.0,
            "daily_returns": np.array([]),
            "aligned_days": int(price_matrix.shape[1]) if isinstance(price_matrix, np.ndarray) and price_matrix.ndim == 2 else 0,
            "assets_used": 0,
        }

    valid_mask = (
        np.isfinite(start_values)
        & (start_values > 0)
        & np.isfinite(price_matrix[:, 0])
        & (price_matrix[:, 0] > 0)
    )
    if int(np.sum(valid_mask)) < 1:
        return {
            "timing_return": 0.0,
            "realized_total_return": 0.0,
            "static_total_return": 0.0,
            "daily_returns": np.array([]),
            "aligned_days": int(price_matrix.shape[1]),
            "assets_used": 0,
        }

    prices = np.array(price_matrix[valid_mask], dtype=float)
    start_vals = np.array(start_values[valid_mask], dtype=float)

    if prices.shape[1] < 2:
        return {
            "timing_return": 0.0,
            "realized_total_return": 0.0,
            "static_total_return": 0.0,
            "daily_returns": np.array([]),
            "aligned_days": int(prices.shape[1]),
            "assets_used": int(len(start_vals)),
        }

    static_weights = start_vals / float(np.sum(start_vals))
    daily_asset_returns = np.diff(prices, axis=1) / prices[:, :-1]
    daily_asset_returns = np.nan_to_num(daily_asset_returns, nan=0.0, posinf=0.0, neginf=0.0)
    daily_returns = np.dot(static_weights, daily_asset_returns)
    realized_total_return = float(np.prod(1.0 + daily_returns) - 1.0)

    asset_period_returns = prices[:, -1] / prices[:, 0] - 1.0
    static_total_return = float(np.dot(static_weights, asset_period_returns))

    return {
        "timing_return": float(realized_total_return - static_total_return),
        "realized_total_return": realized_total_return,
        "static_total_return": static_total_return,
        "daily_returns": daily_returns,
        "aligned_days": int(prices.shape[1]),
        "assets_used": int(len(start_vals)),
    }
