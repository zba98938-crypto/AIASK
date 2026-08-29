"""选股查询解析 — parse_selection_query + 辅助函数"""

import re
from ...utils import ok, fail


def parse_selection_query(query: str):
    """
    解析自然语言选股查询，将中文条件转换为结构化筛选参数

    适用场景: 将用户口语化的选股需求转换为 screener_manager 可消费的条件格式

    Args:
        query (str, required): 自然语言查询，支持基本面+技术面混合条件
            基本面示例: "市盈率小于20且ROE大于15%"、"市值大于100亿"
            技术面示例: "MACD金叉且放量"、"连续3天上涨"
            组合示例: "PE<20且MACD金叉"

    Returns:
        dict: {"success": bool, "data": {
            "query": str,                          # 原始查询
            "fundamental_conditions": list[dict],  # 基本面条件，每项含 field/operator/value
            "technical_conditions": list[dict],    # 技术面条件，每项含 id(str), params(dict|null)
            "logic": str,                          # 条件逻辑关系 "AND"|"OR"
            "parsed": bool,                        # 是否解析成功
            "has_fundamental": bool,
            "has_technical": bool,
            "suggestion": str                      # 推荐的后续调用建议
        }}

    Errors:
        - 无法解析出有效条件时 suggestion 会提示用户使用更具体的描述

    Examples:
        parse_selection_query("市盈率小于20且ROE大于15%")
        parse_selection_query("MACD金叉且放量")
    """
    try:
        conditions = []
        tech_conditions = []
        semantic_conditions = []

        q = query.lower()

        # 通用比较运算符模式（支持中英文）
        LT = r'(?:[<＜]|小于|低于|不超过|不高于|<=|≤)'
        GT = r'(?:[>＞]|大于|高于|超过|不低于|>=|≥)'

        # ============== 基本面条件解析 ==============
        _parse_fundamental(q, conditions, LT, GT)
        _parse_semantic_filters(query, semantic_conditions)

        # ============== 技术面条件解析 ==============
        _parse_technical(q, tech_conditions)

        # 确定逻辑关系
        logic = 'AND'
        if '或' in query or '或者' in query or 'or' in q:
            logic = 'OR'

        return ok({
            'query': query,
            'fundamental_conditions': conditions,
            'technical_conditions': tech_conditions,
            'semantic_conditions': semantic_conditions,
            'industry': _first_semantic_value(semantic_conditions, 'industry'),
            'theme': _first_semantic_value(semantic_conditions, 'theme'),
            'logic': logic,
            'parsed': True,
            'has_fundamental': len(conditions) > 0,
            'has_technical': len(tech_conditions) > 0,
            'has_semantic': len(semantic_conditions) > 0,
            'suggestion': _build_suggestion(conditions, tech_conditions, logic, semantic_conditions),
        })

    except Exception as e:
        return fail(str(e))


# --------------- 内部辅助 ---------------

def _parse_fundamental(q: str, conditions: list, LT: str, GT: str):
    """解析基本面条件"""
    # PE / 市盈率
    pe_match = re.search(rf'(?:pe|市盈率)\s*{LT}\s*(\d+\.?\d*)', q)
    if pe_match:
        conditions.append({'field': 'pe_ratio', 'operator': '<', 'value': float(pe_match.group(1))})
    pe_match2 = re.search(rf'(?:pe|市盈率)\s*{GT}\s*(\d+\.?\d*)', q)
    if pe_match2:
        conditions.append({'field': 'pe_ratio', 'operator': '>', 'value': float(pe_match2.group(1))})
    pe_range = re.search(r'(?:pe|市盈率)\s*(?:在)?\s*(\d+\.?\d*)\s*(?:到|至|-|~)\s*(\d+\.?\d*)', q)
    if pe_range:
        conditions.append({'field': 'pe_ratio', 'operator': '>', 'value': float(pe_range.group(1))})
        conditions.append({'field': 'pe_ratio', 'operator': '<', 'value': float(pe_range.group(2))})

    # PB / 市净率
    pb_match = re.search(rf'(?:pb|市净率)\s*{LT}\s*(\d+\.?\d*)', q)
    if pb_match:
        conditions.append({'field': 'pb_ratio', 'operator': '<', 'value': float(pb_match.group(1))})
    pb_match2 = re.search(rf'(?:pb|市净率)\s*{GT}\s*(\d+\.?\d*)', q)
    if pb_match2:
        conditions.append({'field': 'pb_ratio', 'operator': '>', 'value': float(pb_match2.group(1))})

    # ROE / 净资产收益率
    # P2-4.2.3 fix(诊断报告 §4.2.3):统一以 percent 单位输出,与 db.financials.roe (10.06=10.06%) 对齐
    # 历史问题:'10%' 被解析为 0.10,但 db ROE 字段是 10.06(已是 percent),scale 100x mismatch
    # 修复:解析后保留原始百分数(如 10),并标 unit='percent';不再除以 100
    roe_match = re.search(rf'(?:roe|净资产收益率)\s*{GT}\s*(\d+\.?\d*)%?', q)
    if roe_match:
        val = float(roe_match.group(1))
        conditions.append({'field': 'roe', 'operator': '>', 'value': val, 'unit': 'percent'})
    roe_match2 = re.search(rf'(?:roe|净资产收益率)\s*{LT}\s*(\d+\.?\d*)%?', q)
    if roe_match2:
        val = float(roe_match2.group(1))
        conditions.append({'field': 'roe', 'operator': '<', 'value': val, 'unit': 'percent'})

    # 负债率(同 ROE 修复:统一 percent 单位)
    debt_match = re.search(rf'负债率\s*{LT}\s*(\d+\.?\d*)%?', q)
    if debt_match:
        val = float(debt_match.group(1))
        conditions.append({'field': 'debt_ratio', 'operator': '<', 'value': val, 'unit': 'percent'})
    debt_match2 = re.search(rf'负债率\s*{GT}\s*(\d+\.?\d*)%?', q)
    if debt_match2:
        val = float(debt_match2.group(1))
        conditions.append({'field': 'debt_ratio', 'operator': '>', 'value': val, 'unit': 'percent'})

    # 营收增长
    rev_match = re.search(rf'(?:营收增长|收入增长|营收增速|净利润增长|利润增速)\s*{GT}\s*(\d+\.?\d*)%?', q)
    if rev_match:
        val = float(rev_match.group(1))
        conditions.append({'field': 'revenue_growth', 'operator': '>', 'value': val / 100 if val > 1 else val})

    # 市值
    cap_match = re.search(rf'市值\s*{GT}\s*(\d+\.?\d*)\s*亿', q)
    if cap_match:
        conditions.append({'field': 'market_cap', 'operator': '>', 'value': float(cap_match.group(1)) * 1e8})
    cap_match2 = re.search(rf'市值\s*{LT}\s*(\d+\.?\d*)\s*亿', q)
    if cap_match2:
        conditions.append({'field': 'market_cap', 'operator': '<', 'value': float(cap_match2.group(1)) * 1e8})

    # 股息率
    div_match = re.search(rf'(?:股息率|分红率)\s*{GT}\s*(\d+\.?\d*)%?', q)
    if div_match:
        conditions.append({'field': 'dividend_yield', 'operator': '>', 'value': float(div_match.group(1))})


def _parse_semantic_filters(query: str, semantic_conditions: list):
    text = str(query or "")
    industry_aliases = {
        '白酒': ['白酒', '酿酒', '酒类'],
        '酿酒': ['酿酒', '白酒', '酒类'],
        '半导体': ['半导体', '芯片'],
        '新能源汽车': ['新能源汽车', '新能源车', '电动车'],
        '新能源': ['新能源', '新能源汽车', '光伏', '储能'],
    }
    for canonical, aliases in industry_aliases.items():
        if any(alias in text for alias in aliases):
            semantic_conditions.append({'field': 'industry', 'operator': 'contains', 'value': canonical})
            break

    theme_aliases = {
        '高股息': ['高股息', '高分红'],
        '低估值': ['低估值', '便宜'],
        '成长': ['成长', '高增长'],
    }
    for canonical, aliases in theme_aliases.items():
        if any(alias in text for alias in aliases):
            semantic_conditions.append({'field': 'theme', 'operator': 'contains', 'value': canonical})


def _first_semantic_value(conditions: list, field: str):
    for item in conditions:
        if isinstance(item, dict) and item.get('field') == field:
            return item.get('value')
    return None


# 关键词 → 条件ID 映射
KEYWORD_MAP = {
    # 趋势
    'macd金叉': 'macd_golden_cross',
    'macd死叉': 'macd_death_cross',
    'macd零轴上': 'macd_above_zero',
    'macd底背离': 'macd_divergence_bull',
    'macd顶背离': 'macd_divergence_bear',
    'kdj金叉': 'kdj_golden_cross',
    'kdj死叉': 'kdj_death_cross',
    'kdj超卖': 'kdj_oversold',
    'kdj超买': 'kdj_overbought',
    'rsi超卖': 'rsi_oversold',
    'rsi超买': 'rsi_overbought',
    '布林收窄': 'boll_squeeze',
    '突破布林上轨': 'boll_breakout_upper',
    '跌破布林下轨': 'boll_breakout_lower',
    'dmi趋势增强': 'dmi_trend_strong',
    # 均线
    '均线多头': 'ma_bull',
    '多头排列': 'ma_bull',
    '均线空头': 'ma_bear',
    '空头排列': 'ma_bear',
    '均线金叉': 'golden_cross_ma',
    '均线死叉': 'death_cross_ma',
    '站上均线': 'price_above_ma',
    '跌破均线': 'price_below_ma',
    '创新高': 'new_high',
    '创新低': 'new_low',
    '突破前高': 'breakout_high',
    '上升趋势': 'trend_up',
    # 量价
    '放量': 'volume_breakout',
    '放量突破': 'volume_breakout',
    '缩量': 'volume_shrink',
    '缩量整理': 'volume_shrink',
    '量价齐升': 'volume_price_up',
    '量价背离': 'volume_price_diverge',
    '地量': 'low_volume_bottom',
    '量比': 'volume_ratio_high',
    # K线形态
    '锤头线': 'pattern_hammer',
    '流星线': 'pattern_shooting_star',
    '看涨吞没': 'pattern_engulfing_bull',
    '看跌吞没': 'pattern_engulfing_bear',
    '早晨之星': 'pattern_morning_star',
    '黄昏之星': 'pattern_evening_star',
    '红三兵': 'pattern_three_white',
    '三只乌鸦': 'pattern_three_black',
    '十字星': 'pattern_doji',
    '长下影线': 'pattern_long_lower_shadow',
    # A股特色
    '涨停': 'limit_up',
    '跌停': 'limit_down',
    '连板': 'continuous_limit_up',
    '首板': 'first_limit_up',
    '缩量涨停': 'limit_up_volume_shrink',
    't字板': 't_board',
    '一字板': 'one_line_board',
    '大阳线': 'big_yang',
    '大阴线': 'big_yin',
    '跳空高开': 'gap_up',
    '跳空低开': 'gap_down',
    # 组合策略
    '强势突破': 'strong_breakout',
    '底部反转': 'bottom_reversal',
    '趋势跟踪': 'trend_following',
    'vcp': 'vcp',
    '动量爆发': 'momentum_burst',
    '超跌反弹': 'oversold_bounce',
    '背离买入': 'divergence_buy',
}


def _parse_technical(q: str, tech_conditions: list):
    """解析技术面条件"""
    existing_ids = set()

    def _append_condition(condition_id: str, params: dict | None = None):
        if condition_id in existing_ids:
            return
        payload = {'id': condition_id}
        if isinstance(params, dict) and params:
            payload['params'] = params
        tech_conditions.append(payload)
        existing_ids.add(condition_id)

    # 连续上涨/下跌 带天数（FIX-16: 必须按方向词判定，避免"连续N天上涨"同时命中 upn+downn）
    # 历史 bug: 旧正则 `连(?:续|跌)` 中 `续` 是 `跌` 的同级备选，"连续上涨" 里的"连续"
    # 会让 down 正则也命中，导致 upn AND downn 矛盾条件（AND 下永远 0 命中）。
    def _consecutive_days(direction_terms: tuple[str, ...]) -> int | None:
        """提取“连续N天<方向>”或“<方向>N天”的天数；未出现方向词则返回 None。"""
        for term in direction_terms:
            if term not in q:
                continue
            # 优先匹配“连续/连 N 天 ... 方向”或“方向 ... N 天”，宽松取相邻数字
            m = re.search(r'连(?:续)?\s*(\d+)\s*(?:天|日|个交易日)', q)
            if m:
                return int(m.group(1))
            m2 = re.search(r'(\d+)\s*(?:天|日|个交易日)', q)
            if m2:
                return int(m2.group(1))
            return 3  # 出现方向词但未给天数，默认 3
        return None

    up_n = _consecutive_days(('上涨', '连涨', '上升', '走高'))
    if up_n is not None and ('连' in q or '上涨' in q or '连涨' in q):
        _append_condition('upn', {'n': up_n})

    down_n = _consecutive_days(('下跌', '连跌', '下降', '走低'))
    if down_n is not None and ('连' in q or '下跌' in q or '连跌' in q):
        _append_condition('downn', {'n': down_n})

    # 连板天数
    board_match = re.search(r'(\d+)\s*连板', q)
    if board_match:
        _append_condition('continuous_limit_up', {'n': int(board_match.group(1))})

    # 价格与均线关系
    ma_above_match = re.search(r'(?:站上|突破|上穿)\s*(\d+)\s*日?均线', q)
    if ma_above_match:
        _append_condition('price_above_ma', {'n': int(ma_above_match.group(1))})

    ma_below_match = re.search(r'(?:跌破|失守|下穿)\s*(\d+)\s*日?均线', q)
    if ma_below_match:
        _append_condition('price_below_ma', {'n': int(ma_below_match.group(1))})

    # 均线交叉
    ma_golden_match = re.search(r'(\d+)\s*日均线\s*(?:上穿|金叉)\s*(\d+)\s*日均线', q)
    if ma_golden_match:
        _append_condition(
            'golden_cross_ma',
            {'short': int(ma_golden_match.group(1)), 'long': int(ma_golden_match.group(2))},
        )

    ma_death_match = re.search(r'(\d+)\s*日均线\s*(?:下穿|死叉)\s*(\d+)\s*日均线', q)
    if ma_death_match:
        _append_condition(
            'death_cross_ma',
            {'short': int(ma_death_match.group(1)), 'long': int(ma_death_match.group(2))},
        )

    rsi_cmp_match = re.search(r'(?:rsi|相对强弱指标)\s*(?:低于|小于|<|＜|不高于|<=|≤)\s*(\d+\.?\d*)', q)
    if rsi_cmp_match:
        _append_condition('rsi_below', {'threshold': float(rsi_cmp_match.group(1))})

    rsi_gt_match = re.search(r'(?:rsi|相对强弱指标)\s*(?:高于|大于|>|＞|不低于|>=|≥)\s*(\d+\.?\d*)', q)
    if rsi_gt_match:
        _append_condition('rsi_above', {'threshold': float(rsi_gt_match.group(1))})

    # 关键词匹配
    for keyword, cond_id in KEYWORD_MAP.items():
        if keyword in q:
            _append_condition(cond_id)


def _build_suggestion(fundamental, technical, logic, semantic=None):
    """根据解析结果生成调用建议"""
    semantic = semantic or []
    if fundamental and technical:
        return (
            f"建议使用 screener_manager(action='combined_screen', "
            f"fundamental_criteria={{{_fmt_fund(fundamental)}}}, "
            f"tech_conditions={_fmt_tech(technical)}, "
            f"logic='{logic}')"
        )
    elif technical:
        return (
            f"建议使用 screener_manager(action='technical_screen', "
            f"conditions={_fmt_tech(technical)}, "
            f"logic='{logic}')"
        )
    elif fundamental:
        return (
            f"建议使用 screener_manager(action='screen', "
            f"criteria={{{_fmt_fund(fundamental)}}})"
        )
    elif semantic:
        return "建议先使用 semantic_stock_search 或 screener_manager 的 sectors 条件缩小股票池"
    else:
        return "未能解析出有效条件，请尝试更具体的描述"


def _fmt_fund(conditions):
    """格式化基本面条件为 criteria 参数"""
    mapping = {
        'pe_ratio': {'<': 'max_pe', '>': 'min_pe'},
        'pb_ratio': {'<': 'max_pb', '>': 'min_pb'},
        'roe': {'>': 'min_roe', '<': 'max_roe'},
        'debt_ratio': {'<': 'max_debt_ratio'},
        'revenue_growth': {'>': 'min_revenue_growth'},
        'market_cap': {'>': 'min_market_cap', '<': 'max_market_cap'},
        'dividend_yield': {'>': 'min_dividend_yield'},
    }
    parts = []
    for c in conditions:
        field_map = mapping.get(c['field'], {})
        key = field_map.get(c['operator'])
        if key:
            parts.append(f"'{key}': {c['value']}")
    return ', '.join(parts)


def _fmt_tech(conditions):
    """格式化技术条件，保留参数化条件。"""
    formatted = []
    for item in conditions:
        if not isinstance(item, dict):
            continue
        condition_id = item.get('id')
        params = item.get('params')
        if params:
            formatted.append({'id': condition_id, 'params': params})
        else:
            formatted.append(condition_id)
    return repr(formatted)
