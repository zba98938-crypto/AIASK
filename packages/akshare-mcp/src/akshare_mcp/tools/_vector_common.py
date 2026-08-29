"""向量搜索工具 - 基于特征相似度的实现"""

import asyncio
from collections import defaultdict
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from ..storage import get_db
from ..services.factor_calculator import factor_calculator
from ..services import technical_analysis
from ..services.stock_profile_pipeline import build_stock_profile_payload
from ..services.vector_search import vector_search_engine
from .search import _search_stocks_tushare_fallback
from ..utils import ok, fail, suppress_stdout
import statistics
import math


_GENERIC_SEMANTIC_HINTS = {
    '龙头', '概念', '板块', '赛道', '题材', '核心', '精选', '优选', '成长', '价值'
}
_SEMANTIC_CONCEPT_HINTS = {'概念', '板块', '题材', '赛道'}
_SEMANTIC_ST_PREFIXES = ('*st', 'st', 'sst', 's*st', '退市')
_MIN_SEMANTIC_SCORE = 0.45

_SEMANTIC_SECTOR_SEEDS = {
    "白酒": [
        {"code": "600519", "stock_name": "贵州茅台", "industry": "白酒"},
        {"code": "000858", "stock_name": "五粮液", "industry": "白酒"},
        {"code": "002304", "stock_name": "洋河股份", "industry": "白酒"},
    ],
    "酿酒": [
        {"code": "600519", "stock_name": "贵州茅台", "industry": "酿酒"},
        {"code": "000858", "stock_name": "五粮液", "industry": "酿酒"},
        {"code": "002304", "stock_name": "洋河股份", "industry": "酿酒"},
    ],
    "半导体": [
        {"code": "688981", "stock_name": "中芯国际", "industry": "半导体"},
        {"code": "603501", "stock_name": "韦尔股份", "industry": "半导体"},
        {"code": "002371", "stock_name": "北方华创", "industry": "半导体"},
    ],
    "芯片": [
        {"code": "688981", "stock_name": "中芯国际", "industry": "芯片"},
        {"code": "603501", "stock_name": "韦尔股份", "industry": "芯片"},
        {"code": "002371", "stock_name": "北方华创", "industry": "芯片"},
    ],
    "新能源汽车": [
        {"code": "002594", "stock_name": "比亚迪", "industry": "新能源汽车"},
        {"code": "300750", "stock_name": "宁德时代", "industry": "新能源汽车"},
        {"code": "601633", "stock_name": "长城汽车", "industry": "新能源汽车"},
    ],
    "新能源车": [
        {"code": "002594", "stock_name": "比亚迪", "industry": "新能源车"},
        {"code": "300750", "stock_name": "宁德时代", "industry": "新能源车"},
        {"code": "601633", "stock_name": "长城汽车", "industry": "新能源车"},
    ],
}

_GENERIC_SEMANTIC_HINTS.update({
    "\u9f99\u5934",
    "\u6982\u5ff5",
    "\u677f\u5757",
    "\u8d5b\u9053",
    "\u9898\u6750",
    "\u6838\u5fc3",
    "\u7cbe\u9009",
    "\u4f18\u9009",
    "\u6210\u957f",
    "\u4ef7\u503c",
})
_SEMANTIC_CONCEPT_HINTS.update({
    "\u6982\u5ff5",
    "\u677f\u5757",
    "\u9898\u6750",
    "\u8d5b\u9053",
})
_SEMANTIC_ST_PREFIXES = tuple(dict.fromkeys((*_SEMANTIC_ST_PREFIXES, "\u9000\u5e02")))
_SEMANTIC_SECTOR_SEEDS.update({
    "\u767d\u9152": [
        {"code": "600519", "stock_name": "\u8d35\u5dde\u8305\u53f0", "industry": "\u767d\u9152"},
        {"code": "000858", "stock_name": "\u4e94\u7cae\u6db2", "industry": "\u767d\u9152"},
        {"code": "002304", "stock_name": "\u6d0b\u6cb3\u80a1\u4efd", "industry": "\u767d\u9152"},
    ],
    "\u917f\u9152": [
        {"code": "600519", "stock_name": "\u8d35\u5dde\u8305\u53f0", "industry": "\u917f\u9152"},
        {"code": "000858", "stock_name": "\u4e94\u7cae\u6db2", "industry": "\u917f\u9152"},
        {"code": "002304", "stock_name": "\u6d0b\u6cb3\u80a1\u4efd", "industry": "\u917f\u9152"},
    ],
    "\u534a\u5bfc\u4f53": [
        {"code": "688981", "stock_name": "\u4e2d\u82af\u56fd\u9645", "industry": "\u534a\u5bfc\u4f53"},
        {"code": "603501", "stock_name": "\u97e6\u5c14\u80a1\u4efd", "industry": "\u534a\u5bfc\u4f53"},
        {"code": "002371", "stock_name": "\u5317\u65b9\u534e\u521b", "industry": "\u534a\u5bfc\u4f53"},
    ],
    "\u82af\u7247": [
        {"code": "688981", "stock_name": "\u4e2d\u82af\u56fd\u9645", "industry": "\u82af\u7247"},
        {"code": "603501", "stock_name": "\u97e6\u5c14\u80a1\u4efd", "industry": "\u82af\u7247"},
        {"code": "002371", "stock_name": "\u5317\u65b9\u534e\u521b", "industry": "\u82af\u7247"},
    ],
    "\u65b0\u80fd\u6e90\u6c7d\u8f66": [
        {"code": "002594", "stock_name": "\u6bd4\u4e9a\u8fea", "industry": "\u65b0\u80fd\u6e90\u6c7d\u8f66"},
        {"code": "300750", "stock_name": "\u5b81\u5fb7\u65f6\u4ee3", "industry": "\u65b0\u80fd\u6e90\u6c7d\u8f66"},
        {"code": "601633", "stock_name": "\u957f\u57ce\u6c7d\u8f66", "industry": "\u65b0\u80fd\u6e90\u6c7d\u8f66"},
    ],
    "\u65b0\u80fd\u6e90\u8f66": [
        {"code": "002594", "stock_name": "\u6bd4\u4e9a\u8fea", "industry": "\u65b0\u80fd\u6e90\u8f66"},
        {"code": "300750", "stock_name": "\u5b81\u5fb7\u65f6\u4ee3", "industry": "\u65b0\u80fd\u6e90\u8f66"},
        {"code": "601633", "stock_name": "\u957f\u57ce\u6c7d\u8f66", "industry": "\u65b0\u80fd\u6e90\u8f66"},
    ],
})


def _semantic_is_special_treatment(name: str) -> bool:
    text = str(name or '').strip().lower().replace(' ', '')
    return any(text.startswith(prefix) for prefix in _SEMANTIC_ST_PREFIXES)


def _semantic_is_concept_industry(industry: str) -> bool:
    text = str(industry or '').strip().lower()
    return any(hint in text for hint in _SEMANTIC_CONCEPT_HINTS)


def _normalize_stock_row(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(row or {})
    code = str(payload.get('code') or payload.get('stock_code') or '').strip()
    return {
        'code': code,
        'stock_name': payload.get('stock_name') or payload.get('name') or '',
        'industry': payload.get('industry') or payload.get('sector') or '',
        'pe_ratio': payload.get('pe_ratio'),
        'pb_ratio': payload.get('pb_ratio'),
        'market_cap': payload.get('market_cap'),
    }


async def _load_candidate_stock_rows(db, target_code: str, target_industry: str, limit: int = 100) -> tuple[str, List[Dict[str, Any]]]:
    candidate_scope = 'industry' if target_industry else 'market'

    if hasattr(db, 'list_stock_universe'):
        try:
            rows: List[Dict[str, Any]] = []
            if target_industry:
                rows = [_normalize_stock_row(row) for row in await db.list_stock_universe(limit=limit, industry=target_industry)]
                rows = [row for row in rows if row['code'] and row['code'] != target_code]
            if not rows:
                candidate_scope = 'market'
                rows = [_normalize_stock_row(row) for row in await db.list_stock_universe(limit=limit)]
                rows = [row for row in rows if row['code'] and row['code'] != target_code]
            if rows:
                return candidate_scope, rows[:limit]
        except Exception:
            pass

    if not hasattr(db, 'acquire'):
        return candidate_scope, []

    async with db.acquire() as conn:
        rows = []
        if target_industry:
            rows = await conn.fetch(
                """SELECT code, stock_name, industry, pe_ratio, pb_ratio, market_cap FROM stocks
                   WHERE industry = $1 AND code != $2
                   LIMIT 100""",
                target_industry, target_code
            )
        if not rows:
            candidate_scope = 'market'
            rows = await conn.fetch(
                """SELECT code, stock_name, industry, pe_ratio, pb_ratio, market_cap FROM stocks
                   WHERE code != $1
                   LIMIT 100""",
                target_code
            )
        return candidate_scope, [_normalize_stock_row(dict(row)) for row in rows]


async def _fetch_table_columns(conn, table_name: str) -> set[str]:
    try:
        rows = await conn.fetch(
            "SELECT name AS column_name FROM pragma_table_info($1)",
            table_name,
        )
        names = set()
        for row in rows:
            payload = dict(row)
            name = str(payload.get('column_name') or '').strip()
            if name:
                names.add(name)
        return names
    except Exception:
        return set()


async def _fetch_latest_financial_map(conn, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    if not codes:
        return {}
    try:
        cols = await _fetch_table_columns(conn, 'financials')
        code_col = 'stock_code' if 'stock_code' in cols else 'code'
        rows = await conn.fetch(
            f"""
            SELECT code, roe, debt_ratio, revenue_growth
            FROM (
                SELECT {code_col} AS code,
                       roe,
                       debt_ratio,
                       revenue_growth,
                       ROW_NUMBER() OVER (PARTITION BY {code_col} ORDER BY report_date DESC) AS rn
                FROM financials
                WHERE {code_col} IN ($1)
            ) ranked
            WHERE rn = 1
            """,
            list(dict.fromkeys(codes)),
        )
    except Exception:
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        code = str(payload.get('code') or '').strip()
        if not code:
            continue
        result[code] = payload
    return result


async def _fetch_kline_batch(conn, codes: List[str], limit: int) -> Dict[str, List[Dict[str, Any]]]:
    if not codes or limit <= 0:
        return {}
    try:
        rows = await conn.fetch(
            """
            SELECT code, time, open, high, low, close, volume, amount, turnover, change_pct
            FROM (
                SELECT
                    code, time, open, high, low, close, volume, amount, turnover, change_pct,
                    ROW_NUMBER() OVER (PARTITION BY code ORDER BY time DESC) AS rn
                FROM kline_1d
                WHERE code IN ($1)
            ) ranked
            WHERE rn <= $2
            ORDER BY code, time ASC
            """,
            list(dict.fromkeys(codes)),
            int(limit),
        )
    except Exception:
        return {}

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = dict(row)
        code = str(payload.get('code') or '').strip()
        time_value = payload.get('time')
        if not code or time_value is None:
            continue
        grouped[code].append({
            'date': time_value.strftime('%Y-%m-%d') if hasattr(time_value, 'strftime') else str(time_value),
            'code': code,
            'open': float(payload['open']) if payload.get('open') is not None else None,
            'high': float(payload['high']) if payload.get('high') is not None else None,
            'low': float(payload['low']) if payload.get('low') is not None else None,
            'close': float(payload['close']) if payload.get('close') is not None else None,
            'volume': int(payload['volume']) if payload.get('volume') is not None else 0,
            'amount': float(payload['amount']) if payload.get('amount') is not None else None,
            'turnover': float(payload['turnover']) if payload.get('turnover') is not None else None,
            'change_pct': float(payload['change_pct']) if payload.get('change_pct') is not None else None,
            'source': 'sqlite',
        })
    return grouped


async def _prefetch_candidate_context(
    db,
    candidate_rows: List[Dict[str, Any]],
    *,
    need_financials: bool,
    need_klines: bool,
    kline_limit: int,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    candidate_codes = [str(row.get('code') or '').strip() for row in candidate_rows if str(row.get('code') or '').strip()]
    financial_map: Dict[str, Dict[str, Any]] = {}
    kline_map: Dict[str, List[Dict[str, Any]]] = {}

    if hasattr(db, 'acquire') and candidate_codes and (need_financials or need_klines):
        try:
            async with db.acquire() as conn:
                if need_financials:
                    financial_map = await _fetch_latest_financial_map(conn, candidate_codes)
                if need_klines:
                    kline_map = await _fetch_kline_batch(conn, candidate_codes, kline_limit)
        except Exception:
            financial_map = {}
            kline_map = {}

    if need_financials:
        missing_financial_codes = [code for code in candidate_codes if code not in financial_map]
        if missing_financial_codes:
            async def _load_financial(code: str):
                try:
                    rows = await db.get_financials(code, limit=1)
                    return code, (rows[0] if rows else None)
                except Exception:
                    return code, None

            for code, payload in await asyncio.gather(*[_load_financial(code) for code in missing_financial_codes]):
                if payload:
                    financial_map[code] = dict(payload)

    if need_klines:
        missing_kline_codes = [code for code in candidate_codes if code not in kline_map]
        if missing_kline_codes:
            async def _load_klines(code: str):
                try:
                    rows = await db.get_klines(code, limit=kline_limit)
                    return code, rows if rows and len(rows) >= min(20, kline_limit) else None
                except Exception:
                    return code, None

            for code, payload in await asyncio.gather(*[_load_klines(code) for code in missing_kline_codes]):
                if payload:
                    kline_map[code] = payload

    return financial_map, kline_map


async def _fill_missing_candidate_stock_info(db, candidate_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    missing_codes = [
        str(row.get('code') or '').strip()
        for row in candidate_rows
        if str(row.get('code') or '').strip() and (row.get('pe_ratio') is None or row.get('pb_ratio') is None or not row.get('industry'))
    ]
    if not missing_codes:
        return {}

    async def _load(code: str):
        try:
            return code, (await db.get_stock_info(code) or {})
        except Exception:
            return code, {}

    filled: Dict[str, Dict[str, Any]] = {}
    for code, payload in await asyncio.gather(*[_load(code) for code in list(dict.fromkeys(missing_codes))]):
        if payload:
            filled[code] = dict(payload)
    return filled

def _extract_technical_features(klines: List[Dict[str, Any]]) -> Dict[str, float]:
    if not klines or len(klines) < 20:
        return {}
    closes = [float(k['close']) for k in klines if k.get('close') is not None]
    if len(closes) < 20:
        return {}
    recent_closes = closes[-20:]
    features: Dict[str, float] = {
        'momentum': factor_calculator.calculate_momentum(recent_closes),
        'volatility': factor_calculator.calculate_volatility(recent_closes),
    }
    ma20 = technical_analysis.calculate_sma(closes, 20)
    if ma20 and len(ma20) > 0 and ma20[-1]:
        features['trend'] = (closes[-1] - ma20[-1]) / ma20[-1]
    return features


def _build_target_features(
    target_info: Dict[str, Any],
    financial_row: Optional[Dict[str, Any]],
    target_klines: Optional[List[Dict[str, Any]]],
    similarity_type: str,
) -> Dict[str, float]:
    target_features: Dict[str, float] = {}
    if similarity_type in ['fundamental', 'both']:
        if financial_row:
            target_features['roe'] = financial_row.get('roe', 0)
            target_features['debt_ratio'] = financial_row.get('debt_ratio', 0)
            target_features['revenue_growth'] = financial_row.get('revenue_growth', 0)
        target_features['pe'] = target_info.get('pe_ratio', 0)
        target_features['pb'] = target_info.get('pb_ratio', 0)
    if similarity_type in ['technical', 'both']:
        target_features.update(_extract_technical_features(target_klines or []))
    return target_features


def _build_candidate_features(
    candidate_row: Dict[str, Any],
    filled_info: Optional[Dict[str, Any]],
    financial_row: Optional[Dict[str, Any]],
    klines: Optional[List[Dict[str, Any]]],
    similarity_type: str,
) -> Dict[str, float]:
    features: Dict[str, float] = {}
    info = dict(candidate_row or {})
    info.update({k: v for k, v in dict(filled_info or {}).items() if v is not None})
    if similarity_type in ['fundamental', 'both']:
        if financial_row:
            features['roe'] = financial_row.get('roe', 0)
            features['debt_ratio'] = financial_row.get('debt_ratio', 0)
            features['revenue_growth'] = financial_row.get('revenue_growth', 0)
        features['pe'] = info.get('pe_ratio', 0)
        features['pb'] = info.get('pb_ratio', 0)
    if similarity_type in ['technical', 'both']:
        features.update(_extract_technical_features(klines or []))
    return features


def _compute_feature_scales(target_features: Dict[str, float], candidate_payloads: List[Dict[str, Any]]) -> Dict[str, float]:
    scales: Dict[str, float] = {}
    feature_names = set(target_features.keys())
    for payload in candidate_payloads:
        feature_names.update(dict(payload.get('features') or {}).keys())

    for feature in feature_names:
        values = []
        if feature in target_features:
            values.append(float(target_features[feature] or 0))
        for payload in candidate_payloads:
            features = dict(payload.get('features') or {})
            if feature in features:
                values.append(float(features[feature] or 0))
        if not values:
            continue
        try:
            scale = statistics.pstdev(values) if len(values) > 1 else 0.0
        except statistics.StatisticsError:
            scale = 0.0
        if scale <= 1e-9:
            span = max(values) - min(values) if len(values) > 1 else 0.0
            scale = span if span > 1e-9 else max(max(abs(value) for value in values), 1.0)
        scales[feature] = float(scale or 1.0)
    return scales


def _normalize_kline_vector(values: List[float]) -> List[float]:
    resolved = [float(item) for item in list(values or [])]
    if not resolved:
        return []
    norm = math.sqrt(sum(item * item for item in resolved))
    if norm <= 0:
        return resolved
    return [item / norm for item in resolved]


def _coerce_date_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value or "").strip()
    return text[:10] if text else ""


__all__ = [name for name in globals() if not name.startswith("__")]
