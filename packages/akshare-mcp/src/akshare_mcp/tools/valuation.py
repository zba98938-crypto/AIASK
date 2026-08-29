"""估值工具 — MCP tool 注册入口

@mcp.tool() 定义 + register(mcp) 函数。
纯计算逻辑见 valuation_engine，相对/历史估值见 valuation_peer。
"""

from typing import Optional, List
import statistics

from ..storage import get_db
from ..utils import ok, fail, resolve_existing_security_code_async

# --- 从 engine 子模块导入纯计算函数 & 常量 ---
from .valuation_engine import (
    _clamp,
    _cost_of_equity_capm,
    _compute_wacc,
    _build_driver_fcf_projection,
    _present_value_from_projection,
    _run_sensitivity,
    _sanitize_distribution_samples,
    _run_dcf_distribution,
    _scenario_dcf,
    DEFAULT_SENSITIVITY_SHOCKS,
    INDUSTRY_TEMPLATES,
)

# --- 从 peer 子模块导入相对/历史估值实现 ---
from . import valuation_peer as valuation_peer_mod
from .valuation_peer import (
    _relative_valuation_impl,
    _get_historical_valuation_impl,
)


def register(mcp):
    """注册估值工具"""

    async def _run_valuation_peer_impl(func, *args, **kwargs):
        """确保 valuation.get_db 的 monkeypatch 能透传到 valuation_peer 子模块。"""
        original_get_db = getattr(valuation_peer_mod, 'get_db', None)
        valuation_peer_mod.get_db = get_db
        try:
            return await func(*args, **kwargs)
        finally:
            if original_get_db is not None:
                valuation_peer_mod.get_db = original_get_db

    async def _resolve_existing_valuation_code(
        code: Optional[str] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[dict]]:
        resolved_code, _, error = await resolve_existing_security_code_async(
            code=code,
            stock_code=stock_code,
            symbol=symbol,
            ticker=ticker,
        )
        if error:
            return None, fail(error)
        return resolved_code, None

    @mcp.tool()
    async def get_valuation_metrics(
        code: Optional[str] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        获取估值指标

        Args:
            code: 股票代码
        """
        try:
            code, error_response = await _resolve_existing_valuation_code(code, stock_code, symbol, ticker)
            if error_response is not None:
                return error_response

            db = get_db()
            pe = pb = mcap = None
            name = ""
            source_chain = ['db.stocks']
            invalid_metrics: dict[str, list[dict[str, object]]] = {}
            fallback_used = False

            def _record_invalid(metric_name: str, source_name: str, raw_value, reason: str) -> None:
                invalid_metrics.setdefault(metric_name, []).append(
                    {
                        'source': source_name,
                        'reason': reason,
                        'raw_value': raw_value,
                    }
                )

            def _sanitize_positive_metric(raw_value, *, metric_name: str, source_name: str):
                try:
                    if raw_value is None:
                        return None
                    numeric = float(raw_value)
                except Exception:
                    _record_invalid(metric_name, source_name, raw_value, 'unparseable')
                    return None
                if numeric <= 0:
                    _record_invalid(metric_name, source_name, numeric, 'non_positive')
                    return None
                return float(numeric)

            # 从stocks表获取估值指标
            async with db.acquire() as conn:
                code_col = await db._stocks_code_column(conn) if hasattr(db, "_stocks_code_column") else "code"
                row = await conn.fetchrow(
                    f"""SELECT {code_col} AS code, stock_name, pe_ratio, pb_ratio, market_cap
                       FROM stocks
                       WHERE {code_col} = $1""",
                    code
                )
                if row:
                    name = row['stock_name'] or ""
                    pe = _sanitize_positive_metric(row['pe_ratio'], metric_name='pe_ratio', source_name='db.stocks')
                    pb = _sanitize_positive_metric(row['pb_ratio'], metric_name='pb_ratio', source_name='db.stocks')
                    mcap = _sanitize_positive_metric(row['market_cap'], metric_name='market_cap', source_name='db.stocks')

            # DB 值为空时，尝试从 DB 股票信息接口补齐估值
            if pe is None or pb is None or mcap is None:
                source_chain.append('db.get_stock_info')
                live = await db.get_stock_info(code)
                if live:
                    fallback_used = True
                    name = name or live.get("name", "")
                    pe = pe if pe is not None else _sanitize_positive_metric(
                        live.get("pe_ratio"),
                        metric_name='pe_ratio',
                        source_name='db.get_stock_info',
                    )
                    pb = pb if pb is not None else _sanitize_positive_metric(
                        live.get("pb_ratio"),
                        metric_name='pb_ratio',
                        source_name='db.get_stock_info',
                    )
                    mcap = mcap if mcap is not None else _sanitize_positive_metric(
                        live.get("market_cap"),
                        metric_name='market_cap',
                        source_name='db.get_stock_info',
                    )

            if not name and pe is None and pb is None and mcap is None:
                response = fail('Stock not found')
                response['source_chain'] = source_chain
                response['invalid_metrics'] = invalid_metrics
                return response

            metrics_payload = {
                'code': code,
                'name': name,
                'pe_ratio': round(pe, 2) if pe is not None else None,
                'pb_ratio': round(pb, 2) if pb is not None else None,
                'market_cap': round(mcap, 2) if mcap is not None else None,
                'data_quality': {
                    'source_chain': source_chain,
                    'fallback_used': fallback_used,
                    'invalid_metrics': invalid_metrics,
                    'missing_metrics': [
                        metric_name
                        for metric_name, metric_value in (
                            ('pe_ratio', pe),
                            ('pb_ratio', pb),
                            ('market_cap', mcap),
                        )
                        if metric_value is None
                    ],
                },
            }
            return ok(metrics_payload)

        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def dcf_valuation(
        code: Optional[str] = None,
        discount_rate: float = 0.10,
        growth_rate: float = 0.05,
        years: int = 5,
        risk_free_rate: float = 0.03,
        beta: float = 1.0,
        market_risk_premium: float = 0.06,
        cost_of_debt: float = 0.05,
        tax_rate: float = 0.25,
        equity_weight: float = 0.7,
        debt_weight: float = 0.3,
        terminal_growth_rate: Optional[float] = None,
        capex_ratio: float = 0.04,
        depreciation_ratio: float = 0.03,
        nwc_ratio: float = 0.01,
        enable_sensitivity: bool = True,
        enable_distribution: bool = False,
        distribution_samples: int = 1000,
        distribution_growth_std: float = 0.2,
        distribution_margin_std: float = 0.15,
        distribution_discount_std: float = 0.1,
        distribution_terminal_std: float = 0.1,
        distribution_seed: Optional[int] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        DCF估值（现金流折现，驱动项版本）

        已知限制与降级策略：
        - 首选数据库财务数据，若缺失则降级到 finance.get_financials。
        - 当"最新期净利润<=0/缺失"时，不再直接失败，改为使用历史正净利润均值/单值降级估算。
        - 返回中新增 source_chain / fallback_reason / profit_basis 审计字段，不影响既有字段兼容性。

        Args:
            code: 股票代码
            discount_rate: 折现率（兼容参数；不传时由WACC估算）
            growth_rate: 显性预测期增长率
            years: 预测年数
            risk_free_rate: 无风险利率（CAPM）
            beta: Beta（CAPM）
            market_risk_premium: 市场风险溢价（CAPM）
            cost_of_debt: 税前债务成本
            tax_rate: 所得税率
            equity_weight: 权益资本权重
            debt_weight: 债务资本权重
            terminal_growth_rate: 永续增长率（缺省沿用growth_rate）
            capex_ratio: CapEx/Revenue
            depreciation_ratio: Depreciation/Revenue
            nwc_ratio: ΔNWC/Revenue
            enable_sensitivity: 是否执行敏感性分析
        """
        try:
            code, error_response = await _resolve_existing_valuation_code(code, stock_code, symbol, ticker)
            if error_response is not None:
                return error_response
            if years < 1:
                return fail('years 必须 >= 1')

            source_chain: list[str] = []
            fallback_reason: list[str] = []
            term_g = growth_rate if terminal_growth_rate is None else terminal_growth_rate

            # 参数边界保护
            tax_rate = _clamp(float(tax_rate), 0.0, 0.45)
            capex_ratio = _clamp(float(capex_ratio), 0.0, 0.3)
            depreciation_ratio = _clamp(float(depreciation_ratio), 0.0, 0.2)
            nwc_ratio = _clamp(float(nwc_ratio), -0.05, 0.1)

            db = get_db()
            source_chain.append('db.get_financials')
            financials = await db.get_financials(code, limit=max(8, years * 2))
            if isinstance(financials, dict):
                financials = [financials]
            if not isinstance(financials, list):
                financials = []

            if not financials:
                # DB 无数据时回退到 finance API
                try:
                    source_chain.append('finance.get_financials')
                    from .finance import get_financials as _api_get_financials
                    fin_res = await _api_get_financials(code)
                    if fin_res and fin_res.get('success') and fin_res.get('data'):
                        api_data = fin_res['data']
                        financials = api_data if isinstance(api_data, list) else [api_data]
                        fallback_reason.append('DB无财务数据，已降级使用 finance.get_financials')
                except Exception as e:
                    fallback_reason.append(f'finance.get_financials 降级失败: {e}')

            if not financials:
                return fail('No financial data for DCF')

            def _to_float(v):
                try:
                    if v is None:
                        return None
                    return float(v)
                except Exception:
                    return None

            def _first_number(row: dict, keys: list[str]):
                if not isinstance(row, dict):
                    return None
                for k in keys:
                    val = _to_float(row.get(k))
                    if val is not None:
                        return val
                return None

            def _first_positive(row: dict, keys: list[str]):
                val = _first_number(row, keys)
                return val if val is not None and val > 0 else None

            profit_keys = [
                'net_profit', 'netProfit', 'net_income', 'n_income', 'profit',
                'n_income_attr_p', 'net_profit_atsopc', 'parent_net_profit',
                '归母净利润', '净利润',
            ]
            revenue_keys = [
                'revenue', 'total_revenue', 'totalRevenue', 'operating_revenue', 'operatingRevenue',
                'oper_rev', 'main_business_income', '营业总收入', '营业收入',
            ]

            latest_row = financials[0] if financials else None
            latest_profit = _first_positive(latest_row, profit_keys) if latest_row else None

            revenue_candidates = [
                _first_positive(f, revenue_keys)
                for f in financials
            ]
            revenue_candidates = [v for v in revenue_candidates if v is not None]

            if revenue_candidates:
                base_revenue = float(revenue_candidates[0])
            else:
                # 营收缺失降级：优先使用"历史正净利润 / 估算利润率"反推基准营收
                profit_candidates = [
                    _first_positive(f, profit_keys)
                    for f in financials
                ]
                profit_candidates = [float(v) for v in profit_candidates if v is not None]
                if profit_candidates:
                    fallback_profit = float(statistics.mean(profit_candidates[: min(3, len(profit_candidates))]))
                    inferred_margin = 0.20
                    base_revenue = float(max(fallback_profit / inferred_margin, fallback_profit * 2))
                    fallback_reason.append('缺少有效营收数据，已使用净利润反推基准营收（降级估算）')
                else:
                    # 最后兜底：完全缺少营收/利润时提供保守默认基准，避免工具直接失败
                    base_revenue = 1_000_000_000.0
                    fallback_reason.append('缺少有效营收与净利润数据，已使用默认基准营收进行保守估算')

            profit_basis = {
                'strategy': 'latest_positive_net_profit',
                'sample_count': 1,
                'raw_values': [],
            }

            if latest_profit is not None:
                net_profit = float(latest_profit)
            else:
                history_positive = [
                    _first_positive(f, profit_keys)
                    for f in financials
                ]
                history_positive = [float(v) for v in history_positive if v is not None]
                if not history_positive:
                    net_profit = float(base_revenue * 0.15)
                    profit_basis['strategy'] = 'default_margin_estimate'
                    profit_basis['sample_count'] = 0
                    profit_basis['raw_values'] = []
                    fallback_reason.append('缺少有效净利润数据，已使用默认利润率15%估算')
                elif len(history_positive) >= 2:
                    net_profit = float(statistics.mean(history_positive))
                    profit_basis['strategy'] = 'historical_positive_mean'
                    profit_basis['sample_count'] = len(history_positive)
                    profit_basis['raw_values'] = history_positive[:8]
                    fallback_reason.append('最新期无有效正净利润，已降级使用历史正净利润估算')
                else:
                    net_profit = float(history_positive[0])
                    profit_basis['strategy'] = 'historical_single_positive'
                    profit_basis['sample_count'] = len(history_positive)
                    profit_basis['raw_values'] = history_positive[:8]
                    fallback_reason.append('最新期无有效正净利润，已降级使用历史单期正净利润估算')

            profit_basis['value'] = float(net_profit)
            profit_margin = _clamp(net_profit / base_revenue, 0.02, 0.5)


            cost_of_equity = _cost_of_equity_capm(risk_free_rate, beta, market_risk_premium)
            wacc_info = _compute_wacc(
                equity_weight=equity_weight,
                debt_weight=debt_weight,
                cost_of_equity=cost_of_equity,
                cost_of_debt=cost_of_debt,
                tax_rate=tax_rate,
            )
            implied_wacc = wacc_info['wacc']

            # 兼容旧接口：discount_rate 仍可显式覆盖
            effective_discount_rate = float(discount_rate) if discount_rate and discount_rate > 0 else implied_wacc
            if effective_discount_rate <= term_g:
                return fail('discount_rate/WACC 必须大于 terminal_growth_rate，避免终值分母<=0')

            projection = _build_driver_fcf_projection(
                base_revenue=base_revenue,
                growth_rate=growth_rate,
                years=years,
                profit_margin=profit_margin,
                tax_rate=tax_rate,
                capex_ratio=capex_ratio,
                depreciation_ratio=depreciation_ratio,
                nwc_ratio=nwc_ratio,
            )
            valuation_core = _present_value_from_projection(
                projection,
                discount_rate=effective_discount_rate,
                terminal_growth_rate=term_g,
            )

            sensitivity = []
            if enable_sensitivity:
                sensitivity = _run_sensitivity(
                    base_revenue=base_revenue,
                    years=years,
                    profit_margin=profit_margin,
                    tax_rate=tax_rate,
                    capex_ratio=capex_ratio,
                    depreciation_ratio=depreciation_ratio,
                    nwc_ratio=nwc_ratio,
                    growth_rate=growth_rate,
                    discount_rate=effective_discount_rate,
                    terminal_growth_rate=term_g,
                    growth_shocks=DEFAULT_SENSITIVITY_SHOCKS,
                    discount_shocks=DEFAULT_SENSITIVITY_SHOCKS,
                    terminal_shocks=DEFAULT_SENSITIVITY_SHOCKS,
                )

            valuation_interval = None
            if enable_distribution:
                sample_size = _sanitize_distribution_samples(distribution_samples)
                valuation_interval = _run_dcf_distribution(
                    base_revenue=base_revenue,
                    years=years,
                    tax_rate=tax_rate,
                    capex_ratio=capex_ratio,
                    depreciation_ratio=depreciation_ratio,
                    nwc_ratio=nwc_ratio,
                    growth_rate=growth_rate,
                    profit_margin=profit_margin,
                    discount_rate=effective_discount_rate,
                    terminal_growth_rate=term_g,
                    sample_size=sample_size,
                    growth_std_ratio=distribution_growth_std,
                    margin_std_ratio=distribution_margin_std,
                    discount_std_ratio=distribution_discount_std,
                    terminal_std_ratio=distribution_terminal_std,
                    seed=distribution_seed,
                )

            selected_report_date = None
            if isinstance(latest_row, dict):
                selected_report_date = latest_row.get('report_date') or latest_row.get('reportDate')

            payload = {
                'code': code,
                'intrinsic_value': float(valuation_core['intrinsic_value']),
                'discount_rate': float(effective_discount_rate),
                'growth_rate': float(growth_rate),
                'terminal_growth_rate': float(term_g),
                'years': int(years),
                'financial_report_date': selected_report_date,
                'model': 'Driver DCF with WACC',
                'wacc_breakdown': wacc_info,
                'driver_assumptions': {
                    'base_revenue': float(base_revenue),
                    'profit_margin': float(profit_margin),
                    'capex_ratio': float(capex_ratio),
                    'depreciation_ratio': float(depreciation_ratio),
                    'nwc_ratio': float(nwc_ratio),
                },
                'projection': valuation_core['projection'],
                'pv_sum': float(valuation_core['pv_sum']),
                'pv_terminal': float(valuation_core['pv_terminal']),
                'terminal_value': float(valuation_core['terminal_value']),
                'sensitivity': sensitivity,
                'source_chain': source_chain,
                'fallback_reason': fallback_reason,
                'profit_basis': profit_basis,
                'meta': {
                    'trace': 'dcf_driver_v2',
                    'compatibility_mode': 'legacy_signature_plus_extensions',
                    'used_discount_source': 'input_discount_rate' if discount_rate and discount_rate > 0 else 'wacc',
                    'distribution_enabled': bool(enable_distribution),
                    'profit_basis_strategy': profit_basis.get('strategy'),
                }
            }
            if valuation_interval is not None:
                payload['valuation_interval'] = valuation_interval

            return ok(payload)

        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def ddm_valuation(
        code: Optional[str] = None,
        dividend: Optional[float] = None,
        growth_rate: float = 0.05,
        required_return: float = 0.10,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        DDM估值（股息折现模型）

        Args:
            code: 股票代码
            dividend: 每股股息（不填则从数据估算）
            growth_rate: 股息增长率
            required_return: 要求回报率
        """
        try:
            code, error_response = await _resolve_existing_valuation_code(code, stock_code, symbol, ticker)
            if error_response is not None:
                return error_response
            if growth_rate >= required_return:
                return fail('增长率必须小于要求回报率')

            db = get_db()
            source_chain = ['db.financials']
            fallback_reason: list[str] = []

            # 获取股息数据（如果没有提供）
            if not dividend:
                # 尝试从财务数据估算股息
                async with db.acquire() as conn:
                    f_code_col = await db._financials_code_column(conn)
                    row = await conn.fetchrow(
                        f"""SELECT eps FROM financials
                           WHERE {f_code_col} = $1
                           ORDER BY report_date DESC
                           LIMIT 1""",
                        code
                    )

                    if row and row['eps'] is not None and float(row['eps']) > 0:
                        # 假设分红率为30%
                        dividend = float(row['eps']) * 0.3
                    else:
                        fallback_reason.append('DB eps 缺失或非正值')

                if not dividend or dividend <= 0:
                    source_chain.append('finance.get_financials')
                    from .finance import get_financials as _api_get_financials
                    from .finance import normalize_financial_payload as _normalize_financial_payload

                    fin_res = await _api_get_financials(code)
                    if fin_res and fin_res.get('success') and fin_res.get('data'):
                        fin_data = _normalize_financial_payload(fin_res['data'], include_aliases=False) or {}
                        eps = fin_data.get('eps')
                        if eps is not None and float(eps) > 0:
                            dividend = float(eps) * 0.3
                            fallback_reason.append('已降级使用 finance.get_financials 的 eps 估算股息')

            if not dividend or dividend <= 0:
                return fail(f'股票 {code} 无股息数据，DDM模型不适用')

            # Gordon Growth Model: P = D1 / (r - g)
            next_dividend = dividend * (1 + growth_rate)
            intrinsic_value = next_dividend / (required_return - growth_rate)

            return ok({
                'code': code,
                'model': 'Gordon Growth Model (DDM)',
                'intrinsic_value': float(intrinsic_value),
                'current_dividend': dividend,
                'next_dividend': float(next_dividend),
                'growth_rate': growth_rate,
                'required_return': required_return,
                'source_chain': source_chain,
                'fallback_reason': fallback_reason,
            })

        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def relative_valuation(
        code: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        peers: Optional[List[str]] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        相对估值分析

        Args:
            code: 目标股票代码
            metrics: 估值指标列表，如['pe_ratio', 'pb_ratio', 'ps_ratio']
            peers: 可比公司列表（不填则自动查找同行业公司）
        """
        try:
            code, error_response = await _resolve_existing_valuation_code(code, stock_code, symbol, ticker)
            if error_response is not None:
                return error_response
            return await _run_valuation_peer_impl(_relative_valuation_impl, code, metrics, peers)
        except Exception as e:
            return fail(str(e))

    @mcp.tool()
    async def get_historical_valuation(
        code: Optional[str] = None,
        days: int = 30,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        获取历史估值数据

        已知限制与降级策略：
        - 首选 stock_quotes 历史库；无数据时按 Tushare -> AkShare -> Baostock 依次降级。
        - 不同源字段可用性不同，可能出现仅价格可用、估值字段缺失。
        - 返回新增 data_quality/source_chain/fallback_reason，便于审计与质量评估。

        Args:
            code: 股票代码
            days: 查询天数
        """
        try:
            code, error_response = await _resolve_existing_valuation_code(code, stock_code, symbol, ticker)
            if error_response is not None:
                return error_response
            return await _run_valuation_peer_impl(_get_historical_valuation_impl, code, days)
        except Exception as e:
            return fail(str(e))

    # ------------------------------------------------------------------
    # P0-D: 行业模板查询 & 多情景 DCF
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_industry_templates():
        """
        列出所有可用的行业参数模板

        返回各行业 DCF 默认参数（增长率、利润率、资本开支等），
        可直接用于 scenario_dcf_valuation 的 industry 参数。
        """
        return ok({
            "templates": {
                k: {**v} for k, v in INDUSTRY_TEMPLATES.items()
            },
            "available_industries": list(INDUSTRY_TEMPLATES.keys()),
        })

    @mcp.tool()
    async def scenario_dcf_valuation(
        code: Optional[str] = None,
        base_revenue: float = 0.0,
        industry: Optional[str] = None,
        years: int = 5,
        tax_rate: float = 0.25,
        risk_free_rate: float = 0.028,
        market_risk_premium: float = 0.06,
        shares_outstanding: Optional[float] = None,
        bull_probability: float = 0.25,
        base_probability: float = 0.50,
        bear_probability: float = 0.25,
        bull_growth_premium: float = 0.05,
        bear_growth_discount: float = 0.05,
        bull_margin_premium: float = 0.03,
        bear_margin_discount: float = 0.03,
        custom_scenarios: Optional[List[dict]] = None,
        growth_rate: Optional[float] = None,
        profit_margin: Optional[float] = None,
        capex_ratio: Optional[float] = None,
        depreciation_ratio: Optional[float] = None,
        nwc_ratio: Optional[float] = None,
        beta: Optional[float] = None,
        equity_weight: Optional[float] = None,
        debt_weight: Optional[float] = None,
        cost_of_debt: Optional[float] = None,
        terminal_growth: Optional[float] = None,
        enable_distribution: bool = False,
        distribution_samples: int = 1000,
        distribution_growth_std: float = 0.2,
        distribution_margin_std: float = 0.15,
        distribution_discount_std: float = 0.1,
        distribution_terminal_std: float = 0.1,
        distribution_seed: Optional[int] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
    ):
        """
        多情景概率加权 DCF 估值（支持行业模板）

        自动生成 Base / Bull / Bear 三情景并概率加权，
        也可通过 custom_scenarios 完全自定义情景。

        Args:
            code: 股票代码
            base_revenue: 基准年营收（元）
            industry: 行业模板名称（银行/制造/科技/消费/医药），不填则需手动指定参数
            years: 预测年数（默认5）
            tax_rate: 企业所得税率
            risk_free_rate: 无风险利率
            market_risk_premium: 市场风险溢价
            shares_outstanding: 总股本（填入后返回每股价值）
            bull_probability: 乐观情景概率（默认0.25）
            base_probability: 基准情景概率（默认0.50）
            bear_probability: 悲观情景概率（默认0.25）
            bull_growth_premium: 乐观情景增长率上浮
            bear_growth_discount: 悲观情景增长率下浮
            bull_margin_premium: 乐观情景利润率上浮
            bear_margin_discount: 悲观情景利润率下浮
            custom_scenarios: 完全自定义情景列表（提供后忽略自动生成）
            growth_rate: 覆盖行业模板的增长率
            profit_margin: 覆盖行业模板的利润率
            capex_ratio: 覆盖行业模板的资本开支率
            depreciation_ratio: 覆盖行业模板的折旧率
            nwc_ratio: 覆盖行业模板的净营运资本变动率
            beta: 覆盖行业模板的 Beta
            equity_weight: 覆盖行业模板的权益占比
            debt_weight: 覆盖行业模板的债务占比
            cost_of_debt: 覆盖行业模板的债务成本
            terminal_growth: 覆盖行业模板的永续增长率
        """
        try:
            code, error_response = await _resolve_existing_valuation_code(code, stock_code, symbol, ticker)
            if error_response is not None:
                return error_response
            source_chain: list[str] = []
            fallback_reason: list[str] = []
            base_revenue_source = 'user_input' if base_revenue and base_revenue > 0 else 'auto'

            def _to_float(v):
                try:
                    if v is None:
                        return None
                    return float(v)
                except Exception:
                    return None

            def _first_number(row: dict, keys: list[str]):
                if not isinstance(row, dict):
                    return None
                for k in keys:
                    val = _to_float(row.get(k))
                    if val is not None:
                        return val
                return None

            def _first_positive(row: dict, keys: list[str]):
                val = _first_number(row, keys)
                return val if val is not None and val > 0 else None

            if base_revenue <= 0:
                db = get_db()
                source_chain.append('db.get_financials')
                financials = await db.get_financials(code, limit=max(8, years * 2))
                if isinstance(financials, dict):
                    financials = [financials]
                if not isinstance(financials, list):
                    financials = []

                if not financials:
                    try:
                        source_chain.append('finance.get_financials')
                        from .finance import get_financials as _api_get_financials
                        fin_res = await _api_get_financials(code)
                        if fin_res and fin_res.get('success') and fin_res.get('data'):
                            api_data = fin_res['data']
                            financials = api_data if isinstance(api_data, list) else [api_data]
                            fallback_reason.append('base_revenue 未提供，已降级使用 finance.get_financials 自动回填')
                    except Exception as e:
                        fallback_reason.append(f'finance.get_financials 降级失败: {e}')

                revenue_keys = [
                    'revenue', 'total_revenue', 'totalRevenue', 'operating_revenue', 'operatingRevenue',
                    'oper_rev', 'main_business_income', '营业总收入', '营业收入',
                ]
                profit_keys = [
                    'net_profit', 'netProfit', 'net_income', 'n_income', 'profit',
                    'n_income_attr_p', 'net_profit_atsopc', 'parent_net_profit',
                    '归母净利润', '净利润',
                ]

                revenue_candidates = [_first_positive(item, revenue_keys) for item in financials]
                revenue_candidates = [float(v) for v in revenue_candidates if v is not None]
                if revenue_candidates:
                    base_revenue = float(revenue_candidates[0])
                    base_revenue_source = 'financial_revenue'
                else:
                    profit_candidates = [_first_positive(item, profit_keys) for item in financials]
                    profit_candidates = [float(v) for v in profit_candidates if v is not None]
                    if profit_candidates:
                        fallback_profit = float(statistics.mean(profit_candidates[: min(3, len(profit_candidates))]))
                        inferred_margin = 0.20
                        base_revenue = float(max(fallback_profit / inferred_margin, fallback_profit * 2))
                        base_revenue_source = 'financial_profit_inferred_revenue'
                        fallback_reason.append('缺少有效营收数据，已使用净利润反推 base_revenue')

            if base_revenue <= 0:
                # 最后兜底：使用 10 亿保守基准，避免工具因缺乏财务数据而完全不可用
                base_revenue = 1_000_000_000.0
                base_revenue_source = 'default_conservative_estimate'
                fallback_reason.append(
                    '无法从财务数据自动回填 base_revenue，已使用默认保守基准 10 亿元；'
                    '请通过 base_revenue 参数提供实际营收以获得更准确结果'
                )
            # 1. 确定基准参数：行业模板 → 用户覆盖 → 默认值
            if industry and industry in INDUSTRY_TEMPLATES:
                tpl = INDUSTRY_TEMPLATES[industry]
            else:
                tpl = {}

            def _p(name, default):
                """优先级: 用户显式传参 > 行业模板 > 默认值"""
                user_val = locals_map.get(name)
                if user_val is not None:
                    return user_val
                return tpl.get(name, default)

            locals_map = {
                "growth_rate": growth_rate,
                "profit_margin": profit_margin,
                "capex_ratio": capex_ratio,
                "depreciation_ratio": depreciation_ratio,
                "nwc_ratio": nwc_ratio,
                "beta": beta,
                "equity_weight": equity_weight,
                "debt_weight": debt_weight,
                "cost_of_debt": cost_of_debt,
                "terminal_growth": terminal_growth,
            }

            base_gr = _p("growth_rate", 0.08)
            base_pm = _p("profit_margin", 0.12)
            base_capex = _p("capex_ratio", 0.06)
            base_depr = _p("depreciation_ratio", 0.04)
            base_nwc = _p("nwc_ratio", 0.02)
            base_beta = _p("beta", 1.0)
            base_ew = _p("equity_weight", 0.70)
            base_dw = _p("debt_weight", 0.30)
            base_cod = _p("cost_of_debt", 0.04)
            base_tg = _p("terminal_growth", 0.025)

            # 2. 构建情景
            if custom_scenarios:
                scenarios = custom_scenarios
            else:
                common = dict(
                    capex_ratio=base_capex,
                    depreciation_ratio=base_depr,
                    nwc_ratio=base_nwc,
                    beta=base_beta,
                    equity_weight=base_ew,
                    debt_weight=base_dw,
                    cost_of_debt=base_cod,
                    terminal_growth=base_tg,
                )
                scenarios = [
                    {
                        "name": "Bull（乐观）",
                        "probability": bull_probability,
                        "growth_rate": base_gr + bull_growth_premium,
                        "profit_margin": base_pm + bull_margin_premium,
                        **common,
                    },
                    {
                        "name": "Base（基准）",
                        "probability": base_probability,
                        "growth_rate": base_gr,
                        "profit_margin": base_pm,
                        **common,
                    },
                    {
                        "name": "Bear（悲观）",
                        "probability": bear_probability,
                        "growth_rate": max(base_gr - bear_growth_discount, 0.0),
                        "profit_margin": max(base_pm - bear_margin_discount, 0.01),
                        **common,
                    },
                ]

            # 3. 执行多情景 DCF
            result = _scenario_dcf(
                base_revenue=base_revenue,
                years=years,
                tax_rate=tax_rate,
                risk_free_rate=risk_free_rate,
                market_risk_premium=market_risk_premium,
                scenarios=scenarios,
                enable_distribution=enable_distribution,
                distribution_samples=distribution_samples,
                distribution_growth_std=distribution_growth_std,
                distribution_margin_std=distribution_margin_std,
                distribution_discount_std=distribution_discount_std,
                distribution_terminal_std=distribution_terminal_std,
                distribution_seed=distribution_seed,
            )

            payload = {
                "code": code,
                "model": "Multi-Scenario Probability-Weighted DCF",
                "industry": industry or "custom",
                "years": years,
                "base_revenue": float(base_revenue),
                "base_revenue_source": base_revenue_source,
                "source_chain": source_chain,
                "fallback_reason": fallback_reason,
                **result,
            }

            # F-N08-1 fix (诊断报告 §N08): 内在价值合理性护栏。
            # 历史问题: 负利润率×高 capex 情景使 FCF 为负，终值公式放大成巨额负内在价值
            # (如 -2744亿)、负每股 (-53元)，工具仍 success=true 输出数学上无意义的负估值。
            # 修复: 检测 weighted/scenario 内在价值 <=0 时标注 quality_flags + valuation_reliable=false，
            # 并在 warnings 提示「负内在价值不可作为估值结论」(不静默放行荒谬值)。
            scenario_rows = result.get("scenarios") or []
            negative_scenarios = [
                r.get("scenario")
                for r in scenario_rows
                if isinstance(r, dict) and float(r.get("intrinsic_value", 0.0) or 0.0) <= 0.0
            ]
            weighted_iv = float(result.get("weighted_intrinsic_value", 0.0) or 0.0)
            quality_flags: list[str] = []
            valuation_reliable = True
            valuation_warnings: list[str] = []

            if weighted_iv <= 0.0:
                valuation_reliable = False
                quality_flags.append("non_positive_intrinsic_value")
                valuation_warnings.append(
                    "概率加权内在价值 <= 0（FCF 为负被终值公式放大），"
                    "数学上无意义，不可作为估值结论；通常因利润率过低/资本开支过高，"
                    "请核对 profit_margin / capex_ratio 或改用相对估值"
                )
            elif negative_scenarios:
                quality_flags.append("partial_negative_scenarios")
                valuation_warnings.append(
                    f"以下情景内在价值为负：{negative_scenarios}；"
                    "加权结果虽为正，但负情景表明参数假设可能不合理"
                )

            # 极端离散度也降级可靠性（F-N08-3 关联）
            w_interval = result.get("weighted_valuation_interval") or {}
            if isinstance(w_interval, dict) and w_interval.get("spread_risk") == "extreme":
                quality_flags.append("extreme_dispersion")
                valuation_warnings.append(
                    "蒙特卡洛离散度 extreme（std 远大于 mean），点估计不可靠"
                )

            payload["valuation_reliable"] = valuation_reliable
            if quality_flags:
                payload["quality_flags"] = quality_flags
            if valuation_warnings:
                payload["valuation_warnings"] = valuation_warnings

            if shares_outstanding and shares_outstanding > 0:
                per_share = result["weighted_intrinsic_value"] / shares_outstanding
                payload["per_share_value"] = float(per_share)
                payload["shares_outstanding"] = shares_outstanding
                if per_share <= 0.0:
                    payload["valuation_reliable"] = False
                    if "non_positive_intrinsic_value" not in quality_flags:
                        payload.setdefault("quality_flags", []).append("non_positive_per_share")
                    payload.setdefault("valuation_warnings", []).append(
                        f"每股内在价值为负 ({per_share:.2f} 元)，股价不可能为负，结果不可用"
                    )

            return ok(payload)

        except Exception as e:
            return fail(str(e))
