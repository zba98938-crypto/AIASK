"""
选股引擎 - 条件注册、评估、批量扫描

设计原则：
1. 条件注册表模式：每个条件是独立函数，通过装饰器注册
2. 条件可组合：支持 AND/OR 逻辑组合
3. 批量扫描：对股票池并发获取数据并评估
4. 数据复用：同一股票的K线数据只获取一次，多条件共享
"""

import logging
from typing import Callable, Optional
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ScreenCondition:
    """选股条件定义"""
    id: str
    name: str
    category: str
    description: str
    func: Callable
    default_params: dict = field(default_factory=dict)
    requires_volume: bool = False
    min_klines: int = 1


class ScreenEngine:
    """选股引擎"""

    def __init__(self):
        self._conditions: dict[str, ScreenCondition] = {}
        self._composites: dict[str, dict] = {}

    def register(self, id: str, name: str, category: str,
                 description: str = "", default_params: dict = None,
                 requires_volume: bool = False, min_klines: int = 1):
        """装饰器：注册选股条件"""
        def decorator(func: Callable):
            self._conditions[id] = ScreenCondition(
                id=id, name=name, category=category,
                description=description, func=func,
                default_params=default_params or {},
                requires_volume=requires_volume,
                min_klines=min_klines
            )
            return func
        return decorator

    def register_composite(self, id: str, name: str, description: str,
                           conditions: list, logic: str = "AND"):
        """注册组合策略"""
        self._composites[id] = {
            'name': name, 'description': description,
            'conditions': conditions, 'logic': logic
        }

    def evaluate(self, condition_id: str, klines: list, params: dict = None) -> bool:
        """评估单个条件"""
        if condition_id in self._composites:
            return self._evaluate_composite(condition_id, klines, params)

        cond = self._conditions.get(condition_id)
        if not cond:
            logger.warning(f"Unknown condition: {condition_id}")
            return False

        if len(klines) < cond.min_klines:
            return False

        merged_params = {**cond.default_params, **(params or {})}
        try:
            return bool(cond.func(klines, merged_params))
        except Exception as e:
            logger.error(f"Condition {condition_id} error: {e}")
            return False

    def _evaluate_composite(self, composite_id: str, klines: list,
                            params: dict = None) -> bool:
        """评估组合条件"""
        comp = self._composites[composite_id]
        logic = comp['logic'].upper()

        results = []
        for cond_def in comp['conditions']:
            cid = cond_def['id']
            cparams = {**cond_def.get('params', {}), **(params or {})}
            results.append(self.evaluate(cid, klines, cparams))

        return all(results) if logic == "AND" else any(results)

    def evaluate_multi(self, condition_ids: list, klines: list,
                       logic: str = "AND", params: dict = None) -> dict:
        """评估多个条件的组合

        Args:
            condition_ids: 条件列表，支持两种格式：
                - 字符串列表: ['upn', 'ma_bull']（使用全局 params）
                - 字典列表: [{'id': 'upn', 'params': {'n': 3}}, ...]（每个条件独立参数）
            klines: K线数据
            logic: 组合逻辑 AND/OR
            params: 全局参数（仅字符串列表模式使用）

        Returns:
            {'match': bool, 'details': {condition_id: bool, ...}}
        """
        details = {}
        for cid in condition_ids:
            if isinstance(cid, dict):
                cond_id = cid['id']
                cond_params = cid.get('params', params)
                details[cond_id] = self.evaluate(cond_id, klines, cond_params)
            else:
                details[cid] = self.evaluate(cid, klines, params)

        results = list(details.values())
        match = all(results) if logic.upper() == "AND" else any(results)
        return {'match': match, 'details': details}

    def scan(self, stock_pool: list, condition_ids: list,
             logic: str = "AND", params: dict = None) -> list:
        """
        批量扫描股票池

        Args:
            stock_pool: [{'code': '600519', 'name': '贵州茅台', 'klines': [...]}]
            condition_ids: 条件ID列表
            logic: 组合逻辑 AND/OR
            params: 全局参数

        Returns:
            匹配的股票列表
        """
        matched = []
        for stock in stock_pool:
            klines = stock.get('klines', [])
            if not klines:
                continue

            if self.evaluate_multi(condition_ids, klines, logic, params)['match']:
                matched.append({
                    'code': stock['code'],
                    'name': stock.get('name', ''),
                    'close': klines[-1].get('close', 0),
                    'change_pct': _calc_change_pct(klines),
                    'volume_ratio': _calc_volume_ratio(klines),
                    'matched_conditions': condition_ids,
                })

        return matched

    def list_conditions(self, category: str = None) -> list:
        """列出所有可用条件"""
        result = []
        for cid, cond in self._conditions.items():
            if category and cond.category != category:
                continue
            result.append({
                'id': cid, 'name': cond.name,
                'category': cond.category,
                'description': cond.description,
                'default_params': cond.default_params,
            })
        for cid, comp in self._composites.items():
            if category and category != 'composite':
                continue
            result.append({
                'id': cid, 'name': comp['name'],
                'category': 'composite',
                'description': comp['description'],
                'sub_conditions': [c['id'] for c in comp['conditions']],
            })
        return result

    def list_categories(self) -> list:
        """列出条件分类"""
        cats = {}
        for cond in self._conditions.values():
            cats[cond.category] = cats.get(cond.category, 0) + 1
        cats['composite'] = len(self._composites)

        CATEGORY_NAMES = {
            'trend': '趋势类', 'indicator': '技术指标类',
            'volume': '量价关系类', 'pattern': 'K线形态类',
            'astock': 'A股特色', 'composite': '组合策略',
        }
        return [
            {'id': k, 'name': CATEGORY_NAMES.get(k, k), 'count': v}
            for k, v in cats.items()
        ]


def _calc_change_pct(klines: list) -> float:
    if len(klines) < 2:
        return 0
    prev = klines[-2].get('close', 0)
    curr = klines[-1].get('close', 0)
    return round((curr - prev) / prev * 100, 2) if prev else 0


def _calc_volume_ratio(klines: list) -> float:
    if len(klines) < 6:
        return 0
    today_vol = klines[-1].get('volume', 0) or 0
    avg_vol = sum(k.get('volume', 0) or 0 for k in klines[-6:-1]) / 5
    return round(today_vol / avg_vol, 2) if avg_vol else 0


# 全局引擎实例
engine = ScreenEngine()
