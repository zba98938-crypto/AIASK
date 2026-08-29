"""
工具名称别名映射
用于兼容Node.js版本的工具名称
"""

# Node.js工具名 -> Python工具名
TOOL_ALIASES = {
    # 市场数据
    'get_kline_data': 'get_kline',
    'get_financial_summary': 'get_financials',
    'get_historical_financials': 'get_financials',
    
    # 资金流向
    'get_fund_flow': 'get_stock_fund_flow',
    
    # 技术分析
    'calculate_indicators': 'calculate_technical_indicators',
    
    # 估值
    'get_valuation_metrics': 'get_stock_info',  # 估值指标在stock_info中
    
    # 其他需要映射的工具...
}

# 参数名称映射（Node.js参数名 -> Python参数名）
PARAM_ALIASES = {
    'get_batch_quotes': {
        'codes': 'stock_codes'
    },
    'get_kline_data': {
        'code': 'stock_code',
        'startDate': 'start_date',
        'endDate': 'end_date',
    },
    'run_simple_backtest': {
        'stock_codes': 'code',  # Node支持数组，Python只支持单个
        'initial_capital': 'initial_capital',
    },
    'calculate_technical_indicators': {
        'code': 'code',
        'indicators': 'indicators',
        'period': 'period',
        'limit': 'limit',
    },
    'analyze_portfolio_risk': {
        'holdings': 'holdings',
        'benchmark': 'benchmark',
    },
    'optimize_portfolio': {
        'stocks': 'stocks',
        'method': 'method',
    },
    'get_market_blocks': {
        'type': 'block_type',
    },
    'get_financial_summary': {
        'code': 'code',
    },
    'get_historical_financials': {
        'code': 'code',
        'limit': 'limit',
    },
    'get_historical_valuation': {
        'code': 'code',
        'days': 'days',
    },
    'relative_valuation': {
        'code': 'code',
        'metrics': 'metrics',
        'peers': 'peers',
    },
}

# 返回字段映射（Python字段名 -> Node.js字段名）
RETURN_FIELD_ALIASES = {
    'get_realtime_quote': {
        'change_amt': 'changeAmount',
        'change_pct': 'changePercent',
        'prev_close': 'prevClose',
        'pre_close': 'prevClose',
        'market_cap': 'marketCap',
        'mkt_cap': 'marketCap',
    },
    'get_batch_quotes': {
        'change_amt': 'changeAmount',
        'change_pct': 'changePercent',
        'prev_close': 'prevClose',
        'pre_close': 'prevClose',
    },
    'get_stock_info': {
        'market_cap': 'marketCap',
        'pe_ratio': 'peRatio',
        'pb_ratio': 'pbRatio',
        'stock_name': 'name',
    },
    'get_kline_data': {
        'trade_date': 'date',
    },
    'run_simple_backtest': {
        'total_return': 'totalReturn',
        'sharpe_ratio': 'sharpeRatio',
        'max_drawdown': 'maxDrawdown',
        'win_rate': 'winRate',
    },
    'get_financials': {
        'net_profit': 'netProfit',
        'total_revenue': 'revenue',
        'gross_margin': 'grossMargin',
        'net_margin': 'netMargin',
        'debt_ratio': 'debtRatio',
        'current_ratio': 'currentRatio',
    },
    'get_financial_summary': {
        'net_profit': 'netProfit',
        'total_revenue': 'revenue',
        'gross_margin': 'grossMargin',
        'net_margin': 'netMargin',
        'debt_ratio': 'debtRatio',
        'current_ratio': 'currentRatio',
    },
    'get_historical_financials': {
        'net_profit': 'netProfit',
        'total_revenue': 'revenue',
    },
    'get_valuation_metrics': {
        'pe_ratio': 'peRatio',
        'pb_ratio': 'pbRatio',
        'market_cap': 'marketCap',
    },
    'get_historical_valuation': {
        'pe_ratio': 'peRatio',
        'pb_ratio': 'pbRatio',
        'market_cap': 'marketCap',
        'mkt_cap': 'marketCap',
    },
    'relative_valuation': {
        'pe_ratio': 'peRatio',
        'pb_ratio': 'pbRatio',
        'ps_ratio': 'psRatio',
        'industry_mean': 'industryAvg',
        'industry_median': 'industryMedian',
        'premium_to_mean': 'premium',
        'target_metrics': 'targetMetrics',
        'industry_stats': 'industryStats',
        'peer_count': 'peerCount',
    },
}


def get_tool_alias(node_tool_name: str) -> str:
    """获取工具别名"""
    return TOOL_ALIASES.get(node_tool_name, node_tool_name)


def map_params(tool_name: str, params: dict) -> dict:
    """映射参数名称"""
    if tool_name not in PARAM_ALIASES:
        return params
    
    mapping = PARAM_ALIASES[tool_name]
    mapped_params = {}
    
    for key, value in params.items():
        mapped_key = mapping.get(key, key)
        mapped_params[mapped_key] = value
    
    return mapped_params


def map_return_fields(tool_name: str, data: dict) -> dict:
    """映射返回字段名称（Python -> Node.js）"""
    if tool_name not in RETURN_FIELD_ALIASES:
        return data
    
    mapping = RETURN_FIELD_ALIASES[tool_name]
    mapped_data = {}
    
    for key, value in data.items():
        # 使用映射表转换字段名
        mapped_key = mapping.get(key, key)
        mapped_data[mapped_key] = value
    
    return mapped_data


def map_return_fields_reverse(tool_name: str, data: dict) -> dict:
    """映射返回字段名称（Node.js -> Python）"""
    if tool_name not in RETURN_FIELD_ALIASES:
        return data
    
    mapping = RETURN_FIELD_ALIASES[tool_name]
    # 反向映射
    reverse_mapping = {v: k for k, v in mapping.items()}
    mapped_data = {}
    
    for key, value in data.items():
        mapped_key = reverse_mapping.get(key, key)
        mapped_data[mapped_key] = value
    
    return mapped_data
