"""组合管理工具"""

import time
from typing import List, Dict, Any, Optional
from ..services.portfolio_optimization import simple_portfolio_optimizer as portfolio_optimizer
from ..services.risk_model import risk_model
from ..storage import get_db
from .manager_protocol import fail_with_meta, ok_with_meta
import numpy as np


def register(mcp):
    """注册组合管理工具"""

    def _meta(
        *,
        status: str,
        target: str,
        degraded: bool = False,
        extra_quality: dict | None = None,
    ) -> dict:
        quality = {"status": status}
        if isinstance(extra_quality, dict):
            quality.update(extra_quality)
        return {
            "quality": quality,
            "side_effect": {
                "level": "read_only",
                "target": target,
                "confirmation_required": False,
                "idempotent": True,
            },
            "degraded": degraded,
        }
    
    @mcp.tool()
    async def optimize_portfolio(
        stocks: List[str],
        method: str = 'equal_weight',
        lookback_days: int = 252,
        risk_aversion: float = 1.0,
        risk_free_rate: float = 0.03,
        market_weights: Optional[List[float]] = None,
        views: Optional[List[Dict[str, Any]]] = None,
        risk_budgets: Optional[List[float]] = None,
        max_weight: float = 0.35,
    ):
        """
        组合优化
        
        Args:
            stocks: 股票代码列表
            method: 优化方法
                - 'equal_weight': 等权重
                - 'risk_parity': 风险平价
                - 'mean_variance': 均值方差优化
                - 'black_litterman': Black-Litterman模型
                - 'risk_budget': 风险预算优化
                - 'max_sharpe': 最大夏普比率
            lookback_days: 回溯天数
            risk_aversion: 风险厌恶系数（用于均值方差和Black-Litterman）
            risk_free_rate: 无风险利率（用于夏普比率计算）
            market_weights: 市场权重（用于Black-Litterman）
            views: 主观观点（用于Black-Litterman）
                [{'type': 'absolute', 'asset': 0, 'return': 0.10},
                 {'type': 'relative', 'assets': [0, 1], 'return': 0.05}]
            risk_budgets: 风险预算（用于风险预算优化）
            max_weight: 单资产最大权重上限（用于 max_sharpe，默认 0.35）
        
        Returns:
            最优权重和组合指标
        """
        started_at = time.perf_counter()
        source_chain = ["portfolio.optimize_portfolio"]
        try:
            if not isinstance(stocks, list) or not stocks:
                return fail_with_meta(
                    'stocks 不能为空',
                    tool_name="optimize_portfolio",
                    action="validate",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="invalid_params", target="portfolio_optimization", degraded=True),
                )
            if int(lookback_days or 0) < 2:
                return fail_with_meta(
                    'lookback_days 必须 >= 2',
                    tool_name="optimize_portfolio",
                    action="validate",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="invalid_params", target="portfolio_optimization", degraded=True),
                )
            # 获取历史数据（FIX-20: 显式区分有效股与无数据股，避免静默丢弃 + 权重错位）
            db = get_db()
            source_chain.append("db.get_klines")
            returns_list = []
            valid_codes: List[str] = []
            dropped_codes: List[str] = []

            for code in stocks:
                klines = await db.get_klines(code, limit=lookback_days)
                if klines and len(klines) >= 2:
                    closes = [k['close'] for k in klines]
                    returns = np.diff(closes) / closes[:-1]
                    returns_list.append(returns)
                    valid_codes.append(code)
                else:
                    dropped_codes.append(code)

            if method == 'equal_weight':
                # FIX-20: equal_weight 也只对有效股配权，剔除无数据/垃圾代码
                if not valid_codes:
                    return fail_with_meta(
                        'No data available for any stock',
                        tool_name="optimize_portfolio",
                        action=method,
                        started_at=started_at,
                        source_chain=source_chain,
                        extra_meta=_meta(
                            status="not_found",
                            target="portfolio_optimization",
                            degraded=True,
                            extra_quality={"method": method, "dropped_codes": dropped_codes},
                        ),
                    )
                weights = portfolio_optimizer.optimize_equal_weight(valid_codes)
                return ok_with_meta(
                    {
                        'weights': weights,
                        'method': method,
                        'valid_codes': valid_codes,
                        'dropped_codes': dropped_codes,
                    },
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="available" if not dropped_codes else "degraded",
                        target="portfolio_optimization",
                        degraded=bool(dropped_codes),
                        extra_quality={
                            "method": method,
                            "stock_count": len(valid_codes),
                            "dropped_codes": dropped_codes,
                        },
                    ),
                )

            if not returns_list:
                return fail_with_meta(
                    'No data available',
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="not_found",
                        target="portfolio_optimization",
                        degraded=True,
                        extra_quality={"method": method, "stock_count": len(stocks or []), "dropped_codes": dropped_codes},
                    ),
                )
            
            # 对齐长度
            min_len = min(len(r) for r in returns_list)
            returns_matrix = np.array([r[:min_len] for r in returns_list])
            
            # 计算预期收益率（历史平均）
            expected_returns = np.mean(returns_matrix, axis=1)
            
            # 根据方法选择优化器
            if method == 'risk_parity':
                weights = portfolio_optimizer.optimize_risk_parity(valid_codes, returns_matrix)
                return ok_with_meta(
                    {
                        'weights': weights,
                        'method': method,
                        'valid_codes': valid_codes,
                        'dropped_codes': dropped_codes,
                    },
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="available",
                        target="portfolio_optimization",
                        extra_quality={"method": method, "stock_count": len(stocks or []), "lookback_days": lookback_days},
                    ),
                )
            
            elif method == 'mean_variance':
                weights = portfolio_optimizer.optimize_mean_variance(
                    valid_codes, 
                    returns_matrix, 
                    expected_returns,
                    risk_aversion=risk_aversion
                )
                return ok_with_meta(
                    {
                        'weights': weights,
                        'method': method,
                        'risk_aversion': risk_aversion,
                        'valid_codes': valid_codes,
                        'dropped_codes': dropped_codes,
                    },
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="available",
                        target="portfolio_optimization",
                        extra_quality={"method": method, "stock_count": len(stocks or []), "lookback_days": lookback_days},
                    ),
                )
            
            elif method == 'black_litterman':
                # 默认市场权重为等权（按有效股数量）
                if market_weights is None:
                    market_weights = np.array([1.0 / len(valid_codes)] * len(valid_codes))
                else:
                    market_weights = np.array(market_weights)
                
                # 默认观点为空
                if views is None:
                    views = []
                
                result = portfolio_optimizer.optimize_black_litterman(
                    valid_codes,
                    returns_matrix,
                    market_weights,
                    views,
                    risk_aversion=risk_aversion
                )
                
                return ok_with_meta(
                    {
                        'weights': result['weights'],
                        'posterior_returns': result['posterior_returns'],
                        'expected_return': f"{result['expected_return']*100:.2f}%",
                        'volatility': f"{result['volatility']*100:.2f}%",
                        'sharpe_ratio': f"{result['sharpe_ratio']:.2f}",
                        'method': method,
                        'valid_codes': valid_codes,
                        'dropped_codes': dropped_codes,
                    },
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="available",
                        target="portfolio_optimization",
                        extra_quality={"method": method, "stock_count": len(stocks or []), "lookback_days": lookback_days},
                    ),
                )
            
            elif method == 'risk_budget':
                result = portfolio_optimizer.optimize_risk_budget(
                    valid_codes,
                    returns_matrix,
                    risk_budgets=risk_budgets
                )
                
                return ok_with_meta(
                    {
                        'weights': result['weights'],
                        'risk_contributions': result['risk_contributions'],
                        'portfolio_volatility': f"{result['portfolio_volatility']*100:.2f}%",
                        'method': method,
                        'valid_codes': valid_codes,
                        'dropped_codes': dropped_codes,
                    },
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="available",
                        target="portfolio_optimization",
                        extra_quality={"method": method, "stock_count": len(stocks or []), "lookback_days": lookback_days},
                    ),
                )
            
            elif method == 'max_sharpe':
                result = portfolio_optimizer.optimize_max_sharpe(
                    valid_codes,
                    returns_matrix,
                    expected_returns,
                    risk_free_rate=risk_free_rate,
                    max_weight=max_weight,
                )

                return ok_with_meta(
                    {
                        'weights': result['weights'],
                        'expected_return': f"{result['expected_return']*100:.2f}%",
                        'volatility': f"{result['volatility']*100:.2f}%",
                        'sharpe_ratio': f"{result['sharpe_ratio']:.2f}",
                        'constraints_applied': result.get('constraints_applied', {'max_weight': max_weight}),
                        'method': method,
                        'valid_codes': valid_codes,
                        'dropped_codes': dropped_codes,
                    },
                    tool_name="optimize_portfolio",
                    action=method,
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="available",
                        target="portfolio_optimization",
                        extra_quality={"method": method, "stock_count": len(stocks or []), "lookback_days": lookback_days},
                    ),
                )
            
            else:
                return fail_with_meta(
                    f'Unknown method: {method}. Supported: equal_weight, risk_parity, mean_variance, black_litterman, risk_budget, max_sharpe',
                    tool_name="optimize_portfolio",
                    action="validate",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(
                        status="invalid_params",
                        target="portfolio_optimization",
                        degraded=True,
                        extra_quality={"method": method},
                    ),
                )
        
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="optimize_portfolio",
                action=method,
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(status="failed", target="portfolio_optimization", degraded=True),
            )
    
    @mcp.tool()
    async def analyze_portfolio_risk(
        holdings: Optional[List[Dict[str, Any]]] = None,
        lookback_days: int = 252,
        portfolio_id: Optional[int | str] = None,
        codes: Optional[List[str]] = None,
        weights: Optional[List[float]] = None,
    ):
        """
        分析组合风险
        
        Args:
            holdings: 持仓列表 [{'code': '600519', 'weight': 0.3}, ...]
            lookback_days: 回溯天数
            portfolio_id: 组合ID，可从持仓表读取
            codes: 股票代码列表
            weights: 权重列表，与 codes 对应
        """
        started_at = time.perf_counter()
        source_chain = ["portfolio.analyze_portfolio_risk"]
        try:
            db = get_db()
            requested_holdings: List[Dict[str, Any]] = []

            if holdings:
                requested_holdings = [dict(item) for item in holdings if isinstance(item, dict) and item.get('code')]
            elif codes:
                normalized_codes = [str(code).strip() for code in codes if str(code).strip()]
                if not normalized_codes:
                    return fail_with_meta(
                        'codes 不能为空',
                        tool_name="analyze_portfolio_risk",
                        action="analyze",
                        started_at=started_at,
                        source_chain=source_chain,
                        extra_meta=_meta(status="invalid_params", target="portfolio_risk", degraded=True),
                    )
                raw_weights = weights or []
                if raw_weights and len(raw_weights) != len(normalized_codes):
                    return fail_with_meta(
                        'weights 数量必须与 codes 一致',
                        tool_name="analyze_portfolio_risk",
                        action="analyze",
                        started_at=started_at,
                        source_chain=source_chain,
                        extra_meta=_meta(status="invalid_params", target="portfolio_risk", degraded=True),
                    )
                if raw_weights:
                    requested_holdings = [
                        {'code': code, 'weight': float(raw_weights[idx])}
                        for idx, code in enumerate(normalized_codes)
                    ]
                else:
                    equal_weight = 1.0 / len(normalized_codes)
                    requested_holdings = [
                        {'code': code, 'weight': equal_weight}
                        for code in normalized_codes
                    ]
            elif portfolio_id:
                if not hasattr(db, 'acquire'):
                    return fail_with_meta(
                        '当前数据源不支持通过 portfolio_id 加载持仓',
                        tool_name="analyze_portfolio_risk",
                        action="analyze",
                        started_at=started_at,
                        source_chain=source_chain,
                        extra_meta=_meta(status="unsupported", target="portfolio_risk", degraded=True),
                    )
                source_chain.append("db.holdings")
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT code, shares, cost_price, weight FROM holdings WHERE portfolio_id = $1",
                        portfolio_id,
                    )
                requested_holdings = [
                    {
                        'code': row['code'],
                        'weight': float(row.get('weight') or 0),
                        'shares': row.get('shares'),
                        'cost_price': row.get('cost_price'),
                    }
                    for row in rows
                    if row.get('code')
                ]
            else:
                return fail_with_meta(
                    '需要提供 holdings、portfolio_id 或 codes + weights',
                    tool_name="analyze_portfolio_risk",
                    action="analyze",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="invalid_params", target="portfolio_risk", degraded=True),
                )

            if not requested_holdings:
                return fail_with_meta(
                    '未获取到可分析的持仓数据',
                    tool_name="analyze_portfolio_risk",
                    action="analyze",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="not_found", target="portfolio_risk", degraded=True),
                )

            returns_list = []
            valid_holdings = []
            dropped_holdings = []
            source_chain.append("db.get_klines")
            
            for holding in requested_holdings:
                code = holding['code']
                klines = await db.get_klines(code, limit=lookback_days)
                if klines and len(klines) >= 2:
                    closes = [k['close'] for k in klines]
                    returns = np.diff(closes) / closes[:-1]
                    returns_list.append(returns)
                    valid_holdings.append(dict(holding))
                else:
                    dropped_holdings.append({
                        'code': code,
                        'reason': 'insufficient_kline_data',
                    })
            
            if not returns_list:
                return fail_with_meta(
                    'No data available',
                    tool_name="analyze_portfolio_risk",
                    action="analyze",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="not_found", target="portfolio_risk", degraded=True),
                )
            
            min_len = min(len(r) for r in returns_list)
            returns_matrix = np.array([r[:min_len] for r in returns_list])
            
            # 计算组合收益率
            weights = np.array([float(h.get('weight', 0) or 0) for h in valid_holdings], dtype=float)
            weight_sum = float(np.sum(weights))
            if weight_sum <= 0:
                weights = np.array([1.0 / len(valid_holdings)] * len(valid_holdings), dtype=float)
            else:
                weights = weights / weight_sum
            analyzed_holdings = [
                {**holding, 'weight': float(weights[idx])}
                for idx, holding in enumerate(valid_holdings)
            ]
            portfolio_returns = np.dot(weights, returns_matrix)
            
            # 计算VaR
            var_result = risk_model.calculate_var(portfolio_returns.tolist())
            
            # 计算组合风险
            risk_result = risk_model.calculate_portfolio_risk(analyzed_holdings, returns_matrix)
            
            degraded = bool(dropped_holdings)
            return ok_with_meta(
                {
                    'var': var_result,
                    'risk': risk_result,
                    'portfolio_id': portfolio_id,
                    'analyzed_holdings': analyzed_holdings,
                    'dropped_holdings': dropped_holdings,
                    'coverage': {
                        'requested': len(requested_holdings),
                        'used': len(analyzed_holdings),
                        'dropped': len(dropped_holdings),
                    },
                },
                tool_name="analyze_portfolio_risk",
                action="analyze",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(
                    status="partial" if degraded else "available",
                    target=portfolio_id or "portfolio_risk",
                    degraded=degraded,
                    extra_quality={
                        "requested_holdings": len(requested_holdings),
                        "used_holdings": len(analyzed_holdings),
                        "dropped_holdings": len(dropped_holdings),
                    },
                ),
            )
        
        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="analyze_portfolio_risk",
                action="analyze",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(status="failed", target=portfolio_id or "portfolio_risk", degraded=True),
            )

    @mcp.tool()
    async def stress_test_portfolio(
        holdings: List[Dict[str, Any]],
        scenarios: Optional[List[str]] = None
    ):
        """
        组合压力测试（委托服务层 RiskModel.stress_test 执行 4 种场景 + 自定义冲击）

        Args:
            holdings: 持仓列表 [{'code': '600519', 'weight': 0.3, 'value': 100000}, ...]
            scenarios: 压力场景 ['market_crash', 'sector_rotation', 'interest_rate_hike', 'black_swan']
        """
        started_at = time.perf_counter()
        source_chain = ["portfolio.stress_test_portfolio", "risk_model.stress_test"]
        try:
            if not isinstance(holdings, list) or not holdings:
                return fail_with_meta(
                    'holdings 不能为空',
                    tool_name="stress_test_portfolio",
                    action="stress_test",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="invalid_params", target="portfolio_stress", degraded=True),
                )
            if not scenarios:
                scenarios = ['market_crash', 'sector_rotation', 'interest_rate_hike', 'black_swan']

            # 如果 holdings 缺少 value 字段，用 weight * 1000000 估算
            for h in holdings:
                if 'value' not in h:
                    h['value'] = h.get('weight', 0) * 1_000_000

            results = {}
            for scenario in scenarios:
                result = risk_model.stress_test(holdings, scenario=scenario)
                if isinstance(result, dict):
                    for key in ("loss_percent", "loss_percentage", "return_percent", "total_return_pct"):
                        if key not in result:
                            continue
                        try:
                            raw = result.get(key)
                            if isinstance(raw, str):
                                raw_value = float(raw.strip().rstrip("%"))
                            else:
                                raw_value = float(raw)
                            if abs(raw_value) <= 1:
                                raw_value *= 100
                            if key in {"loss_percent", "loss_percentage"}:
                                result["loss_percent"] = abs(raw_value)
                                result["signed_return_percent"] = -abs(raw_value)
                            else:
                                result.setdefault("signed_return_percent", raw_value)
                                result.setdefault("loss_percent", abs(raw_value) if raw_value < 0 else 0.0)
                            break
                        except Exception:
                            continue
                results[scenario] = result

            return ok_with_meta(
                {
                    'holdings_count': len(holdings),
                    'stress_tests': results,
                },
                tool_name="stress_test_portfolio",
                action="stress_test",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(
                    status="available",
                    target="portfolio_stress",
                    extra_quality={"holdings_count": len(holdings or []), "scenario_count": len(scenarios or [])},
                ),
            )

        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="stress_test_portfolio",
                action="stress_test",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(status="failed", target="portfolio_stress", degraded=True),
            )

    @mcp.tool()
    async def analyze_portfolio_risk_barra(
        holdings: List[Dict[str, Any]],
        lookback_days: int = 252
    ):
        """
        Barra 多因子风险分解

        Args:
            holdings: 持仓列表 [{'code': '600519', 'weight': 0.3}, ...]
            lookback_days: 回溯天数
        """
        started_at = time.perf_counter()
        source_chain = ["portfolio.analyze_portfolio_risk_barra", "db.get_klines", "risk_model.calculate_barra_risk"]
        try:
            db = get_db()
            codes = [h['code'] for h in holdings]

            # 获取因子暴露（简化：用技术指标近似 Barra 风格因子）
            factor_names = ['momentum', 'volatility', 'size', 'value', 'quality']
            factor_exposures = {}
            returns_list = []

            for code in codes:
                klines = await db.get_klines(code, limit=lookback_days)
                if not klines:
                    continue
                closes = [k['close'] for k in klines]
                returns = np.diff(closes) / closes[:-1]
                returns_list.append(returns)

                # 简化因子暴露估算
                mom = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 else 0
                vol = float(np.std(returns[-60:])) if len(returns) >= 60 else 0
                factor_exposures[code] = {
                    'momentum': mom,
                    'volatility': vol,
                    'size': 0.0,
                    'value': 0.0,
                    'quality': 0.0,
                }

            if not returns_list or not factor_exposures:
                return fail_with_meta(
                    'No data available for Barra decomposition',
                    tool_name="analyze_portfolio_risk_barra",
                    action="decompose",
                    started_at=started_at,
                    source_chain=source_chain,
                    extra_meta=_meta(status="not_found", target="portfolio_barra", degraded=True),
                )

            # 构建因子协方差（简化：单位矩阵 × 平均方差）
            n_factors = len(factor_names)
            factor_cov = np.eye(n_factors) * 0.01

            # 特质风险
            specific_risks = {}
            for i, code in enumerate(codes):
                if i < len(returns_list):
                    specific_risks[code] = float(np.std(returns_list[i]))

            result = risk_model.calculate_barra_risk(
                holdings=holdings,
                factor_exposures=factor_exposures,
                factor_covariance=factor_cov,
                specific_risks=specific_risks,
            )
            return ok_with_meta(
                result,
                tool_name="analyze_portfolio_risk_barra",
                action="decompose",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(
                    status="available",
                    target="portfolio_barra",
                    extra_quality={
                        "holdings_count": len(holdings or []),
                        "factor_exposure_count": len(factor_exposures),
                        "lookback_days": lookback_days,
                    },
                ),
            )

        except Exception as e:
            return fail_with_meta(
                str(e),
                tool_name="analyze_portfolio_risk_barra",
                action="decompose",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta=_meta(status="failed", target="portfolio_barra", degraded=True),
            )
