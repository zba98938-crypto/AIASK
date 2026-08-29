"""基本面分析管理器 - 杜邦分析、同行对比、内在价值（增强版）"""

from typing import Any, Optional
import math
import time
from ...storage import get_db
from ...utils import normalize_code
from ...data_source import data_source
from ..manager_protocol import (
    normalize_manager_payload,
    fail_with_meta,
    normalize_manager_code,
    normalize_manager_kwargs,
    ok_with_meta,
)
from ..valuation import _sanitize_distribution_samples, _run_dcf_distribution
import logging

logger = logging.getLogger(__name__)


def _safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(v):
        return float(default)
    return float(v)


def _latest_value(record: dict, *keys: str, default=None):
    for key in keys:
        if key in record and record.get(key) is not None:
            return record.get(key)
    return default


async def _get_financial_tool_payload(code: str) -> tuple[Optional[dict], list[str]]:
    try:
        from ..finance import get_financials

        result = await get_financials(code)
    except Exception:
        return None, []

    if not result.get("success") or not result.get("data"):
        return None, result.get("source_chain") or ["finance.get_financials"]

    payload = result.get("data")
    if isinstance(payload, dict):
        return dict(payload), result.get("source_chain") or ["finance.get_financials"]
    return None, result.get("source_chain") or ["finance.get_financials"]


def _safe_metric_value(value, default=0):
    if value is None:
        return default
    try:
        import math as _math
        import pandas as pd

        if pd.isna(value):
            return default
        parsed = float(value)
        return default if _math.isnan(parsed) else parsed
    except (ValueError, TypeError):
        return default


def _build_metrics_from_latest(latest: dict) -> dict:
    return {
        "revenue": _safe_metric_value(latest.get("revenue") or latest.get("total_revenue")),
        "net_income": _safe_metric_value(
            latest.get("n_income")
            or latest.get("net_income")
            or latest.get("netProfit")
            or latest.get("net_profit")
        ),
        "eps": _safe_metric_value(latest.get("basic_eps") or latest.get("eps")),
        "pe_ratio": latest.get("pe_ratio"),
        "pb_ratio": latest.get("pb_ratio"),
        "roe": _safe_metric_value(latest.get("roe"), None),
        "debt_ratio": _safe_metric_value(
            latest.get("debt_ratio") or latest.get("debt_to_assets") or latest.get("debtRatio"),
            None,
        ),
        "gross_margin": _safe_metric_value(
            latest.get("gross_margin") or latest.get("grossprofit_margin") or latest.get("grossProfitMargin"),
            None,
        ),
        "revenue_growth": _safe_metric_value(latest.get("revenue_growth") or latest.get("or_yoy"), None),
        "profit_growth": _safe_metric_value(latest.get("profit_growth") or latest.get("netprofit_yoy"), None),
    }


def _has_intrinsic_value_inputs(latest: dict) -> bool:
    metrics = _build_metrics_from_latest(latest or {})
    revenue = _safe_float(metrics.get("revenue"), 0.0)
    net_income = _safe_float(metrics.get("net_income"), 0.0)
    free_cash_flow = _safe_float(_latest_value(latest or {}, "free_cash_flow", "fcf"), 0.0)
    return any(value > 0 for value in (revenue, net_income, free_cash_flow))


def _dedupe_chain(items: list[str]) -> list[str]:
    deduped = []
    seen = set()
    for item in items:
        label = str(item or "").strip()
        if not label or label in seen:
            continue
        deduped.append(label)
        seen.add(label)
    return deduped


def _build_intrinsic_value_payload(code: str, method: str, latest: dict, kwargs: dict) -> dict:
    method_l = str(method or "dcf").lower()

    if method_l == "dcf":
        growth_rate = _safe_float(kwargs.get("growth_rate", 0.10), 0.10)
        discount_rate = _safe_float(kwargs.get("discount_rate", 0.10), 0.10)
        terminal_growth = _safe_float(
            kwargs.get("terminal_growth", kwargs.get("terminal_growth_rate", 0.03)),
            0.03,
        )
        years = max(1, min(20, int(kwargs.get("years", 5) or 5)))
        if discount_rate <= terminal_growth:
            raise ValueError("discount_rate must be greater than terminal_growth")

        net_profit = _safe_float(_latest_value(latest, "net_profit", "n_income", "netProfit"), 0.0)
        revenue = _safe_float(_latest_value(latest, "revenue", "total_revenue"), 0.0)
        fcf_raw = kwargs.get("fcf", _latest_value(latest, "free_cash_flow"))
        fcf = _safe_float(fcf_raw, 0.0)
        if fcf <= 0:
            if net_profit > 0:
                fcf = net_profit * 0.8
            elif revenue > 0:
                fcf = revenue * 0.08
            else:
                raise ValueError("no valid free cash flow input for dcf valuation")

        pv_fcf = 0.0
        for year in range(1, years + 1):
            future_fcf = fcf * ((1 + growth_rate) ** year)
            pv_fcf += future_fcf / ((1 + discount_rate) ** year)
        terminal_fcf = fcf * ((1 + growth_rate) ** years) * (1 + terminal_growth)
        terminal_value = terminal_fcf / (discount_rate - terminal_growth)
        pv_terminal = terminal_value / ((1 + discount_rate) ** years)
        enterprise_value = pv_fcf + pv_terminal

        shares = _safe_float(
            kwargs.get(
                "shares_outstanding",
                _latest_value(latest, "total_shares", "shares_outstanding", default=1_000_000_000),
            ),
            1_000_000_000,
        )
        if shares <= 0:
            shares = 1_000_000_000.0

        payload = {
            "code": code,
            "method": "DCF",
            "intrinsic_value": float(enterprise_value),
            "intrinsic_price_per_share": float(enterprise_value / shares),
            "assumptions": {
                "fcf": float(fcf),
                "growth_rate": f"{growth_rate * 100:.1f}%",
                "discount_rate": f"{discount_rate * 100:.1f}%",
                "terminal_growth": f"{terminal_growth * 100:.1f}%",
                "years": years,
            },
            "components": {
                "pv_fcf": float(pv_fcf),
                "pv_terminal": float(pv_terminal),
                "terminal_value": float(terminal_value),
            },
        }

        if bool(kwargs.get("enable_distribution", False)):
            sample_size = _sanitize_distribution_samples(int(kwargs.get("distribution_samples", 1000) or 1000))
            base_revenue = _safe_float(kwargs.get("base_revenue"), 0.0)
            if base_revenue <= 0:
                base_revenue = max(revenue, fcf / 0.2 if fcf > 0 else 0.0, 1.0)
            profit_margin = _safe_float(kwargs.get("profit_margin"), 0.0)
            if profit_margin <= 0:
                profit_margin = max(min(net_profit / base_revenue, 0.6), 0.01) if net_profit > 0 else 0.15

            payload["valuation_interval"] = _run_dcf_distribution(
                base_revenue=base_revenue,
                years=years,
                tax_rate=max(min(_safe_float(kwargs.get("tax_rate", 0.25), 0.25), 0.45), 0.0),
                capex_ratio=max(min(_safe_float(kwargs.get("capex_ratio", 0.04), 0.04), 0.3), 0.0),
                depreciation_ratio=max(min(_safe_float(kwargs.get("depreciation_ratio", 0.03), 0.03), 0.2), 0.0),
                nwc_ratio=max(min(_safe_float(kwargs.get("nwc_ratio", 0.01), 0.01), 0.1), -0.05),
                growth_rate=growth_rate,
                profit_margin=profit_margin,
                discount_rate=discount_rate,
                terminal_growth_rate=terminal_growth,
                sample_size=sample_size,
                growth_std_ratio=_safe_float(kwargs.get("distribution_growth_std", 0.2), 0.2),
                margin_std_ratio=_safe_float(kwargs.get("distribution_margin_std", 0.15), 0.15),
                discount_std_ratio=_safe_float(kwargs.get("distribution_discount_std", 0.1), 0.1),
                terminal_std_ratio=_safe_float(kwargs.get("distribution_terminal_std", 0.1), 0.1),
                seed=kwargs.get("distribution_seed"),
            )
        return payload

    if method_l == "pe":
        shares = _safe_float(_latest_value(latest, "total_shares", "shares_outstanding"), 1_000_000_000)
        net_profit = _safe_float(_latest_value(latest, "net_profit", "n_income"), 0.0)
        # P2-4.5.12 fix: EPS 内部一致性校验(诊断报告 §4.5.12)
        # 历史问题:5 估值器(PE/PB/DCF/DDM/相对估值)各自重新算 EPS,导致同一公司同一时点 EPS 不一致
        # 修复:优先级 user.eps > financials.basic_eps/eps > 计算值(net_profit/shares)
        eps_user = _safe_float(kwargs.get("eps"), 0.0)
        eps_from_financials = _safe_float(_latest_value(latest, "basic_eps", "eps"), 0.0)
        eps_calc = float(net_profit / shares) if shares > 0 and net_profit > 0 else 0.0
        # 确定使用的 EPS
        if eps_user > 0:
            eps = eps_user
            eps_source = 'user_input'
        elif eps_from_financials > 0:
            eps = eps_from_financials
            eps_source = 'financials.basic_eps'
        elif eps_calc > 0:
            eps = eps_calc
            eps_source = 'computed_net_profit_div_shares'
        else:
            eps = 0.0
            eps_source = 'unavailable'
        industry_pe = _safe_float(kwargs.get("industry_pe", 15), 15.0)
        # 一致性校验(允许 5% 误差)
        consistency_warning = None
        if eps_from_financials > 0 and eps_calc > 0:
            diff_pct = abs(eps_from_financials - eps_calc) / max(eps_from_financials, 1e-9)
            if diff_pct > 0.05:
                consistency_warning = {
                    'code': 'eps_inconsistent_across_sources',
                    'message': f'financials.eps={eps_from_financials:.4f} 与 computed(net_profit/shares)={eps_calc:.4f} 偏差 {diff_pct*100:.1f}%,可能 net_profit 与 shares_outstanding 期数不匹配',
                    'severity': 'warning',
                    'eps_from_financials': eps_from_financials,
                    'eps_computed': eps_calc,
                    'used_eps': eps,
                    'used_eps_source': eps_source,
                }
        result = {
            "code": code,
            "method": "PE",
            "intrinsic_price_per_share": float(eps * industry_pe),
            "eps": float(eps),
            "eps_source": eps_source,
            "industry_pe": float(industry_pe),
        }
        if consistency_warning:
            result['warnings'] = [consistency_warning]
        return result

    if method_l == "pb":
        bvps = _safe_float(
            kwargs.get("bvps", _latest_value(latest, "bvps", "book_value_per_share")),
            0.0,
        )
        industry_pb = _safe_float(kwargs.get("industry_pb", 2.0), 2.0)
        return {
            "code": code,
            "method": "PB",
            "intrinsic_price_per_share": float(bvps * industry_pb),
            "bvps": float(bvps),
            "industry_pb": float(industry_pb),
        }

    raise ValueError(f"unsupported valuation method: {method_l}")


def register_fundamental_analysis_manager(mcp):
    """注册基本面分析管理器工具"""
    
    @mcp.tool()
    async def fundamental_analysis_manager(action: str, params: dict | None = None, kwargs: Any = None, code: str | None = None):
        """基本面分析管理器（统一 action + kwargs 协议）

        Args:
            action (str, required): 操作类型，可选 help/analyze/dupont/compare
            code (str, optional): 股票代码
            kwargs: JSON 字符串或关键字参数，不同 action 所需参数:
                - help: 无需额外参数
                - analyze: code(str, 股票代码)
                - dupont: code(str, 杜邦分析)
                - compare: code(str), peers(list[str], optional, 对比公司列表)

        Returns:
            dict: {"success": bool, "data": {...}, "error": str|None}

        Examples:
            # 查看帮助
            fundamental_analysis_manager(action="help", kwargs="{}")
            # 基本面分析
            fundamental_analysis_manager(action="analyze", code="600519", kwargs="{}")
            # 杜邦分析
            fundamental_analysis_manager(action="dupont", code="600519", kwargs="{}")
            # 同行对比
            fundamental_analysis_manager(action="compare", code="600519", kwargs='{"peers":["000858","002304"]}')
        """
        start_time = time.perf_counter()
        try:
            db = get_db()
            kwargs = normalize_manager_kwargs(
                kwargs,
                field_aliases={
                    "codes": ("Codes", "peers"),
                },
            )
            code, kwargs = normalize_manager_code(code, kwargs)

            def _ok(data: dict, source_chain=None):
                return ok_with_meta(
                    data,
                    tool_name="fundamental_analysis_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )

            def _fail(message: str, source_chain=None):
                return fail_with_meta(
                    message,
                    tool_name="fundamental_analysis_manager",
                    action=action,
                    started_at=start_time,
                    source_chain=source_chain,
                )
            
            if action == 'help':
                return _ok({
                    'supported_actions': {
                        'analyze': '基本面分析（需要 code）',
                        'dupont': '杜邦分析（需要 code）',
                        'compare': '同行对比（需要 code, peers）',
                        'intrinsic_value': '内在价值估算（需要 code，可选 method=dcf/pe/pb）',
                        'help': '显示帮助信息',
                    }
                }, source_chain=['fundamental_analysis_manager'])
            
            elif action == 'analyze' and code:
                code = normalize_code(code)
                source_chain = ['fundamental_analysis_manager', 'db.get_financials']

                # 1. 尝试从 DB 获取财务数据
                financials = await db.get_financials(code, limit=4)
                
                # 2. DB 无数据时从 Tushare 组合报表获取
                if not financials:
                    logger.info(f"[FundamentalManager] Fetching financials for {code}")
                    source_chain = ['fundamental_analysis_manager', 'tushare.income', 'tushare.fina_indicator']
                    
                    # 尝试 Tushare Pro — 同时获取利润表和财务指标
                    ts_pro = data_source.get_tushare_pro()
                    if ts_pro:
                        try:
                            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                            df_income = ts_pro.income(ts_code=ts_code, fields='end_date,revenue,n_income,basic_eps,total_revenue')
                            df_fina = None
                            try:
                                df_fina = ts_pro.fina_indicator(ts_code=ts_code, fields='end_date,roe,debt_to_assets,grossprofit_margin,netprofit_yoy,or_yoy')
                            except Exception:
                                pass
                            
                            df_balance = None
                            try:
                                df_balance = ts_pro.balancesheet(ts_code=ts_code, fields='end_date,total_liab,total_assets')
                            except Exception:
                                pass
                            
                            if df_income is not None and not df_income.empty:
                                records = df_income.head(4).to_dict('records')
                                # 清理 pandas NaN 值
                                import pandas as pd
                                for rec in records:
                                    for k, v in list(rec.items()):
                                        try:
                                            if pd.isna(v):
                                                rec[k] = None
                                        except (TypeError, ValueError):
                                            pass
                                if df_fina is not None and not df_fina.empty:
                                    fina_map = {}
                                    for _, row in df_fina.iterrows():
                                        ed = str(row.get('end_date', ''))
                                        fina_map[ed] = row.to_dict()
                                    for rec in records:
                                        ed = str(rec.get('end_date', ''))
                                        fina = fina_map.get(ed)
                                        if not fina and ed:
                                            year_prefix = ed[:4]
                                            candidates = [(k, v) for k, v in fina_map.items() if k.startswith(year_prefix)]
                                            if candidates:
                                                candidates.sort(key=lambda x: x[0], reverse=True)
                                                fina = candidates[0][1]
                                            else:
                                                all_fina = sorted(fina_map.items(), key=lambda x: x[0], reverse=True)
                                                if all_fina:
                                                    fina = all_fina[0][1]
                                        if fina:
                                            rec['roe'] = rec.get('roe') or fina.get('roe')
                                            rec['debt_ratio'] = rec.get('debt_ratio') or fina.get('debt_to_assets')
                                            rec['gross_margin'] = rec.get('gross_margin') or fina.get('grossprofit_margin')
                                            rec['revenue_growth'] = rec.get('revenue_growth') or fina.get('or_yoy')
                                            rec['profit_growth'] = rec.get('profit_growth') or fina.get('netprofit_yoy')
                                
                                if df_fina is not None and not df_fina.empty and records:
                                    latest_fina = df_fina.iloc[0].to_dict()
                                    for rec in records:
                                        if rec.get('roe') is None:
                                            rec['roe'] = latest_fina.get('roe')
                                        if rec.get('debt_ratio') is None:
                                            rec['debt_ratio'] = latest_fina.get('debt_to_assets')
                                        if rec.get('gross_margin') is None:
                                            rec['gross_margin'] = latest_fina.get('grossprofit_margin')
                                        if rec.get('revenue_growth') is None:
                                            rec['revenue_growth'] = latest_fina.get('or_yoy')
                                        if rec.get('profit_growth') is None:
                                            rec['profit_growth'] = latest_fina.get('netprofit_yoy')
                                
                                if df_balance is not None and not df_balance.empty:
                                    balance_map = {}
                                    for _, row in df_balance.iterrows():
                                        ed = str(row.get('end_date', ''))
                                        total_liab = row.get('total_liab')
                                        total_assets = row.get('total_assets')
                                        if total_liab is not None and total_assets is not None and float(total_assets or 0) > 0:
                                            balance_map[ed] = float(total_liab) / float(total_assets) * 100
                                    for rec in records:
                                        ed = str(rec.get('end_date', ''))
                                        if rec.get('debt_ratio') is None and ed in balance_map:
                                            rec['debt_ratio'] = balance_map[ed]
                                
                                financials = records
                        except Exception as e:
                            logger.warning(f"[FundamentalManager] Tushare failed: {e}")
                
                # 3. 当前运行环境下，降级到公共财务工具
                if not financials:
                    tool_payload, tool_chain = await _get_financial_tool_payload(code)
                    if tool_payload:
                        financials = [tool_payload]
                        source_chain = ['fundamental_analysis_manager'] + list(tool_chain or ['finance.get_financials'])
                
                if not financials:
                    return _fail(f'无法获取 {code} 的财务数据', source_chain=source_chain)
                
                metrics = {}
                if financials and isinstance(financials[0], dict):
                    latest = financials[0]
                    metrics = _build_metrics_from_latest(latest)
                
                if not metrics.get('revenue') and not metrics.get('net_income'):
                    fin_data, tool_chain = await _get_financial_tool_payload(code)
                    if fin_data:
                        metrics['revenue'] = metrics.get('revenue') or fin_data.get('revenue') or 0
                        metrics['net_income'] = metrics.get('net_income') or fin_data.get('netProfit') or fin_data.get('net_income') or 0
                        metrics['eps'] = metrics.get('eps') or fin_data.get('eps') or 0
                        metrics['pe_ratio'] = metrics.get('pe_ratio') or fin_data.get('pe_ratio')
                        metrics['roe'] = fin_data.get('roe')
                        metrics['source'] = fin_data.get('source', 'finance_tool')
                        source_chain = _dedupe_chain(source_chain + ['finance.get_financials'] + list(tool_chain or []))

                # P1-3.4 fix: 同步 join db.stocks 拿 PE/PB(诊断报告 §3.4)
                # 历史问题:fundamental_analysis_manager.analyze 返回 pe_ratio/pb_ratio=null
                # 而 build_stock_context.valuation 有 pe=19.91,manager 与 db 脱节
                if metrics.get('pe_ratio') is None or metrics.get('pb_ratio') is None:
                    try:
                        async with db.acquire() as conn:
                            stocks_row = await conn.fetchrow(
                                """SELECT pe_ratio, pb_ratio, market_cap
                                   FROM stocks WHERE code = $1""",
                                code,
                            )
                        if stocks_row:
                            db_pe = stocks_row.get('pe_ratio')
                            db_pb = stocks_row.get('pb_ratio')
                            db_mcap = stocks_row.get('market_cap')
                            if metrics.get('pe_ratio') is None and db_pe is not None and float(db_pe) > 0:
                                metrics['pe_ratio'] = float(db_pe)
                                metrics['pe_calculation_method'] = 'daily_snapshot'
                                metrics['pe_source'] = 'db.stocks'
                            if metrics.get('pb_ratio') is None and db_pb is not None and float(db_pb) > 0:
                                metrics['pb_ratio'] = float(db_pb)
                                metrics['pb_source'] = 'db.stocks'
                            if metrics.get('market_cap') is None and db_mcap is not None and float(db_mcap) > 0:
                                metrics['market_cap'] = float(db_mcap)
                                metrics['market_cap_source'] = 'db.stocks'
                            source_chain = _dedupe_chain(source_chain + ['db.stocks'])
                    except Exception:
                        pass
                
                return _ok({
                    'code': code,
                    'financials': financials,
                    'metrics': metrics,
                    'data_points': len(financials)
                }, source_chain=source_chain)
            
            elif action == 'dupont':
                if not code:
                    return _fail('需要提供股票代码', source_chain=['fundamental_analysis_manager'])
                
                code = normalize_code(code)
                source_chain = ['fundamental_analysis_manager', 'db.get_financials']
                financials = await db.get_financials(code, limit=1)
                if not financials:
                    fin_data, tool_chain = await _get_financial_tool_payload(code)
                    if fin_data:
                        financials = [fin_data]
                        source_chain = ['fundamental_analysis_manager'] + list(tool_chain or ['finance.get_financials'])
                if not financials:
                    return _fail('无财务数据', source_chain=source_chain)
                
                latest = financials[0]
                roe = _safe_float(latest.get('roe'), 0.0)
                net_margin = _safe_float(
                    latest.get('netprofit_margin')
                    or latest.get('net_margin')
                    or latest.get('netProfitMargin'),
                    0.0,
                )
                asset_turnover = _safe_float(
                    latest.get('asset_turnover')
                    or latest.get('assets_turn')
                    or latest.get('assetTurnover'),
                    0.0,
                )
                equity_multiplier = _safe_float(
                    latest.get('equity_multiplier')
                    or latest.get('equityMultiplier'),
                    0.0,
                )
                
                return _ok({
                    'code': code,
                    'roe': float(roe),
                    'components': {
                        'net_margin': float(net_margin),
                        'asset_turnover': float(asset_turnover),
                        'equity_multiplier': float(equity_multiplier),
                    },
                    'analysis': '杜邦分析'
                }, source_chain=source_chain)
            
            elif action == 'compare':
                codes = kwargs.get('codes', [])
                if isinstance(codes, str):
                    codes = [item.strip() for item in codes.split(',') if item.strip()]
                elif not isinstance(codes, list):
                    codes = []
                if code:
                    codes = [code] + codes
                codes = [normalize_code(item) for item in codes if str(item or '').strip()]
                deduped_codes = []
                seen_codes = set()
                for item in codes:
                    if item in seen_codes:
                        continue
                    deduped_codes.append(item)
                    seen_codes.add(item)
                codes = deduped_codes
                if not codes:
                    return _fail('需要提供对比股票代码（codes / peers）', source_chain=['fundamental_analysis_manager'])
                
                comparison = []
                child_source_chain = ['fundamental_analysis_manager', 'fundamental_analysis_manager.analyze']
                for c in codes[:5]:
                    result = await fundamental_analysis_manager('analyze', code=c)
                    if result.get('success'):
                        comparison.append({
                            'code': c,
                            'metrics': result['data']['metrics'],
                            'meta': result.get('meta'),
                        })
                        child_source_chain.extend(result.get('meta', {}).get('source_chain') or [])
                
                return _ok(
                    {
                        'comparison': comparison,
                        'requested_codes': codes[:5],
                    },
                    source_chain=_dedupe_chain(child_source_chain),
                )
            
            elif action == 'intrinsic_value':
                if not code:
                    return _fail('code is required', source_chain=['fundamental_analysis_manager'])

                code = normalize_code(code)
                method = str(kwargs.get('method', 'dcf')).lower()
                source_chain = ['fundamental_analysis_manager', 'db.get_financials']
                financials = await db.get_financials(code, limit=4)
                if not financials:
                    stock_info = await db.get_stock_info(code)
                    if stock_info:
                        financials = [dict(stock_info)]
                        source_chain = ['fundamental_analysis_manager', 'db.get_stock_info']
                if not financials or not _has_intrinsic_value_inputs(financials[0]):
                    fin_data, tool_chain = await _get_financial_tool_payload(code)
                    if fin_data:
                        if financials:
                            merged = dict(fin_data)
                            merged.update({k: v for k, v in dict(financials[0]).items() if v not in (None, "", [], {})})
                            financials = [merged]
                        else:
                            financials = [fin_data]
                        source_chain = ['fundamental_analysis_manager'] + list(tool_chain or ['finance.get_financials'])

                if not financials:
                    return _fail(f'no financial data for {code}', source_chain=source_chain)

                payload = _build_intrinsic_value_payload(code, method, financials[0], kwargs)
                return _ok(payload, source_chain=source_chain)
            
            else:
                return _fail(
                    f'Unknown action: {action}. Supported: help, analyze, dupont, compare, intrinsic_value',
                    source_chain=['fundamental_analysis_manager'],
                )
        
        except Exception as e:
            logger.error(f"[FundamentalManager] Error: {e}")
            return fail_with_meta(
                str(e),
                tool_name='fundamental_analysis_manager',
                action=action,
                started_at=start_time,
                source_chain=['fundamental_analysis_manager'],
            )
