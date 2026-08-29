"""策略向量平台：统一画像、持久化索引和 ANN-like 相似检索。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from .text_embedding import get_strategy_text_embedding_service
from .vector_search import VectorSearchEngine

logger = logging.getLogger(__name__)


class _StrategyVectorPlatformBackendMixin:
        @staticmethod
        def _embedding_service_is_closed(service: Any) -> bool:
            if service is None:
                return True
            client_state = getattr(service, 'client_state', None)
            if callable(client_state):
                try:
                    return str(client_state() or '').strip().lower() == 'closed'
                except Exception:
                    return False
            client_state = getattr(service, '_client_state', None)
            if callable(client_state):
                try:
                    return str(client_state() or '').strip().lower() == 'closed'
                except Exception:
                    return False
            is_closed = getattr(service, 'is_closed', None)
            if callable(is_closed):
                try:
                    return bool(is_closed())
                except Exception:
                    return False
            return False

        def _bind_text_embedding_service(self):
            service = getattr(self, 'text_embedding_service', None)
            if service is None or self._embedding_service_is_closed(service):
                service = get_strategy_text_embedding_service()
                self.text_embedding_service = service
            return service

        async def _ensure_text_embedding_service(self):
            service = self._bind_text_embedding_service()
            ensure_client = getattr(service, 'ensure_client', None)
            if callable(ensure_client):
                result = ensure_client()
                if asyncio.iscoroutine(result):
                    await result
            return getattr(self, 'text_embedding_service', service)

        def _resolved_preferred_backend(self) -> str:
            requested = str(self.preferred_backend or '').strip().lower()
            if requested in {'sqlite_python', 'index', 'python'}:
                return requested
            return 'sqlite_python'

        def _policy_meta(self) -> dict:
            return {
                'production_backend_standard': self.PRODUCTION_BACKEND_STANDARD,
                'fallback_allowed': bool(self.allow_fallback),
            }

        @staticmethod
        def _coerce_positive_int(value: Any, default: int) -> int:
            try:
                resolved = int(value)
            except Exception:
                resolved = int(default)
            return max(1, resolved)

        def _hnsw_index_params(self, overrides: Optional[dict] = None) -> dict:
            params = dict(overrides or {})
            return {
                'm': self._coerce_positive_int(
                    params.get('m')
                    or os.getenv('STRATEGY_VECTOR_HNSW_M')
                    or os.getenv('VECTOR_HNSW_M')
                    or 16,
                    16,
                ),
                'ef_construction': self._coerce_positive_int(
                    params.get('ef_construction')
                    or os.getenv('STRATEGY_VECTOR_HNSW_EF_CONSTRUCTION')
                    or os.getenv('VECTOR_HNSW_EF_CONSTRUCTION')
                    or 64,
                    64,
                ),
                'ef_search': self._coerce_positive_int(
                    params.get('ef_search')
                    or os.getenv('STRATEGY_VECTOR_HNSW_EF_SEARCH')
                    or os.getenv('VECTOR_HNSW_EF_SEARCH')
                    or 80,
                    80,
                ),
            }

        def requested_backend_name(self, db) -> str:
            del db
            return self._resolved_preferred_backend()

        def backend_name(self, db) -> str:
            requested = self.requested_backend_name(db)
            if requested == 'sqlite_python' and not getattr(db, 'supports_sqlite_python', lambda: False)():
                if not self.allow_fallback:
                    return 'sqlite_python'
                if hasattr(db, 'get_vector_backend'):
                    try:
                        fallback = str(db.get_vector_backend() or '').strip().lower()
                        if fallback in {'sqlite_python', 'index', 'python'}:
                            return fallback
                    except Exception:
                        pass
                return str(getattr(self.engine, 'backend', 'index') or 'index')
            if requested in {'sqlite_python', 'index', 'python'}:
                return requested
            return 'sqlite_python' if getattr(db, 'supports_sqlite_python', lambda: False)() else self.engine.backend

        def _build_backend_audit(self, db, started_at: float) -> dict:
            backend_requested = self.requested_backend_name(db)
            backend_used = self.backend_name(db)
            return {
                'backend_requested': backend_requested,
                'backend_used': backend_used,
                'fallback_used': backend_requested != backend_used,
                'fallback_reason': None if backend_requested == backend_used else 'preferred_backend_unavailable',
                'latency_ms': round((time.perf_counter() - started_at) * 1000, 3),
                **self._policy_meta(),
            }

        def _merge_backend_audit(
            self,
            *,
            backend_requested: str,
            backend_used: str,
            started_at: float,
            fallback_reason: Optional[str] = None,
            fallback_used: Optional[bool] = None,
        ) -> dict:
            resolved_fallback_used = (
                bool(fallback_reason) or backend_requested != backend_used
                if fallback_used is None
                else bool(fallback_used or backend_requested != backend_used)
            )
            resolved_reason = fallback_reason
            if resolved_reason is None and backend_requested != backend_used:
                resolved_reason = 'preferred_backend_unavailable'
            return {
                'backend_requested': backend_requested,
                'backend_used': backend_used,
                'fallback_used': resolved_fallback_used,
                'fallback_reason': resolved_reason,
                'latency_ms': round((time.perf_counter() - started_at) * 1000, 3),
                **self._policy_meta(),
            }

        def default_vector_method(self) -> str:
            service = self._bind_text_embedding_service()
            return 'text_embedding' if getattr(service, 'prefers_text_embedding_default', lambda: service.is_enabled())() else 'price_volume'

        def resolve_vector_method(self, vector_method: Optional[str]) -> str:
            normalized = str(vector_method or '').strip().lower()
            if not normalized:
                return self.default_vector_method()
            if normalized in self.TEXT_EMBEDDING_ALIASES:
                return 'text_embedding'
            return normalized

        def ensure_vector_method_available(self, vector_method: Optional[str]) -> str:
            resolved = self.resolve_vector_method(vector_method)
            service = self._bind_text_embedding_service()
            if resolved == 'text_embedding' and not service.is_enabled():
                raise RuntimeError('text_embedding requested but provider not configured')
            if resolved not in self.SUPPORTED_NUMERIC_METHODS and resolved != 'text_embedding':
                raise ValueError(f'unsupported vector_method: {resolved}')
            return resolved

        @staticmethod
        def _text_embedding_fallback_method() -> str:
            return 'price_volume'

        @staticmethod
        def _sanitize_collection_part(value: Any, max_len: int = 48) -> str:
            normalized = ''.join(ch.lower() if str(ch).isalnum() else '_' for ch in str(value or '').strip())
            normalized = '_'.join(part for part in normalized.split('_') if part)
            truncated = normalized[: max(8, int(max_len or 48))].strip('_')
            return truncated or 'na'

        @staticmethod
        def _default_strategy_model_id(vector_method: str, vector_dim: int) -> str:
            normalized_method = str(vector_method or 'price_volume').strip().lower() or 'price_volume'
            if normalized_method == 'price_volume' and int(vector_dim or 0) == 120:
                return 'strategy-behavior-v1'
            return f'strategy-{normalized_method}-v1'

        def _resolve_strategy_model_id(
            self,
            *,
            vector_method: str,
            vector_dim: int,
            embedding_meta: Optional[dict] = None,
        ) -> str:
            meta = dict(embedding_meta or {})
            explicit = str(meta.get('model_id') or meta.get('embedding_model') or '').strip()
            return explicit or self._default_strategy_model_id(vector_method, vector_dim)

        def _strategy_collection_name(
            cls,
            *,
            index_name: str,
            model_id: str,
            vector_dim: int,
            metric: str = 'cosine',
            normalization: str = 'unit',
        ) -> str:
            resolved_index_name = str(index_name or 'strategy_behavior').strip() or 'strategy_behavior'
            base = f'{resolved_index_name}_embeddings'
            resolved_model_id = str(model_id or '').strip() or 'strategy-behavior-v1'
            resolved_metric = str(metric or 'cosine').strip().lower() or 'cosine'
            resolved_normalization = str(normalization or 'unit').strip().lower() or 'unit'
            if (
                resolved_model_id == 'strategy-behavior-v1'
                and int(vector_dim or 0) == 120
                and resolved_metric == 'cosine'
                and resolved_normalization == 'unit'
            ):
                return base
            suffix = '__'.join(
                [
                    cls._sanitize_collection_part(resolved_model_id),
                    f'd{int(vector_dim or 0)}',
                    cls._sanitize_collection_part(resolved_metric, max_len=16),
                    cls._sanitize_collection_part(resolved_normalization, max_len=16),
                ]
            )
            return f'{base}__{suffix}'

        @staticmethod
        def _strategy_index_name_from_collection(collection_name: Optional[str], collection: Optional[dict] = None) -> str:
            meta = dict((collection or {}).get('metadata') or {})
            explicit = str(meta.get('index_name') or '').strip()
            if explicit:
                return explicit
            normalized = str(collection_name or '').strip()
            if normalized.endswith('_embeddings'):
                return normalized[: -len('_embeddings')]
            if '_embeddings__' in normalized:
                return normalized.split('_embeddings__', 1)[0]
            return 'strategy_behavior'

        def _collection_sort_key(cls, collection: dict) -> tuple:
            active_priority = 0 if str(collection.get('active_version') or '').strip() else 1
            updated_at = str(collection.get('updated_at') or collection.get('created_at') or '')
            return (
                active_priority,
                updated_at,
                str(collection.get('collection_name') or ''),
            )

        @staticmethod
        def _sqlite_python_backend_family(backend_used: Optional[str]) -> str:
            normalized = str(backend_used or '').strip().lower()
            if normalized.startswith('sqlite_python'):
                return 'sqlite_python'
            return normalized or 'unavailable'

        @staticmethod
        def _unified_retrieval_mode(backend_used: Optional[str]) -> str:
            normalized = str(backend_used or '').strip().lower()
            if normalized == 'sqlite_python_index_item':
                return 'unified_sqlite_python_ann'
            if normalized == 'sqlite_python_profile':
                return 'unified_sqlite_python_exact'
            if normalized == 'exact_json':
                return 'unified_exact_json'
            return f'unified_{normalized}' if normalized else 'unified_unknown'

        @staticmethod
        def _unified_result_source(retrieval_mode: Optional[str]) -> str:
            normalized = str(retrieval_mode or '').strip().lower()
            if normalized == 'unified_sqlite_python_ann':
                return 'unified_ann'
            if normalized == 'unified_sqlite_python_exact':
                return 'unified_profile_exact'
            if normalized == 'unified_exact_json':
                return 'unified_exact_json'
            return 'unified_unknown'

        @staticmethod
        def _legacy_result_source(retrieval_mode: Optional[str]) -> str:
            normalized = str(retrieval_mode or '').strip().lower()
            if normalized in {'persisted_ann', 'sqlite_python_ann'}:
                return 'legacy_ann'
            if normalized == 'sqlite_python_exact':
                return 'legacy_profile_exact'
            if normalized == 'full_scan':
                return 'legacy_full_scan'
            return 'legacy_unknown'

        @staticmethod
        def _signature(strategy: dict, profile_type: str, vector_method: str) -> str:
            payload = {
                'strategy_id': strategy.get('id'),
                'strategy_type': strategy.get('strategy_type'),
                'params': strategy.get('params') or {},
                'profile_type': profile_type,
                'vector_method': vector_method,
            }
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.sha1(raw.encode('utf-8')).hexdigest()

        @staticmethod
        def _as_float_array(values: Any) -> np.ndarray:
            if values is None:
                return np.asarray([], dtype=np.float64)
            return np.asarray(values, dtype=np.float64)

        @staticmethod
        def _returns_to_pseudo_klines(series: np.ndarray) -> List[dict]:
            price = 100.0
            rows: List[dict] = []
            for ret in np.asarray(series[-60:], dtype=np.float64):
                open_price = price
                price = open_price * (1 + float(ret))
                rows.append({
                    'open': round(open_price, 6),
                    'high': round(max(open_price, price), 6),
                    'low': round(min(open_price, price), 6),
                    'close': round(price, 6),
                    'volume': 1.0,
                })
            return rows

        @staticmethod
        def _normalize_embedding(values: Any) -> np.ndarray:
            vector = _StrategyVectorPlatformBackendMixin._as_float_array(values)
            if vector.ndim != 1 or len(vector) == 0:
                return np.asarray([], dtype=np.float64)
            norm = float(np.linalg.norm(vector))
            if norm <= 1e-12:
                return np.asarray([], dtype=np.float64)
            return vector / norm

        @staticmethod
        def _resolved_index_name(index_name: Optional[str], profile: Optional[dict]) -> str:
            meta = dict((profile or {}).get('metadata') or {})
            return str(index_name or meta.get('index_name') or 'strategy_behavior')

        @staticmethod
        def _resolve_bucket_count(sample_count: int) -> int:
            if sample_count <= 1:
                return 1
            return max(1, min(8, int(round(math.sqrt(sample_count)))))

        @staticmethod
        def _bucket_label(idx: int) -> str:
            return f'bucket_{idx + 1:02d}'

        @staticmethod
        def _summarize_holdings(holdings: Any, limit: int = 5) -> str:
            rows = []
            for item in list(holdings or [])[: max(1, min(int(limit or 5), 10))]:
                if not isinstance(item, dict):
                    continue
                code = str(item.get('code') or item.get('symbol') or '').strip()
                if not code:
                    continue
                weight = float(item.get('weight') or 0.0)
                rows.append(f'{code}({weight:.2%})')
            return ', '.join(rows) if rows else '无显著持仓'

        @staticmethod
        def _max_drawdown(series: np.ndarray) -> float:
            if len(series) == 0:
                return 0.0
            nav = np.cumprod(1.0 + np.asarray(series, dtype=np.float64))
            peaks = np.maximum.accumulate(nav)
            drawdowns = nav / np.maximum(peaks, 1e-12) - 1.0
            return float(np.min(drawdowns)) if len(drawdowns) else 0.0

        def _build_text_embedding_document(
            self,
            strategy: dict,
            panels: dict,
            *,
            profile_type: str,
            index_name: str,
            index_version: str,
        ) -> str:
            series = self._as_float_array(panels.get('strategy_returns'))
            holdings = list(panels.get('holdings') or [])
            params_text = json.dumps(strategy.get('params') or {}, ensure_ascii=False, sort_keys=True)
            latest = series[-20:] if len(series) >= 20 else series
            recent_returns = ', '.join(f'{float(item):.4f}' for item in latest.tolist()) if len(latest) else '无'
            mean_return = float(np.mean(series)) if len(series) else 0.0
            volatility = float(np.std(series)) if len(series) else 0.0
            recent_5 = float(np.prod(1.0 + series[-5:]) - 1.0) if len(series) >= 5 else float(np.prod(1.0 + series) - 1.0) if len(series) else 0.0
            recent_20 = float(np.prod(1.0 + series[-20:]) - 1.0) if len(series) >= 20 else float(np.prod(1.0 + series) - 1.0) if len(series) else 0.0
            recent_60 = float(np.prod(1.0 + series[-60:]) - 1.0) if len(series) >= 60 else float(np.prod(1.0 + series) - 1.0) if len(series) else 0.0
            positive_ratio = float(np.mean(series > 0)) if len(series) else 0.0
            drawdown = self._max_drawdown(series)
            factor_panel = self._as_float_array(panels.get('factor_panel'))
            return_panel = self._as_float_array(panels.get('return_panel'))
            factor_shape = f'{factor_panel.shape[0]}x{factor_panel.shape[1]}' if factor_panel.ndim == 2 else '0x0'
            return_shape = f'{return_panel.shape[0]}x{return_panel.shape[1]}' if return_panel.ndim == 2 else '0x0'
            return "\n".join([
                '策略文本画像',
                f"策略ID: {strategy.get('id') or 'unknown'}",
                f"策略名称: {strategy.get('name') or strategy.get('strategy_type') or 'unknown'}",
                f"策略类型: {strategy.get('strategy_type') or 'unknown'}",
                f"画像类型: {profile_type}",
                f"索引名称: {index_name}",
                f"索引版本: {index_version}",
                f"参数: {params_text}",
                f"样本持仓: {self._summarize_holdings(holdings)}",
                f"行为序列长度: {int(len(series))}",
                f"因子面板形状: {factor_shape}",
                f"收益面板形状: {return_shape}",
                f"平均单期收益: {mean_return:.6f}",
                f"波动率: {volatility:.6f}",
                f"正收益占比: {positive_ratio:.2%}",
                f"最大回撤: {drawdown:.2%}",
                f"最近5期累计收益: {recent_5:.2%}",
                f"最近20期累计收益: {recent_20:.2%}",
                f"最近60期累计收益: {recent_60:.2%}",
                f"最近收益序列: {recent_returns}",
            ])

        async def _build_embedding(
            self,
            *,
            strategy: dict,
            panels: dict,
            vector_method: str,
            profile_type: str,
            index_name: str,
            index_version: str,
        ) -> tuple[np.ndarray, dict, str]:
            series = panels.get('strategy_returns')
            if series is None or len(series) < 30:
                return np.asarray([], dtype=np.float64), {}, vector_method
            if vector_method == 'text_embedding':
                service = await self._ensure_text_embedding_service()
                document = self._build_text_embedding_document(
                    strategy,
                    panels,
                    profile_type=profile_type,
                    index_name=index_name,
                    index_version=index_version,
                )
                text_meta = {
                    'embedding_source': 'strategy_text_profile',
                    'embedding_provider': getattr(getattr(service, 'config', None), 'provider', 'openai_compatible'),
                    'embedding_model': getattr(getattr(service, 'config', None), 'model', None),
                    'embedding_text_preview': document[:240],
                    'embedding_text_hash': hashlib.sha1(document.encode('utf-8')).hexdigest(),
                    'sample_length': int(len(series)),
                    'pattern_length': int(min(len(series), 60)),
                }
                try:
                    embedding = np.asarray(await service.embed_text(document), dtype=np.float64)
                    return embedding, {
                        **text_meta,
                        'requested_vector_method': 'text_embedding',
                        'resolved_vector_method': 'text_embedding',
                        'fallback_used': False,
                        'fallback_reason': None,
                    }, 'text_embedding'
                except Exception as exc:
                    fallback_method = self._text_embedding_fallback_method()
                    logger.info(
                        'StrategyVectorPlatform text embedding unavailable for %s, fallback to %s: %s',
                        strategy.get('id'),
                        fallback_method,
                        exc.__class__.__name__,
                    )
                    klines = self._returns_to_pseudo_klines(np.asarray(series, dtype=np.float64))
                    fallback_embedding = self.engine.kline_to_vector(klines, fallback_method)
                    return np.asarray(fallback_embedding if fallback_embedding is not None else [], dtype=np.float64), {
                        **text_meta,
                        'embedding_source': 'strategy_returns_fallback',
                        'requested_vector_method': 'text_embedding',
                        'resolved_vector_method': fallback_method,
                        'fallback_used': True,
                        'fallback_reason': 'text_embedding_request_failed',
                        'fallback_error_type': exc.__class__.__name__,
                        'fallback_error': str(exc or exc.__class__.__name__)[:240],
                    }, fallback_method
            klines = self._returns_to_pseudo_klines(np.asarray(series, dtype=np.float64))
            embedding = self.engine.kline_to_vector(klines, vector_method)
            return np.asarray(embedding if embedding is not None else [], dtype=np.float64), {
                'sample_length': int(len(klines)),
                'pattern_length': int(len(klines)),
                'requested_vector_method': vector_method,
                'resolved_vector_method': vector_method,
                'fallback_used': False,
                'fallback_reason': None,
            }, vector_method
