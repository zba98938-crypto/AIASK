"""
向量搜索服务 - K线形态相似度搜索
当前默认使用 Python / 进程内 index 后端；
策略工厂等持久化路径可在 SQLite 启用 sqlite_python 后走数据库向量检索。

索引持久化: 设置 VECTOR_SEARCH_CACHE_DIR 后，build_index 会将索引写入磁盘,
重启后自动加载，避免每次冷启动重算。
"""

import json as _json
import logging
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from scipy.spatial.distance import cosine, euclidean
from sklearn.preprocessing import StandardScaler
import hashlib
import os

_vs_logger = logging.getLogger(__name__)


class VectorSearchEngine:
    """向量搜索引擎"""
    
    def __init__(self, backend: Optional[str] = None, allow_fallback: Optional[bool] = None):
        self.scaler = StandardScaler()
        self.pattern_cache = {}
        self._index_matrix: Optional[np.ndarray] = None
        self._index_codes: list[str] = []
        self._index_norms: Optional[np.ndarray] = None
        self._index_signature: Optional[str] = None
        self._index_reused_last_build = False
        self._index_build_ms_last = 0.0
        env_backend = os.getenv('VECTOR_SEARCH_BACKEND', 'python').strip().lower()
        self.backend = (backend or env_backend or 'python').strip().lower()
        if self.backend not in {'python', 'index'}:
            self.backend = 'python'

        if allow_fallback is None:
            allow_fallback = os.getenv('VECTOR_SEARCH_ALLOW_FALLBACK', 'true').strip().lower() not in {
                '0', 'false', 'no'
            }
        self.allow_fallback = bool(allow_fallback)
        self.last_backend_used = self.backend
        self.last_meta: Dict[str, Any] = {
            'backend_requested': self.backend,
            'backend_used': self.backend,
            'fallback_used': False,
            'fallback_reason': None,
            'latency_ms': 0.0,
            'candidate_count': 0,
        }

    def set_backend(self, backend: str, allow_fallback: Optional[bool] = None) -> None:
        """设置默认检索后端。"""
        backend = (backend or 'python').strip().lower()
        if backend not in {'python', 'index'}:
            raise ValueError("backend must be 'python' or 'index'")
        self.backend = backend
        if allow_fallback is not None:
            self.allow_fallback = bool(allow_fallback)
    
    # ========== K线形态向量化 ==========
    
    @staticmethod
    def kline_to_vector(klines: List[Dict[str, Any]], method: str = 'price_volume') -> np.ndarray:
        """
        将K线数据转换为向量
        
        Args:
            klines: K线数据列表
            method: 向量化方法
                - 'price_volume': 价格+成交量
                - 'ohlc': 开高低收
                - 'returns': 收益率序列
                - 'technical': 技术指标组合
        
        Returns:
            向量表示
        """
        if not klines:
            return np.array([])
        
        if method == 'price_volume':
            # 价格归一化 + 成交量归一化
            closes = np.array([k['close'] for k in klines])
            volumes = np.array([k.get('volume', 0) for k in klines])
            
            # 归一化到[0, 1]
            close_norm = (closes - closes.min()) / (closes.max() - closes.min() + 1e-8)
            volume_norm = (volumes - volumes.min()) / (volumes.max() - volumes.min() + 1e-8)
            
            # 拼接
            vector = np.concatenate([close_norm, volume_norm])
            
        elif method == 'ohlc':
            # OHLC四个价格
            opens = np.array([k.get('open', k['close']) for k in klines])
            highs = np.array([k.get('high', k['close']) for k in klines])
            lows = np.array([k.get('low', k['close']) for k in klines])
            closes = np.array([k['close'] for k in klines])
            
            # 归一化
            all_prices = np.concatenate([opens, highs, lows, closes])
            min_price, max_price = all_prices.min(), all_prices.max()
            
            opens_norm = (opens - min_price) / (max_price - min_price + 1e-8)
            highs_norm = (highs - min_price) / (max_price - min_price + 1e-8)
            lows_norm = (lows - min_price) / (max_price - min_price + 1e-8)
            closes_norm = (closes - min_price) / (max_price - min_price + 1e-8)
            
            vector = np.concatenate([opens_norm, highs_norm, lows_norm, closes_norm])
            
        elif method == 'returns':
            # 收益率序列
            closes = np.array([k['close'] for k in klines])
            returns = np.diff(closes) / closes[:-1]
            
            # 标准化
            returns_norm = (returns - returns.mean()) / (returns.std() + 1e-8)
            vector = returns_norm
            
        elif method == 'technical':
            # 技术指标组合
            closes = np.array([k['close'] for k in klines])
            volumes = np.array([k.get('volume', 0) for k in klines])
            
            # 计算多个技术指标
            features = []
            
            # 1. 价格位置（相对最高最低）
            price_position = (closes[-1] - closes.min()) / (closes.max() - closes.min() + 1e-8)
            features.append(price_position)
            
            # 2. 短期趋势（5日）
            if len(closes) >= 5:
                trend_5 = (closes[-1] - closes[-5]) / closes[-5]
                features.append(trend_5)
            
            # 3. 中期趋势（20日）
            if len(closes) >= 20:
                trend_20 = (closes[-1] - closes[-20]) / closes[-20]
                features.append(trend_20)
            
            # 4. 波动率
            if len(closes) >= 20:
                returns = np.diff(closes[-20:]) / closes[-20:-1]
                volatility = np.std(returns)
                features.append(volatility)
            
            # 5. 成交量比
            if len(volumes) >= 5:
                volume_ratio = volumes[-1] / (np.mean(volumes[-5:]) + 1e-8)
                features.append(volume_ratio)
            
            vector = np.array(features)
        
        else:
            raise ValueError(f"Unknown method: {method}")
        
        return vector
    
    # ========== 相似度计算 ==========
    
    @staticmethod
    def calculate_similarity(
        vector1: np.ndarray,
        vector2: np.ndarray,
        metric: str = 'cosine'
    ) -> float:
        """
        计算两个向量的相似度
        
        Args:
            vector1: 向量1
            vector2: 向量2
            metric: 相似度度量
                - 'cosine': 余弦相似度
                - 'euclidean': 欧氏距离
                - 'correlation': 相关系数
        
        Returns:
            相似度分数（越大越相似）
        """
        if len(vector1) != len(vector2):
            raise ValueError("Vectors must have same length")
        
        if metric == 'cosine':
            # 余弦相似度 [0, 1]
            similarity = 1 - cosine(vector1, vector2)
            
        elif metric == 'euclidean':
            # 欧氏距离转相似度
            distance = euclidean(vector1, vector2)
            similarity = 1 / (1 + distance)
            
        elif metric == 'correlation':
            # 皮尔逊相关系数 [-1, 1] -> [0, 1]
            correlation = np.corrcoef(vector1, vector2)[0, 1]
            similarity = (correlation + 1) / 2
        
        else:
            raise ValueError(f"Unknown metric: {metric}")
        
        return float(similarity)
    
    # ========== 模式搜索 ==========
    
    def find_similar_patterns(
        self,
        query_klines: List[Dict[str, Any]],
        candidate_klines_dict: Dict[str, List[Dict[str, Any]]],
        top_k: int = 10,
        method: str = 'price_volume',
        metric: str = 'cosine',
        backend: Optional[str] = None,
        allow_fallback: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        查找相似的K线形态

        Args:
            query_klines: 查询K线
            candidate_klines_dict: 候选K线字典 {code: klines}
            top_k: 返回前K个最相似的
            method: 向量化方法
            metric: 相似度度量

        Returns:
            相似形态列表
        """
        import time

        started_at = time.perf_counter()
        backend_requested = (backend or self.backend).strip().lower()
        if backend_requested not in {'python', 'index'}:
            backend_requested = 'python'
        fallback_enabled = self.allow_fallback if allow_fallback is None else bool(allow_fallback)

        def _build_meta(
            *,
            backend_used: str,
            fallback_used: bool,
            fallback_reason: Optional[str],
            candidate_count: int = 0,
        ) -> Dict[str, Any]:
            return {
                'backend_requested': backend_requested,
                'backend_used': backend_used,
                'fallback_used': fallback_used,
                'fallback_reason': fallback_reason,
                'latency_ms': round((time.perf_counter() - started_at) * 1000, 3),
                'candidate_count': candidate_count,
                'is_ann': False,
            }

        def _decorate(
            items: List[Dict[str, Any]],
            *,
            backend_used: str,
            fallback_used: bool,
            fallback_reason: Optional[str],
            source: Optional[str] = None,
            candidate_count: int = 0,
        ) -> List[Dict[str, Any]]:
            meta = _build_meta(
                backend_used=backend_used,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                candidate_count=candidate_count,
            )
            self.last_backend_used = backend_used
            self.last_meta = meta
            decorated = []
            for item in items:
                enriched = dict(item)
                if source is not None:
                    enriched['source'] = source
                # preserve per-item candidate_count if explicitly set (index path)
                item_candidate_count = enriched.get('candidate_count')
                enriched.update(meta)
                if item_candidate_count is not None:
                    enriched['candidate_count'] = item_candidate_count
                decorated.append(enriched)
            return decorated

        query_vector = self.kline_to_vector(query_klines, method)
        if len(query_vector) == 0:
            return _decorate(
                [],
                backend_used=backend_requested,
                fallback_used=False,
                fallback_reason='empty_query_vector',
                candidate_count=len(candidate_klines_dict),
            )

        similarities_cache: Optional[List[Dict[str, Any]]] = None

        def _compute_python_similarities() -> List[Dict[str, Any]]:
            nonlocal similarities_cache
            if similarities_cache is not None:
                return similarities_cache

            similarities: List[Dict[str, Any]] = []
            for code, klines in candidate_klines_dict.items():
                if len(klines) < len(query_klines):
                    continue

                candidate_klines = klines[-len(query_klines):]
                candidate_vector = self.kline_to_vector(candidate_klines, method)
                if len(candidate_vector) != len(query_vector):
                    continue

                similarity = self.calculate_similarity(query_vector, candidate_vector, metric)
                similarities.append({
                    'code': code,
                    'similarity': similarity,
                    'klines': candidate_klines,
                    'source': 'python',
                })

            similarities_cache = similarities
            return similarities

        def _python_results(*, backend_used: str = 'python', fallback_used: bool = False, fallback_reason: Optional[str] = None, source: str = 'python') -> List[Dict[str, Any]]:
            similarities = _compute_python_similarities()
            similarities.sort(key=lambda x: x['similarity'], reverse=True)
            return _decorate(
                similarities[:top_k],
                backend_used=backend_used,
                fallback_used=fallback_used,
                fallback_reason=fallback_reason,
                source=source,
                candidate_count=len(similarities),
            )

        if backend_requested == 'python':
            return _python_results()

        try:
            self.build_index(
                klines_dict=candidate_klines_dict,
                pattern_length=len(query_klines),
                method=method,
            )
            index_results = self.search_index(query_vector=query_vector, top_k=top_k, metric=metric)
            if index_results:
                enriched = []
                for item in index_results:
                    code = item.get('code')
                    if not code:
                        continue
                    candidate_klines = candidate_klines_dict.get(code, [])
                    if len(candidate_klines) >= len(query_klines):
                        candidate_klines = candidate_klines[-len(query_klines):]
                    enriched.append({
                        'code': code,
                        'similarity': item.get('similarity', 0.0),
                        'klines': candidate_klines,
                        'source': 'index',
                        'candidate_count': int(item.get('candidate_count') or len(self._index_codes)),
                        'index_reused': bool(item.get('index_reused', self._index_reused_last_build)),
                        'index_build_ms': float(item.get('index_build_ms', self._index_build_ms_last)),
                    })
                return _decorate(
                    enriched,
                    backend_used='index',
                    fallback_used=False,
                    fallback_reason=None,
                    source='index',
                    candidate_count=len(self._index_codes),
                )

            if fallback_enabled:
                return _python_results(
                    backend_used='python_fallback',
                    fallback_used=True,
                    fallback_reason='index_empty_result',
                    source='python_fallback',
                )
            return _decorate(
                [],
                backend_used='index',
                fallback_used=False,
                fallback_reason='index_empty_result',
                candidate_count=len(self._index_codes),
            )
        except Exception as exc:
            if fallback_enabled:
                return _python_results(
                    backend_used='python_fallback',
                    fallback_used=True,
                    fallback_reason=f'index_exception:{type(exc).__name__}',
                    source='python_fallback',
                )
            return _decorate(
                [],
                backend_used='index_error',
                fallback_used=False,
                fallback_reason=f'index_exception:{type(exc).__name__}',
                candidate_count=len(candidate_klines_dict),
            )

    # ========== 形态识别 ==========
    
    @staticmethod
    def recognize_pattern(klines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        识别K线形态
        
        Returns:
            形态信息
        """
        if len(klines) < 3:
            return {'pattern': 'unknown', 'confidence': 0.0}
        
        closes = np.array([k['close'] for k in klines])
        
        # 识别常见形态
        patterns = []
        
        # 1. 上升趋势
        if len(closes) >= 5:
            trend = np.polyfit(range(len(closes)), closes, 1)[0]
            if trend > 0:
                patterns.append(('uptrend', abs(trend) / closes.mean()))
        
        # 2. 下降趋势
        if len(closes) >= 5:
            trend = np.polyfit(range(len(closes)), closes, 1)[0]
            if trend < 0:
                patterns.append(('downtrend', abs(trend) / closes.mean()))
        
        # 3. 头肩顶
        if len(closes) >= 5:
            if closes[2] > closes[0] and closes[2] > closes[4] and closes[1] < closes[2] and closes[3] < closes[2]:
                patterns.append(('head_shoulders', 0.7))
        
        # 4. 双底
        if len(closes) >= 5:
            if closes[0] < closes[2] and closes[4] < closes[2] and abs(closes[0] - closes[4]) / closes[0] < 0.02:
                patterns.append(('double_bottom', 0.6))
        
        # 5. 突破
        if len(closes) >= 20:
            recent_high = np.max(closes[-20:-1])
            if closes[-1] > recent_high * 1.02:
                patterns.append(('breakout', 0.8))
        
        # 返回最强的形态
        if patterns:
            patterns.sort(key=lambda x: x[1], reverse=True)
            return {
                'pattern': patterns[0][0],
                'confidence': float(patterns[0][1]),
                'all_patterns': patterns,
            }
        
        return {'pattern': 'consolidation', 'confidence': 0.5}
    
    # ========== 向量索引 ==========
    
    def build_index(
        self,
        klines_dict: Dict[str, List[Dict[str, Any]]],
        pattern_length: int = 20,
        method: str = 'price_volume'
    ) -> Dict[str, np.ndarray]:
        """
        构建向量索引
        
        Args:
            klines_dict: K线数据字典
            pattern_length: 形态长度
            method: 向量化方法
        
        Returns:
            向量索引 {code: vector}
        """
        import time

        started_at = time.perf_counter()
        signature = self._build_index_signature(klines_dict, pattern_length, method)
        if (
            signature == self._index_signature
            and self.pattern_cache
            and self._index_matrix is not None
            and len(self._index_codes) == len(self.pattern_cache)
        ):
            self._index_reused_last_build = True
            self._index_build_ms_last = 0.0
            return self.pattern_cache

        index = {}
        
        for code, klines in klines_dict.items():
            if len(klines) < pattern_length:
                continue
            
            # 取最后N根K线
            pattern_klines = klines[-pattern_length:]
            vector = self.kline_to_vector(pattern_klines, method)
            
            if len(vector) > 0:
                index[code] = vector
        
        self.pattern_cache = index
        self._index_signature = signature
        self._index_reused_last_build = False
        self._index_build_ms_last = round((time.perf_counter() - started_at) * 1000, 3)
        self._hydrate_index_structures()
        self._save_index_cache(index)
        return index

    @staticmethod
    def _build_index_signature(
        klines_dict: Dict[str, List[Dict[str, Any]]],
        pattern_length: int,
        method: str,
    ) -> str:
        digest = hashlib.sha1()
        digest.update(str(pattern_length).encode('utf-8'))
        digest.update(b'|')
        digest.update(str(method or '').encode('utf-8'))
        for code in sorted(klines_dict.keys()):
            klines = klines_dict.get(code) or []
            tail = klines[-pattern_length:] if len(klines) >= pattern_length else klines
            last_row = tail[-1] if tail else {}
            digest.update(f'|{code}|{len(tail)}|{last_row.get("date") or last_row.get("time") or ""}'.encode('utf-8'))
        return digest.hexdigest()

    def _hydrate_index_structures(self) -> None:
        codes: list[str] = []
        vectors: list[np.ndarray] = []
        for code, vector in self.pattern_cache.items():
            arr = np.asarray(vector, dtype=np.float64)
            if arr.ndim != 1 or arr.size == 0:
                continue
            codes.append(code)
            vectors.append(arr)

        if not vectors:
            self._index_codes = []
            self._index_matrix = None
            self._index_norms = None
            return

        matrix = np.vstack(vectors).astype(np.float64, copy=False)
        self._index_codes = codes
        self._index_matrix = matrix
        self._index_norms = np.linalg.norm(matrix, axis=1)

    def _index_cache_path(self) -> Optional[Path]:
        cache_dir = os.getenv("VECTOR_SEARCH_CACHE_DIR", "").strip()
        if not cache_dir:
            return None
        p = Path(cache_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p / "kline_pattern_index.json"

    def _save_index_cache(self, index: Dict[str, np.ndarray]) -> None:
        path = self._index_cache_path()
        if path is None:
            return
        try:
            serializable = {
                'version': 2,
                'signature': self._index_signature,
                'vectors': {k: v.tolist() for k, v in index.items()},
            }
            path.write_text(_json.dumps(serializable), encoding="utf-8")
            _vs_logger.info("Vector index persisted to %s (%d entries)", path, len(index))
        except Exception as e:
            _vs_logger.warning("Failed to persist vector index: %s", e)

    def load_index_cache(self) -> bool:
        """Load persisted index from disk. Returns True if loaded successfully."""
        path = self._index_cache_path()
        if path is None or not path.exists():
            return False
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("vectors"), dict):
                self._index_signature = str(data.get("signature") or '') or None
                vectors = data.get("vectors") or {}
            else:
                self._index_signature = None
                vectors = data
            self.pattern_cache = {k: np.array(v) for k, v in dict(vectors or {}).items()}
            self._hydrate_index_structures()
            _vs_logger.info("Vector index loaded from %s (%d entries)", path, len(self.pattern_cache))
            return True
        except Exception as e:
            _vs_logger.warning("Failed to load vector index cache: %s", e)
            return False

    def search_index(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        metric: str = 'cosine'
    ) -> List[Dict[str, Any]]:
        """
        在索引中搜索
        
        Args:
            query_vector: 查询向量
            top_k: 返回前K个
            metric: 相似度度量
        
        Returns:
            搜索结果
        """
        if not self.pattern_cache:
            return []

        if self._index_matrix is None or len(self._index_codes) != len(self.pattern_cache):
            self._hydrate_index_structures()
        if self._index_matrix is None or self._index_matrix.size == 0:
            return []

        query = np.asarray(query_vector, dtype=np.float64)
        if query.ndim != 1 or query.size == 0:
            return []
        if self._index_matrix.shape[1] != query.shape[0]:
            return []

        if metric == 'cosine':
            query_norm = float(np.linalg.norm(query))
            if query_norm <= 1e-12:
                return []
            norms = self._index_norms if self._index_norms is not None else np.linalg.norm(self._index_matrix, axis=1)
            denom = np.maximum(norms * query_norm, 1e-12)
            similarities = (self._index_matrix @ query) / denom
        elif metric == 'euclidean':
            distances = np.linalg.norm(self._index_matrix - query, axis=1)
            similarities = 1.0 / (1.0 + distances)
        elif metric == 'correlation':
            centered_matrix = self._index_matrix - self._index_matrix.mean(axis=1, keepdims=True)
            centered_query = query - query.mean()
            denom = np.maximum(
                np.linalg.norm(centered_matrix, axis=1) * float(np.linalg.norm(centered_query)),
                1e-12,
            )
            similarities = ((centered_matrix @ centered_query) / denom + 1.0) / 2.0
        else:
            raise ValueError(f"Unknown metric: {metric}")

        candidate_count = int(len(self._index_codes))
        if top_k >= candidate_count:
            ranked = np.argsort(similarities)[::-1]
        else:
            ranked = np.argpartition(similarities, -top_k)[-top_k:]
            ranked = ranked[np.argsort(similarities[ranked])[::-1]]

        results: List[Dict[str, Any]] = []
        for idx in ranked[:top_k]:
            results.append({
                'code': self._index_codes[int(idx)],
                'similarity': float(similarities[int(idx)]),
                'candidate_count': candidate_count,
                'index_reused': self._index_reused_last_build,
                'index_build_ms': self._index_build_ms_last,
            })
        return results
    
    # ========== DTW动态时间规整 ==========
    
    @staticmethod
    def dtw_distance(series1: np.ndarray, series2: np.ndarray) -> float:
        """
        计算DTW距离（动态时间规整）
        用于比较不同长度的时间序列
        
        Args:
            series1: 时间序列1
            series2: 时间序列2
        
        Returns:
            DTW距离
        """
        n, m = len(series1), len(series2)
        
        # 初始化DTW矩阵
        dtw_matrix = np.full((n + 1, m + 1), np.inf)
        dtw_matrix[0, 0] = 0
        
        # 填充DTW矩阵
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                cost = abs(series1[i-1] - series2[j-1])
                dtw_matrix[i, j] = cost + min(
                    dtw_matrix[i-1, j],      # 插入
                    dtw_matrix[i, j-1],      # 删除
                    dtw_matrix[i-1, j-1]     # 匹配
                )
        
        return float(dtw_matrix[n, m])
    
    def find_similar_patterns_dtw(
        self,
        query_klines: List[Dict[str, Any]],
        candidate_klines_dict: Dict[str, List[Dict[str, Any]]],
        top_k: int = 10
    ) -> List[Dict[str, Any]]:
        """
        使用DTW查找相似形态（支持不同长度）
        
        Args:
            query_klines: 查询K线
            candidate_klines_dict: 候选K线字典
            top_k: 返回前K个
        
        Returns:
            相似形态列表
        """
        query_closes = np.array([k['close'] for k in query_klines])
        query_norm = (query_closes - query_closes.mean()) / (query_closes.std() + 1e-8)
        
        similarities = []
        
        for code, klines in candidate_klines_dict.items():
            if len(klines) < 5:
                continue
            
            candidate_closes = np.array([k['close'] for k in klines])
            candidate_norm = (candidate_closes - candidate_closes.mean()) / (candidate_closes.std() + 1e-8)
            
            # 计算DTW距离
            distance = self.dtw_distance(query_norm, candidate_norm)
            
            # 转换为相似度
            similarity = 1 / (1 + distance)
            
            similarities.append({
                'code': code,
                'similarity': similarity,
                'dtw_distance': distance,
            })
        
        similarities.sort(key=lambda x: x['similarity'], reverse=True)
        return similarities[:top_k]


# 全局实例
vector_search_engine = VectorSearchEngine()
