"""Unified vector storage mixin for market / quant / strategy derived objects."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any, Iterable, List, Optional

from ._vector_unified_utils import _VectorUnifiedUtilsMixin
from ._vector_unified_storage import _VectorUnifiedStorageMixin
from ._vector_unified_storage_search import _VectorUnifiedStorageSearchMixin
from ._vector_unified_indexes import _VectorUnifiedIndexesMixin
from ._vector_unified_docs import _VectorUnifiedDocsMixin


class VectorUnifiedMixin(_VectorUnifiedUtilsMixin, _VectorUnifiedStorageMixin, _VectorUnifiedStorageSearchMixin, _VectorUnifiedIndexesMixin, _VectorUnifiedDocsMixin):
        """Generic vector archive / sqlite_python store / ANN governance helpers."""
