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

from ._vector_platform_backend import _StrategyVectorPlatformBackendMixin
from ._vector_platform_profiles import _StrategyVectorPlatformProfilesMixin
from ._vector_platform_indexes import _StrategyVectorPlatformIndexesMixin
from ._vector_platform_search import _StrategyVectorPlatformSearchMixin


class StrategyVectorPlatform(_StrategyVectorPlatformBackendMixin, _StrategyVectorPlatformProfilesMixin, _StrategyVectorPlatformIndexesMixin, _StrategyVectorPlatformSearchMixin):
        SUPPORTED_NUMERIC_METHODS = {'price_volume', 'ohlc', 'returns', 'technical'}

        TEXT_EMBEDDING_ALIASES = {'text', 'text_embedding', 'text_embed', 'semantic', 'semantic_text'}

        PRODUCTION_BACKEND_STANDARD = 'sqlite_python_with_observable_fallback'

        def __init__(self):
            self.engine = VectorSearchEngine(backend='index', allow_fallback=True)
            self.text_embedding_service = get_strategy_text_embedding_service()
            self.preferred_backend = str(os.getenv('STRATEGY_VECTOR_BACKEND') or '').strip().lower()
            self.allow_fallback = str(os.getenv('STRATEGY_VECTOR_ALLOW_FALLBACK', '1')).strip().lower() not in {
                '0', 'false', 'no', 'off'
            }




_vector_platform: Optional[StrategyVectorPlatform] = None


def get_strategy_vector_platform() -> StrategyVectorPlatform:
    global _vector_platform
    if _vector_platform is None:
        _vector_platform = StrategyVectorPlatform()
    return _vector_platform
