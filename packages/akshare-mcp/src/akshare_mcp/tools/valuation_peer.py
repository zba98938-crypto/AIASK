"""相对估值 & 历史估值

包含 4 阶段可比公司筛选、相对估值分析、历史估值数据获取等
需要数据库 / 数据源访问的工具实现逻辑。
由 valuation.py 中的 @mcp.tool() 调用。
"""

from typing import Optional, List
import statistics

from ..storage import get_db
from ..utils import ok, fail
from .finance import normalize_financial_payload


_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "pe_ratio": ("pe_ratio", "pe", "peRatio", "ttm_pe", "pe_ttm"),
    "pb_ratio": ("pb_ratio", "pb", "pbRatio", "ttm_pb"),
    "ps_ratio": ("ps_ratio", "ps", "psRatio", "ttm_ps"),
    "market_cap": ("market_cap", "marketCap", "mkt_cap", "total_market_cap", "totalMarketCap", "total_mv"),
}


def _safe_float(value):
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def _normalize_ratio_fraction(value):
    num = _safe_float(value)
    if num is None:
        return None
    # 财务比率在不同源中可能以 0~1 或 0~100 表示；统一转成 0~1 便于比较。
    if abs(num) > 1:
        return num / 100.0
    return num


def _restore_ratio_scale(value, reference):
    num = _safe_float(value)
    ref = _safe_float(reference)
    if num is None:
        return None
    if ref is not None and abs(ref) > 1:
        return num * 100.0
    return num


def _sanitize_positive_metric(
    value,
    *,
    metric: str,
    invalid_bucket: Optional[dict[str, dict[str, object]]] = None,
):
    num = _safe_float(value)
    if num is None:
        if invalid_bucket is not None:
            invalid_bucket[metric] = {
                "reason": "missing",
                "raw_value": value,
            }
        return None
    if num <= 0:
        if invalid_bucket is not None:
            invalid_bucket[metric] = {
                "reason": "non_positive",
                "raw_value": num,
            }
        return None
    return float(num)


def _record_peer_invalid_metric(
    store: dict[str, dict[str, object]],
    *,
    metric: str,
    code: str,
    reason: str,
) -> None:
    bucket = store.setdefault(metric, {"missing": 0, "non_positive": 0, "sample_codes": []})
    bucket[reason] = int(bucket.get(reason, 0) or 0) + 1
    sample_codes = bucket.setdefault("sample_codes", [])
    if code not in sample_codes and len(sample_codes) < 5:
        sample_codes.append(code)


def _canonicalize_metric_name(metric: str) -> str:
    raw = str(metric or "").strip()
    for canonical_name, aliases in _METRIC_ALIASES.items():
        if raw == canonical_name or raw in aliases:
            return canonical_name
    return raw


def _canonicalize_metric_list(metrics: Optional[List[str]]) -> list[str]:
    normalized: list[str] = []
    for metric in list(metrics or []):
        canonical_name = _canonicalize_metric_name(metric)
        if canonical_name and canonical_name not in normalized:
            normalized.append(canonical_name)
    return normalized


def _pick_metric_value(metric: str, *rows: object):
    aliases = _METRIC_ALIASES.get(metric, (metric,))
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in aliases:
            value = row.get(key)
            if value not in (None, ""):
                return value
    return None


def _has_requested_metric_values(row: object, metrics: list[str]) -> bool:
    if not isinstance(row, dict):
        return False
    return any(_pick_metric_value(metric, row) not in (None, "") for metric in metrics)


# ---------------------------------------------------------------------------
# 相对估值（4-stage peer selection）
# ---------------------------------------------------------------------------

async def _relative_valuation_impl(
    code: str,
    metrics: Optional[List[str]] = None,
    peers: Optional[List[str]] = None,
):
    """
    相对估值分析实现。

    Args:
        code: 目标股票代码
        metrics: 估值指标列表，如['pe_ratio', 'pb_ratio', 'ps_ratio']
        peers: 可比公司列表（不填则自动查找同行业公司）
    """
    db = get_db()

    # 默认估值指标
    if not metrics:
        metrics = ['pe_ratio', 'pb_ratio']
    metrics = _canonicalize_metric_list(metrics) or ['pe_ratio', 'pb_ratio']

    # 获取目标股票信息
    target_info = await db.get_stock_info(code)
    if not target_info:
        return fail(f'Stock {code} not found')

    target_industry = target_info.get('industry', '')
    target_market_cap = float(target_info.get('market_cap') or 0.0)
    target_financial = None
    try:
        target_financial = await db.get_financials(code, limit=1)
    except Exception:
        target_financial = None
    if not target_financial and not _has_requested_metric_values(target_info, metrics):
        try:
            from .finance import get_financials as _api_get_financials
            fin_res = await _api_get_financials(code)
            if fin_res and fin_res.get('success') and fin_res.get('data'):
                target_financial = [fin_res['data']]
        except Exception:
            pass

    def _latest_row(rows):
        if isinstance(rows, list) and rows:
            for item in rows:
                if isinstance(item, dict):
                    return item
        if isinstance(rows, dict):
            return rows
        return None

    def _first_number(row: dict, keys: List[str]):
        if not isinstance(row, dict):
            return None
        for k in keys:
            if k in row and row.get(k) is not None:
                val = _safe_float(row.get(k))
                if val is not None:
                    return val
        return None

    def _derive_growth(fin_row: dict):
        fin_row = normalize_financial_payload(fin_row, include_aliases=False) or {}
        return _first_number(fin_row, [
            'revenueGrowth', 'profitGrowth',
            'revenue_yoy', 'revenue_growth', 'growth_rate',
            'net_profit_growth', 'profit_growth',
        ])

    def _derive_cashflow_quality(fin_row: dict):
        fin_row = normalize_financial_payload(fin_row, include_aliases=False) or {}
        ocf = _first_number(fin_row, [
            'operatingCashFlow',
            'operating_cash_flow',
            'net_operate_cash_flow',
            'net_cash_flow_from_operating_activities',
            'cashflow_from_operations',
        ])
        net_profit = _first_number(fin_row, [
            'netProfit',
            'net_profit',
            'net_income',
            'profit',
        ])
        ratio = None
        if net_profit is not None and abs(net_profit) > 1e-9 and ocf is not None:
            ratio = ocf / net_profit
        return ocf, net_profit, ratio

    target_fin_row = normalize_financial_payload(_latest_row(target_financial) or {}, include_aliases=False) or {}
    target_roe = _safe_float(target_fin_row.get('roe'))
    target_debt_ratio = _safe_float(target_fin_row.get('debtRatio'))
    target_growth = _derive_growth(target_fin_row)
    target_ocf, target_net_profit, target_ocf_profit_ratio = _derive_cashflow_quality(target_fin_row)

    # 获取目标股票估值指标
    target_metrics = {}
    invalid_target_metrics: dict[str, dict[str, object]] = {}
    for metric in metrics:
        value = _sanitize_positive_metric(
            _pick_metric_value(metric, target_info, target_fin_row),
            metric=metric,
            invalid_bucket=invalid_target_metrics,
        )
        if value is not None:
            target_metrics[metric] = value

    if not target_metrics:
        response = fail(f'No valid valuation metrics for {code}')
        response['requested_metrics'] = list(metrics or [])
        response['invalid_target_metrics'] = invalid_target_metrics
        return response

    # 查找可比公司（优先同行业，扩大样本后再做层层过滤）
    peer_source = 'explicit'
    peer_fallback_reasons: list[str] = []
    if not peers:
        async with db.acquire() as conn:
            code_col = await db._stocks_code_column(conn) if hasattr(db, "_stocks_code_column") else "code"
            rows = []
            if target_industry:
                rows = await conn.fetch(
                    f"""SELECT {code_col} AS code FROM stocks
                       WHERE industry = $1 AND {code_col} != $2
                       LIMIT 200""",
                    target_industry, code
                )
                peers = [row['code'] for row in rows]
                peer_source = 'industry'
                if not peers:
                    peer_fallback_reasons.append('industry_peer_empty')
            else:
                peer_fallback_reasons.append('industry_missing')

            if not peers:
                if target_market_cap > 0:
                    rows = await conn.fetch(
                        f"""SELECT {code_col} AS code FROM stocks
                           WHERE {code_col} != $1
                           ORDER BY ABS(COALESCE(market_cap, 0) - $2) ASC
                           LIMIT 200""",
                        code, target_market_cap
                    )
                else:
                    rows = await conn.fetch(
                        f"""SELECT {code_col} AS code FROM stocks
                           WHERE {code_col} != $1
                           ORDER BY market_cap DESC NULLS LAST
                           LIMIT 200""",
                        code
                    )
                peers = [row['code'] for row in rows]
                peer_source = 'market_cap_fallback'

    if not peers:
        return fail(f'No peer companies found for {code}')

    # 获取候选可比公司估值与质量数据
    peer_candidates = []
    invalid_peer_metrics: dict[str, dict[str, object]] = {}
    peers_without_valid_metrics = 0
    for peer_code in peers:
        peer_info = await db.get_stock_info(peer_code)
        if not peer_info:
            continue

        peer_metrics = {'code': peer_code, 'name': peer_info.get('name', '')}
        peer_financial = None
        try:
            peer_financial = await db.get_financials(peer_code, limit=1)
        except Exception:
            peer_financial = None
        if not peer_financial and not _has_requested_metric_values(peer_info, metrics):
            try:
                from .finance import get_financials as _api_get_financials
                fin_res = await _api_get_financials(peer_code)
                if fin_res and fin_res.get('success') and fin_res.get('data'):
                    peer_financial = [fin_res['data']]
            except Exception:
                pass
        peer_fin_row = normalize_financial_payload(_latest_row(peer_financial) or {}, include_aliases=False) or {}
        valid = False
        for metric in metrics:
            value = _safe_float(_pick_metric_value(metric, peer_info, peer_fin_row))
            if value is None:
                _record_peer_invalid_metric(invalid_peer_metrics, metric=metric, code=peer_code, reason='missing')
                continue
            if value <= 0:
                _record_peer_invalid_metric(invalid_peer_metrics, metric=metric, code=peer_code, reason='non_positive')
                continue
            peer_metrics[metric] = float(value)
            valid = True

        if valid:
            peer_metrics['_market_cap'] = _sanitize_positive_metric(
                _pick_metric_value('market_cap', peer_info, peer_fin_row),
                metric='market_cap',
            ) or 0.0
            peer_metrics['_roe'] = _safe_float(peer_fin_row.get('roe'))
            peer_metrics['_debt_ratio'] = _safe_float(peer_fin_row.get('debtRatio'))
            peer_metrics['_growth'] = _derive_growth(peer_fin_row)
            peer_ocf, peer_profit, peer_ocf_profit_ratio = _derive_cashflow_quality(peer_fin_row)
            peer_metrics['_ocf'] = peer_ocf
            peer_metrics['_net_profit'] = peer_profit
            peer_metrics['_ocf_profit_ratio'] = peer_ocf_profit_ratio
            peer_candidates.append(peer_metrics)
        else:
            peers_without_valid_metrics += 1

    if not peer_candidates:
        response = fail('No valid peer data found')
        response['requested_metrics'] = list(metrics or [])
        response['invalid_target_metrics'] = invalid_target_metrics
        response['invalid_peer_metrics'] = invalid_peer_metrics
        return response

    peer_pool_build = {
        'candidate_count': len(peer_candidates),
        'peer_source': peer_source,
        'fallback_reasons': peer_fallback_reasons,
        'size_filter_relaxed': False,
        'quality_filter_relaxed': False,
        'growth_filter_relaxed': False,
        'cashflow_filter_relaxed': False,
        'size_ratio_min': 0.3,
        'size_ratio_max': 3.0,
        'quality_thresholds': {},
        'growth_thresholds': {},
        'cashflow_thresholds': {},
        'relaxation_reasons': [],
        'requested_metrics': list(metrics or []),
        'peers_without_valid_metrics': peers_without_valid_metrics,
    }

    # 过滤1：规模可比（默认 0.3x~3x）
    peer_stage = peer_candidates
    size_filtered = peer_stage
    if target_market_cap > 0:
        size_filtered = [
            p for p in peer_stage
            if p.get('_market_cap', 0.0) > 0
            and 0.3 <= (p.get('_market_cap', 0.0) / target_market_cap) <= 3.0
        ]
        peer_pool_build['after_size_filter'] = len(size_filtered)
        if len(size_filtered) >= 5:
            peer_stage = size_filtered
        else:
            peer_pool_build['size_filter_relaxed'] = True
    else:
        peer_pool_build['after_size_filter'] = len(peer_stage)

    # 过滤2：质量可比（ROE/负债率）
    quality_filtered = peer_stage
    roe_threshold = None
    debt_threshold = None
    debt_threshold_fraction = None
    if target_roe is not None:
        roe_threshold = max(0.05, target_roe * 0.5)
        peer_pool_build['quality_thresholds']['roe_min'] = roe_threshold
    if target_debt_ratio is not None:
        target_debt_fraction = _normalize_ratio_fraction(target_debt_ratio)
        if target_debt_fraction is not None:
            debt_threshold_fraction = min(0.95, target_debt_fraction + 0.25)
            debt_threshold = _restore_ratio_scale(debt_threshold_fraction, target_debt_ratio)
            peer_pool_build['quality_thresholds']['debt_ratio_max'] = debt_threshold

    if roe_threshold is not None or debt_threshold is not None:
        quality_filtered = []
        for p in peer_stage:
            ok_roe = True
            if roe_threshold is not None:
                peer_roe = p.get('_roe')
                ok_roe = (peer_roe is not None) and (peer_roe >= roe_threshold)

            ok_debt = True
            if debt_threshold_fraction is not None:
                peer_debt = _normalize_ratio_fraction(p.get('_debt_ratio'))
                ok_debt = (peer_debt is None) or (peer_debt <= debt_threshold_fraction)

            if ok_roe and ok_debt:
                quality_filtered.append(p)

        peer_pool_build['after_quality_filter'] = len(quality_filtered)
        if len(quality_filtered) >= 5:
            peer_stage = quality_filtered
        else:
            peer_pool_build['quality_filter_relaxed'] = True
            peer_pool_build['relaxation_reasons'].append('quality_filter_relaxed_due_to_small_sample')
    else:
        peer_pool_build['after_quality_filter'] = len(peer_stage)

    # 过滤3：成长可比（优先使用 revenue/profit growth）
    growth_filtered = peer_stage
    growth_target = target_growth
    growth_floor = None
    growth_ceil = None
    if growth_target is not None:
        growth_tolerance = max(0.08, abs(growth_target) * 0.6)
        growth_floor = growth_target - growth_tolerance
        growth_ceil = growth_target + growth_tolerance
        peer_pool_build['growth_thresholds'] = {
            'target_growth': float(growth_target),
            'growth_floor': float(growth_floor),
            'growth_ceil': float(growth_ceil),
            'mode': 'around_target',
        }
    else:
        growth_floor = -0.30
        peer_pool_build['growth_thresholds'] = {
            'growth_floor': float(growth_floor),
            'mode': 'minimum_floor',
        }

    growth_filtered = []
    growth_data_available = False
    for p in peer_stage:
        g = p.get('_growth')
        if g is None:
            continue
        growth_data_available = True
        if growth_ceil is not None:
            if growth_floor <= g <= growth_ceil:
                growth_filtered.append(p)
        else:
            if g >= growth_floor:
                growth_filtered.append(p)

    peer_pool_build['after_growth_filter'] = len(growth_filtered)
    if growth_data_available and len(growth_filtered) >= 5:
        peer_stage = growth_filtered
    else:
        peer_pool_build['growth_filter_relaxed'] = True
        if not growth_data_available:
            peer_pool_build['relaxation_reasons'].append('growth_filter_relaxed_due_to_missing_data')
        else:
            peer_pool_build['relaxation_reasons'].append('growth_filter_relaxed_due_to_small_sample')

    # 过滤4：现金流口径一致性（经营现金流与净利润匹配）
    cashflow_filtered = []
    cashflow_data_available = False

    if target_ocf_profit_ratio is not None:
        ratio_floor = max(0.2, target_ocf_profit_ratio * 0.5)
        ratio_ceil = max(ratio_floor + 0.1, target_ocf_profit_ratio * 1.5)
        peer_pool_build['cashflow_thresholds'] = {
            'target_ocf_profit_ratio': float(target_ocf_profit_ratio),
            'ratio_floor': float(ratio_floor),
            'ratio_ceil': float(ratio_ceil),
            'require_positive_ocf': bool((target_ocf or 0.0) > 0),
            'mode': 'ratio_band',
        }
    else:
        ratio_floor = 0.2
        ratio_ceil = None
        peer_pool_build['cashflow_thresholds'] = {
            'ratio_floor': float(ratio_floor),
            'require_positive_ocf': True,
            'mode': 'minimum_ratio_or_positive_ocf',
        }

    for p in peer_stage:
        ocf = p.get('_ocf')
        ratio = p.get('_ocf_profit_ratio')
        has_any = (ocf is not None) or (ratio is not None)
        if not has_any:
            continue
        cashflow_data_available = True

        ok_cash = True
        if ratio is not None:
            ok_cash = ratio >= ratio_floor
            if ratio_ceil is not None:
                ok_cash = ok_cash and (ratio <= ratio_ceil)
        elif ocf is not None:
            ok_cash = ocf > 0

        if ok_cash:
            cashflow_filtered.append(p)

    peer_pool_build['after_cashflow_filter'] = len(cashflow_filtered)
    if cashflow_data_available and len(cashflow_filtered) >= 5:
        peer_stage = cashflow_filtered
    else:
        peer_pool_build['cashflow_filter_relaxed'] = True
        if not cashflow_data_available:
            peer_pool_build['relaxation_reasons'].append('cashflow_filter_relaxed_due_to_missing_data')
        else:
            peer_pool_build['relaxation_reasons'].append('cashflow_filter_relaxed_due_to_small_sample')

    # 排序：优先规模更接近目标
    if target_market_cap > 0:
        peer_stage = sorted(
            peer_stage,
            key=lambda p: abs((p.get('_market_cap', target_market_cap) / target_market_cap) - 1.0),
        )

    peer_data = []
    for p in peer_stage:
        public_row = {k: v for k, v in p.items() if not str(k).startswith('_')}
        peer_data.append(public_row)

    if not peer_data:
        return fail('No valid peer data found')

    # 计算行业统计
    industry_stats = {}
    for metric in metrics:
        values = [p[metric] for p in peer_data if metric in p]
        if values:
            industry_stats[metric] = {
                'mean': float(statistics.mean(values)),
                'median': float(statistics.median(values)),
                'min': float(min(values)),
                'max': float(max(values)),
                'count': len(values)
            }

    # 计算相对估值
    comparison = {}
    for metric in target_metrics:
        if metric in industry_stats:
            target_value = target_metrics[metric]
            industry_mean = industry_stats[metric]['mean']
            industry_median = industry_stats[metric]['median']

            ptm = float((target_value - industry_median) / industry_median * 100) if industry_median else None
            # 偏离风险标记：|premium_to_median| > 30% 为 high
            if ptm is not None:
                abs_ptm = abs(ptm)
                if abs_ptm > 50:
                    deviation_risk = 'extreme'
                elif abs_ptm > 30:
                    deviation_risk = 'high'
                else:
                    deviation_risk = 'normal'
                risk_flag = 'high_premium' if ptm > 30 else ('high_discount' if ptm < -30 else 'normal')
            else:
                deviation_risk = None
                risk_flag = None

            comparison[metric] = {
                'target': target_value,
                'industry_mean': industry_mean,
                'industry_median': industry_median,
                'premium_to_mean': float((target_value - industry_mean) / industry_mean * 100) if industry_mean else None,
                'premium_to_median': ptm,
                'deviation_risk': deviation_risk,
                'risk_flag': risk_flag,
                'percentile': float(sum(1 for p in peer_data if p.get(metric, float('inf')) < target_value) / len(peer_data) * 100)
            }

    return ok({
        'code': code,
        'name': target_info.get('name', ''),
        'industry': target_industry,
        'target_metrics': target_metrics,
        'invalid_target_metrics': invalid_target_metrics,
        'invalid_peer_metrics': invalid_peer_metrics,
        'industry_stats': industry_stats,
        'comparison': comparison,
        'peer_count': len(peer_data),
        'peer_pool_build': peer_pool_build,
        'peers': peer_data[:10]  # 只返回前10个可比公司
    })


# ---------------------------------------------------------------------------
# 历史估值
# ---------------------------------------------------------------------------

async def _get_historical_valuation_impl(
    code: str,
    days: int = 30,
):
    """
    获取历史估值数据实现。

    已知限制与降级策略：
    - 首选 stock_quotes 历史库；无数据时按 Tushare -> AkShare -> Baostock 依次降级。
    - 不同源字段可用性不同，可能出现仅价格可用、估值字段缺失。
    - 返回新增 data_quality/source_chain/fallback_reason，便于审计与质量评估。
    """
    from datetime import datetime, timedelta

    db = get_db()
    requested_rows = max(int(days or 30), 1)
    rows = []
    db_query_failed = False
    source_chain: list[str] = ['db.stock_quotes']
    fallback_reason: list[str] = []

    def _append_source(name: str) -> None:
        if name not in source_chain:
            source_chain.append(name)

    def _to_float(value):
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _normalize_date(value) -> str | None:
        if value is None:
            return None
        try:
            if hasattr(value, 'strftime'):
                return value.strftime('%Y-%m-%d')
        except Exception:
            pass
        text = str(value).strip()
        if not text:
            return None
        if len(text) >= 10 and text[4] == '-' and text[7] == '-':
            return text[:10]
        digits = ''.join(ch for ch in text if ch.isdigit())
        if len(digits) >= 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        return text[:10] or None

    invalid_value_fields: dict[str, int] = {}

    def _sanitize_history_metric(field: str, value):
        number = _to_float(value)
        if number is None:
            return None
        if field in ('pe_ratio', 'pb_ratio', 'ps_ratio', 'market_cap', 'price') and number <= 0:
            invalid_value_fields[field] = invalid_value_fields.get(field, 0) + 1
            return None
        return number

    def _make_history_row(
        *,
        date=None,
        pe_ratio=None,
        pb_ratio=None,
        market_cap=None,
        price=None,
    ) -> dict:
        return {
            'date': _normalize_date(date),
            'pe_ratio': _sanitize_history_metric('pe_ratio', pe_ratio),
            'pb_ratio': _sanitize_history_metric('pb_ratio', pb_ratio),
            'market_cap': _sanitize_history_metric('market_cap', market_cap),
            'price': _sanitize_history_metric('price', price),
        }

    def _record_score(item: dict) -> int:
        return sum(item.get(k) is not None for k in ('pe_ratio', 'pb_ratio', 'market_cap', 'price'))

    def _deduplicate(items: list[dict]) -> list[dict]:
        dedup_map: dict[str, dict] = {}
        for idx, item in enumerate(items):
            key = item.get('date') or f'__nodate_{idx}'
            old = dedup_map.get(key)
            if old is None or _record_score(item) > _record_score(old):
                dedup_map[key] = item
        history = list(dedup_map.values())
        history.sort(key=lambda x: (x.get('date') is not None, x.get('date') or ''), reverse=True)
        return history

    def _history_quality(items: list[dict]) -> dict:
        deduped = _deduplicate(items)
        row_count = len(deduped)
        valuation_rows = sum(
            1 for item in deduped
            if item.get('pe_ratio') is not None or item.get('pb_ratio') is not None
        )
        complete_rows = sum(
            1 for item in deduped
            if all(item.get(k) is not None for k in ('pe_ratio', 'pb_ratio', 'market_cap', 'price'))
        )
        return {
            'history': deduped,
            'row_count': row_count,
            'valuation_rows': valuation_rows,
            'complete_rows': complete_rows,
        }

    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """SELECT time, pe, pb, mkt_cap, price
                   FROM stock_quotes
                   WHERE code = $1
                   AND time >= datetime(CURRENT_TIMESTAMP, '-' || $2 || ' days')
                   ORDER BY time DESC""",
                code, days
            )
    except Exception as e:
        rows = []
        db_query_failed = True
        fallback_reason.append(f'stock_quotes查询失败: {e}')

    # 0a. 用 tdx_stock_extra 补充缺失的 PE/PB 历史（sync_more_info 每日填充）
    try:
        async with db.acquire() as conn:
            tdx_rows = await conn.fetch(
                """SELECT trade_date AS time, pe_ttm AS pe, pb_mrq AS pb,
                          zsz AS mkt_cap, NULL AS price
                   FROM tdx_stock_extra
                   WHERE code = $1
                     AND trade_date >= date(CURRENT_TIMESTAMP, '-' || $2 || ' days')
                   ORDER BY trade_date DESC""",
                code, days
            )
        if tdx_rows:
            existing_dates = set()
            for r in (rows or []):
                t_val = r.get('time') if isinstance(r, dict) else r['time']
                existing_dates.add(_normalize_date(t_val))
            extra_rows = []
            for r in tdx_rows:
                ts = _normalize_date(dict(r).get('time'))
                if ts and ts not in existing_dates:
                    extra_rows.append(dict(r))
            if extra_rows:
                rows = list(rows or []) + extra_rows
                _append_source('tqcenter.tdx_stock_extra')
    except Exception as e:
        fallback_reason.append(f'tdx_stock_extra查询失败: {e}')

    history_raw = [
        _make_history_row(
            date=row.get('time') if isinstance(row, dict) else row['time'],
            pe_ratio=row.get('pe') if isinstance(row, dict) else row['pe'],
            pb_ratio=row.get('pb') if isinstance(row, dict) else row['pb'],
            market_cap=row.get('mkt_cap') if isinstance(row, dict) else row['mkt_cap'],
            price=row.get('price') if isinstance(row, dict) else row['price'],
        )
        for row in (rows or [])
    ]

    quality = _history_quality(history_raw)
    needs_external_valuation = (not db_query_failed) and quality['valuation_rows'] == 0

    if needs_external_valuation:
        if quality['row_count'] > 0:
            fallback_reason.append('stock_quotes 历史估值覆盖不足，已补充外部数据源')
        else:
            fallback_reason.append('stock_quotes 无历史估值序列，已按降级链路补充外部数据源')
        _append_source('tushare.daily_basic')
        try:
            from ..data_source import data_source
            pro = data_source.get_tushare_pro()
            if pro:
                ts_code = f"{code}.SH" if str(code).startswith('6') else f"{code}.SZ"
                end_date = datetime.now().strftime('%Y%m%d')
                start_date = (datetime.now() - timedelta(days=max(days, 1))).strftime('%Y%m%d')
                df = pro.daily_basic(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    fields='trade_date,pe_ttm,pb,total_mv,close',
                )
                if df is not None and not df.empty:
                    for _, r in df.iterrows():
                        history_raw.append(
                            _make_history_row(
                                date=r.get('trade_date'),
                                pe_ratio=r.get('pe_ttm'),
                                pb_ratio=r.get('pb'),
                                market_cap=r.get('total_mv'),
                                price=r.get('close'),
                            )
                        )
        except Exception as e:
            fallback_reason.append(f'Tushare降级失败: {e}')

        quality = _history_quality(history_raw)
        needs_external_valuation = quality['valuation_rows'] == 0

    if needs_external_valuation:
        try:
            import akshare as ak

            preferred_attr = 'stock_a_indicator_lg' if hasattr(ak, 'stock_a_indicator_lg') else (
                'stock_value_em' if hasattr(ak, 'stock_value_em') else None
            )
            if preferred_attr is None:
                raise AttributeError('stock_a_indicator_lg / stock_value_em 均不可用')

            source_name = f'akshare.{preferred_attr}'
            _append_source(source_name)
            try:
                df = getattr(ak, preferred_attr)(symbol=code)
                if df is not None and not df.empty:
                    tail_df = df.tail(max(requested_rows, 1)) if hasattr(df, 'tail') else df
                    for _, r in tail_df.iterrows():
                        history_raw.append(
                            _make_history_row(
                                date=r.get('trade_date') or r.get('日期') or r.get('数据日期'),
                                pe_ratio=r.get('pe') if 'pe' in r else (r.get('PE(TTM)') if 'PE(TTM)' in r else r.get('市盈率')),
                                pb_ratio=r.get('pb') if 'pb' in r else r.get('市净率'),
                                market_cap=r.get('total_mv') if 'total_mv' in r else r.get('总市值'),
                                price=r.get('close') if 'close' in r else (r.get('当日收盘价') if '当日收盘价' in r else r.get('收盘价')),
                            )
                        )
            except Exception as e:
                fallback_reason.append(f'{source_name} 降级失败: {e}')
        except Exception as e:
            fallback_reason.append(f'AkShare导入失败: {e}')

        quality = _history_quality(history_raw)

    if not db_query_failed and quality['valuation_rows'] == 0 and quality['row_count'] == 0:
        _append_source('baostock.history_k_data_plus')
        try:
            from ..baostock_api import baostock_client
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=max(days * 2, 5))).strftime('%Y-%m-%d')
            df = baostock_client.query_history_k_data_plus(
                code,
                fields='date,close',
                start_date=start_date,
                end_date=end_date,
                frequency='d',
                adjustflag='2',
            )
            if df is not None and not df.empty:
                tail_df = df.tail(max(requested_rows, 1)) if hasattr(df, 'tail') else df
                for _, r in tail_df.iterrows():
                    history_raw.append(
                        _make_history_row(
                            date=r.get('date'),
                            price=r.get('close'),
                        )
                    )
        except Exception as e:
            fallback_reason.append(f'Baostock降级失败: {e}')
        quality = _history_quality(history_raw)

    if quality['valuation_rows'] == 0:
        history_raw = []
        _append_source('db.get_stock_info')
        stock_info = None
        try:
            stock_info = await db.get_stock_info(code)
        except Exception as e:
            fallback_reason.append(f'db.get_stock_info 降级失败: {e}')
        if stock_info:
            fallback_reason.append('无历史估值序列，已降级为股票基础估值快照')
            history_raw.append(
                _make_history_row(
                    date=None,
                    pe_ratio=stock_info.get('pe_ratio'),
                    pb_ratio=stock_info.get('pb_ratio'),
                    market_cap=stock_info.get('market_cap'),
                    price=None,
                )
            )

    history = _deduplicate(history_raw)[:requested_rows]

    pe_values = [h['pe_ratio'] for h in history if h.get('pe_ratio') is not None]
    pb_values = [h['pb_ratio'] for h in history if h.get('pb_ratio') is not None]

    stats = {}
    if pe_values:
        stats['pe'] = {
            'current': pe_values[0],
            'mean': float(statistics.mean(pe_values)),
            'median': float(statistics.median(pe_values)),
            'min': float(min(pe_values)),
            'max': float(max(pe_values))
        }
    if pb_values:
        stats['pb'] = {
            'current': pb_values[0],
            'mean': float(statistics.mean(pb_values)),
            'median': float(statistics.median(pb_values)),
            'min': float(min(pb_values)),
            'max': float(max(pb_values))
        }

    total_cells = len(history) * 4
    missing_cells = sum(1 for h in history for k in ('pe_ratio', 'pb_ratio', 'market_cap', 'price') if h.get(k) is None)
    completeness_ratio = float((total_cells - missing_cells) / total_cells) if total_cells > 0 else 0.0
    data_quality = {
        'raw_count': len(history_raw),
        'deduplicated_count': len(history),
        'duplicate_removed': max(0, len(history_raw) - len(history)),
        'field_count': 4,
        'missing_cells': missing_cells,
        'invalid_value_cells': int(sum(invalid_value_fields.values())),
        'invalid_value_fields': invalid_value_fields,
        'completeness_ratio': round(completeness_ratio, 4),
        'missing_ratio': round(1.0 - completeness_ratio, 4),
    }

    payload = {
        'code': code,
        'days': days,
        'history': history,
        'stats': stats,
        'count': len(history),
        'source_chain': source_chain,
        'fallback_reason': fallback_reason,
        'data_quality': data_quality,
    }
    if db_query_failed or not rows or len(source_chain) > 1:
        payload['message'] = 'stock_quotes 无历史数据或查询失败，已返回降级结果'
        payload['source'] = 'fallback'

    return ok(payload)
