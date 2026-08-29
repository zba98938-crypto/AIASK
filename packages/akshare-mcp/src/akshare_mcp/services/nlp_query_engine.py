"""
NLP查询引擎 - 自然语言查询解析
将自然语言转换为结构化查询
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta


class NLPQueryEngine:
    """NLP查询引擎"""
    
    def __init__(self):
        # 关键词映射
        self.action_keywords = {
            'query': ['查询', '查看', '显示', '获取', '找', '搜索', '看看'],
            'analyze': ['分析', '评估', '研究', '诊断', '判断'],
            'compare': ['对比', '比较', '对照'],
            'backtest': ['回测', '测试', '验证'],
            'screen': ['选股', '筛选', '过滤'],
            'recommend': ['推荐', '建议', '推荐股票'],
        }
        
        self.entity_keywords = {
            'stock': ['股票', '个股', '证券'],
            'sector': ['板块', '行业', '概念'],
            'index': ['指数', '大盘'],
            'factor': ['因子', '指标'],
            'strategy': ['策略', '方法'],
        }
        
        self.indicator_keywords = {
            'price': ['价格', '股价', '收盘价'],
            'volume': ['成交量', '量'],
            'pe': ['市盈率', 'PE'],
            'pb': ['市净率', 'PB'],
            'roe': ['ROE', '净资产收益率'],
            'revenue': ['营收', '收入'],
            'profit': ['利润', '净利润'],
            'market_cap': ['市值', '总市值'],
        }
        
        self.time_keywords = {
            'today': ['今天', '今日'],
            'yesterday': ['昨天', '昨日'],
            'week': ['本周', '这周', '一周'],
            'month': ['本月', '这月', '一个月'],
            'year': ['今年', '本年', '一年'],
        }
        
        self.comparison_keywords = {
            'greater': ['大于', '高于', '超过', '多于', '>'],
            'less': ['小于', '低于', '少于', '<'],
            'equal': ['等于', '=', '是'],
            'between': ['之间', '介于'],
        }
    
    # ========== 意图识别 ==========
    
    def detect_intent(self, query: str) -> str:
        """
        识别查询意图
        
        Returns:
            意图类型: query, analyze, compare, backtest, screen, recommend
        """
        query_lower = query.lower()
        
        for intent, keywords in self.action_keywords.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return intent
        
        return 'query'  # 默认为查询
    
    # ========== 实体识别 ==========
    
    def extract_entities(self, query: str) -> Dict[str, Any]:
        """
        提取查询中的实体
        
        Returns:
            实体字典
        """
        entities = {
            'codes': [],
            'sectors': [],
            'indicators': [],
            'time_range': None,
            'conditions': [],
        }
        
        # 提取股票代码（6位数字）
        # 使用“非数字边界”而非 \b，兼容中文连写场景（如：查询000001和000002的价格）
        code_pattern = r'(?<!\d)\d{6}(?!\d)'
        codes = re.findall(code_pattern, query)
        entities['codes'] = codes
        
        # 提取股票名称（简化处理）
        # 实际应该查询数据库匹配
        
        # 提取指标
        for indicator, keywords in self.indicator_keywords.items():
            for keyword in keywords:
                if keyword in query:
                    entities['indicators'].append(indicator)
        
        # 提取时间范围
        entities['time_range'] = self._extract_time_range(query)
        
        # 提取条件
        entities['conditions'] = self._extract_conditions(query)
        
        return entities
    
    def _extract_time_range(self, query: str) -> Optional[Dict[str, str]]:
        """提取时间范围"""
        today = datetime.now()
        
        for period, keywords in self.time_keywords.items():
            for keyword in keywords:
                if keyword in query:
                    if period == 'today':
                        return {
                            'start': today.strftime('%Y-%m-%d'),
                            'end': today.strftime('%Y-%m-%d'),
                        }
                    elif period == 'yesterday':
                        yesterday = today - timedelta(days=1)
                        return {
                            'start': yesterday.strftime('%Y-%m-%d'),
                            'end': yesterday.strftime('%Y-%m-%d'),
                        }
                    elif period == 'week':
                        week_ago = today - timedelta(days=7)
                        return {
                            'start': week_ago.strftime('%Y-%m-%d'),
                            'end': today.strftime('%Y-%m-%d'),
                        }
                    elif period == 'month':
                        month_ago = today - timedelta(days=30)
                        return {
                            'start': month_ago.strftime('%Y-%m-%d'),
                            'end': today.strftime('%Y-%m-%d'),
                        }
                    elif period == 'year':
                        year_ago = today - timedelta(days=365)
                        return {
                            'start': year_ago.strftime('%Y-%m-%d'),
                            'end': today.strftime('%Y-%m-%d'),
                        }
        
        # 提取具体日期（YYYY-MM-DD格式）
        date_pattern = r'\d{4}-\d{2}-\d{2}'
        dates = re.findall(date_pattern, query)
        if len(dates) >= 2:
            return {'start': dates[0], 'end': dates[1]}
        elif len(dates) == 1:
            return {'start': dates[0], 'end': dates[0]}
        
        return None
    
    def _extract_conditions(self, query: str) -> List[Dict[str, Any]]:
        """提取查询条件"""
        conditions = []
        
        # 提取数值条件
        # 例如: "市盈率小于30", "ROE大于10%"
        
        for indicator, keywords in self.indicator_keywords.items():
            for keyword in keywords:
                if keyword in query:
                    # 查找比较运算符和数值
                    for comp_type, comp_keywords in self.comparison_keywords.items():
                        for comp_keyword in comp_keywords:
                            pattern = f'{keyword}.*?{comp_keyword}.*?(\\d+\\.?\\d*)'
                            match = re.search(pattern, query)
                            if match:
                                value = float(match.group(1))
                                conditions.append({
                                    'indicator': indicator,
                                    'operator': comp_type,
                                    'value': value,
                                })
        
        return conditions
    
    # ========== 查询解析 ==========
    
    def parse_query(self, query: str) -> Dict[str, Any]:
        """
        解析自然语言查询
        
        Args:
            query: 自然语言查询
        
        Returns:
            结构化查询
        """
        intent = self.detect_intent(query)
        entities = self.extract_entities(query)
        
        parsed = {
            'intent': intent,
            'entities': entities,
            'original_query': query,
        }
        
        # 根据意图生成具体的查询参数
        if intent == 'query':
            parsed['action'] = self._build_query_action(entities)
        elif intent == 'analyze':
            parsed['action'] = self._build_analyze_action(entities)
        elif intent == 'screen':
            parsed['action'] = self._build_screen_action(entities)
        elif intent == 'backtest':
            parsed['action'] = self._build_backtest_action(entities)
        
        return parsed
    
    def _build_query_action(self, entities: Dict[str, Any]) -> Dict[str, Any]:
        """构建查询动作"""
        action = {
            'type': 'query',
            'params': {}
        }
        
        if entities['codes']:
            action['params']['codes'] = entities['codes']
        
        if entities['time_range']:
            action['params']['time_range'] = entities['time_range']
        
        if entities['indicators']:
            action['params']['indicators'] = entities['indicators']
        
        return action
    
    def _build_analyze_action(self, entities: Dict[str, Any]) -> Dict[str, Any]:
        """构建分析动作"""
        action = {
            'type': 'analyze',
            'params': {
                'codes': entities['codes'],
                'indicators': entities['indicators'] or ['all'],
            }
        }
        return action
    
    def _build_screen_action(self, entities: Dict[str, Any]) -> Dict[str, Any]:
        """构建选股动作"""
        action = {
            'type': 'screen',
            'params': {
                'conditions': entities['conditions'],
            }
        }
        return action
    
    def _build_backtest_action(self, entities: Dict[str, Any]) -> Dict[str, Any]:
        """构建回测动作"""
        action = {
            'type': 'backtest',
            'params': {
                'codes': entities['codes'],
                'time_range': entities['time_range'],
            }
        }
        return action
    
    # ========== 智能诊断 ==========
    
    def diagnose_stock(self, query: str, stock_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        智能诊断股票
        
        Args:
            query: 诊断问题
            stock_data: 股票数据
        
        Returns:
            诊断结果
        """
        diagnosis = {
            'query': query,
            'code': stock_data.get('code'),
            'issues': [],
            'suggestions': [],
            'score': 0,
        }
        
        # 技术面诊断
        if 'technical' in stock_data:
            tech = stock_data['technical']
            
            # 趋势诊断
            if tech.get('trend') == 'down':
                diagnosis['issues'].append('技术面：下降趋势')
                diagnosis['suggestions'].append('建议：等待趋势反转信号')
            
            # 超买超卖
            if tech.get('rsi', 50) > 70:
                diagnosis['issues'].append('技术面：RSI超买')
                diagnosis['suggestions'].append('建议：注意回调风险')
            elif tech.get('rsi', 50) < 30:
                diagnosis['issues'].append('技术面：RSI超卖')
                diagnosis['suggestions'].append('建议：可能存在反弹机会')
        
        # 基本面诊断
        if 'fundamental' in stock_data:
            fund = stock_data['fundamental']
            
            # 估值诊断
            pe = fund.get('pe', 0)
            if pe > 50:
                diagnosis['issues'].append('基本面：估值偏高')
                diagnosis['suggestions'].append('建议：关注业绩增长是否匹配估值')
            elif pe < 10 and pe > 0:
                diagnosis['issues'].append('基本面：估值偏低')
                diagnosis['suggestions'].append('建议：可能存在价值投资机会')
            
            # 盈利能力
            roe = fund.get('roe', 0)
            if roe < 5:
                diagnosis['issues'].append('基本面：盈利能力较弱')
                diagnosis['suggestions'].append('建议：关注公司经营改善情况')
        
        # 计算综合评分
        score = 50  # 基础分
        score -= len(diagnosis['issues']) * 10
        score = max(0, min(100, score))
        diagnosis['score'] = score
        
        return diagnosis
    
    # ========== 查询建议 ==========
    
    def suggest_queries(self, context: str = 'general') -> List[str]:
        """
        根据上下文建议查询
        
        Args:
            context: 上下文类型
        
        Returns:
            建议的查询列表
        """
        suggestions = {
            'general': [
                '查询000001的最新价格',
                '分析科技板块的表现',
                '选出市盈率小于30且ROE大于10的股票',
                '回测均线策略在000001上的表现',
                '推荐今天值得关注的股票',
            ],
            'technical': [
                '查询000001的技术指标',
                '分析000001的趋势',
                '找出突破新高的股票',
                '查看RSI超卖的股票',
            ],
            'fundamental': [
                '查询000001的财务数据',
                '分析000001的盈利能力',
                '选出高ROE低PE的股票',
                '对比000001和000002的基本面',
            ],
            'market': [
                '查看今天的涨停股票',
                '分析市场情绪',
                '查询龙虎榜数据',
                '看看资金流向',
            ],
        }
        
        return suggestions.get(context, suggestions['general'])
    
    # ========== 查询优化 ==========
    
    def optimize_query(self, query: str) -> str:
        """
        优化查询语句
        
        Args:
            query: 原始查询
        
        Returns:
            优化后的查询
        """
        # 去除多余空格
        query = ' '.join(query.split())
        
        # 标准化同义词
        synonyms = {
            '看一下': '查询',
            '看看': '查询',
            '帮我': '',
            '请': '',
        }
        
        for old, new in synonyms.items():
            query = query.replace(old, new)
        
        return query.strip()


# 全局实例
nlp_query_engine = NLPQueryEngine()
