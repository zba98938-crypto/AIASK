"""SQLite 策略超市 Mixin — CRUD / 静态工具 / 工厂 / 质量报告 / 领域事件"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _StrategyCrudUtilsMixin:
        @staticmethod
        def _decode_json_field(value: Any, default: Any) -> Any:
            if value is None:
                return default
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except Exception:
                    return default
            return value

        @staticmethod
        def _coerce_timestamp(value: Any) -> Optional[datetime]:
            if value is None or isinstance(value, datetime):
                return value
            raw = str(value or '').strip()
            if not raw:
                return None
            normalized = raw[:-1] + '+00:00' if raw.endswith('Z') else raw
            try:
                return datetime.fromisoformat(normalized)
            except Exception:
                return None

        @staticmethod
        def _coerce_date(value: Any) -> Optional[date]:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            raw = str(value or '').strip()
            if not raw:
                return None
            normalized = raw[:-1] + '+00:00' if raw.endswith('Z') else raw
            try:
                return datetime.fromisoformat(normalized).date()
            except Exception:
                pass
            try:
                return date.fromisoformat(raw.split('T', 1)[0])
            except Exception:
                return None

        @staticmethod
        def _coerce_ts_code(value: Any) -> Optional[str]:
            if value is None:
                return None
            raw = str(value or "").strip().upper()
            if not raw:
                return None
            if "." in raw:
                return raw
            digits = "".join(ch for ch in raw if ch.isdigit())
            if len(digits) == 6:
                suffix = "SH" if digits.startswith(("5", "6", "9")) else "SZ"
                return f"{digits}.{suffix}"
            return raw

        @staticmethod
        def _normalize_strategy_statuses(status: Any) -> Optional[list[str]]:
            if status is None:
                return None
            raw_values = status if isinstance(status, (list, tuple, set)) else str(status).split(",")
            normalized: list[str] = []
            for item in raw_values:
                token = str(item or "").strip().lower()
                if not token:
                    continue
                if token in {"all", "*"}:
                    return None
                if token == "published":
                    token = "listed"
                if token not in normalized:
                    normalized.append(token)
            return normalized or None

        @staticmethod
        def _encode_sqlite_python(values: Any) -> Optional[str]:
            try:
                vector = [float(item) for item in list(values or [])]
            except Exception:
                return None
            if not vector:
                return None
            cleaned: List[float] = []
            for item in vector:
                if item != item or item in {float('inf'), float('-inf')}:
                    cleaned.append(0.0)
                else:
                    cleaned.append(float(item))
            return '[' + ','.join(format(item, '.10g') for item in cleaned) + ']'

        @staticmethod
        def _resolve_vector_index_name(payload: dict) -> str:
            meta = dict(payload.get('metadata') or {})
            return str(payload.get('index_name') or meta.get('index_name') or 'strategy_behavior')

        @staticmethod
        def _sqlite_python_distance_sql(column: str, metric: str, dim: int, query_ref: str = '$1') -> tuple[str, str]:
            del column, metric, dim, query_ref
            return "0.0", "0.0"

        @staticmethod
        def _sqlite_python_opclass(metric: str) -> str:
            return 'vector_l2_ops' if str(metric or 'cosine').lower() == 'euclidean' else 'vector_cosine_ops'

        @staticmethod
        def _sql_quote(value: Any) -> str:
            return "'" + str(value or '').replace("'", "''") + "'"

        @staticmethod
        def _sqlite_python_partial_index_name(prefix: str, *parts: Any) -> str:
            digest = hashlib.sha1('|'.join(str(part or '') for part in parts).encode('utf-8')).hexdigest()[:12]
            return f"{prefix}_{digest}"

        @staticmethod
        def _coerce_positive_int(value: Any, default: int) -> int:
            try:
                resolved = int(value)
            except Exception:
                resolved = int(default)
            return max(1, resolved)

        @classmethod
        def _resolve_sqlite_python_hnsw_params(cls, index_params: Optional[dict] = None) -> dict:
            params = dict(index_params or {})
            m = (
                params.get("m")
                or os.getenv("STRATEGY_VECTOR_HNSW_M")
                or os.getenv("VECTOR_HNSW_M")
                or 16
            )
            ef_construction = (
                params.get("ef_construction")
                or os.getenv("STRATEGY_VECTOR_HNSW_EF_CONSTRUCTION")
                or os.getenv("VECTOR_HNSW_EF_CONSTRUCTION")
                or 64
            )
            ef_search = (
                params.get("ef_search")
                or os.getenv("STRATEGY_VECTOR_HNSW_EF_SEARCH")
                or os.getenv("VECTOR_HNSW_EF_SEARCH")
                or 80
            )
            return {
                "m": cls._coerce_positive_int(m, 16),
                "ef_construction": cls._coerce_positive_int(ef_construction, 64),
                "ef_search": cls._coerce_positive_int(ef_search, 80),
            }

        @classmethod
        def _sqlite_python_hnsw_with_clause(cls, index_params: Optional[dict] = None) -> str:
            params = cls._resolve_sqlite_python_hnsw_params(index_params)
            return (
                f" WITH (m = {int(params['m'])}, ef_construction = {int(params['ef_construction'])})"
            )

        @classmethod
        def _resolve_sqlite_python_index_build_settings(cls, index_params: Optional[dict] = None) -> dict:
            params = dict(index_params or {})
            maintenance_work_mem = (
                params.get("maintenance_work_mem")
                or os.getenv("STRATEGY_VECTOR_INDEX_BUILD_MAINTENANCE_WORK_MEM")
                or os.getenv("VECTOR_INDEX_BUILD_MAINTENANCE_WORK_MEM")
                or "256MB"
            )
            max_parallel_maintenance_workers = (
                params.get("max_parallel_maintenance_workers")
                or os.getenv("STRATEGY_VECTOR_INDEX_BUILD_MAX_PARALLEL_MAINTENANCE_WORKERS")
                or os.getenv("VECTOR_INDEX_BUILD_MAX_PARALLEL_MAINTENANCE_WORKERS")
                or 1
            )
            return {
                "maintenance_work_mem": str(maintenance_work_mem or "").strip() or None,
                "max_parallel_maintenance_workers": cls._coerce_positive_int(
                    max_parallel_maintenance_workers,
                    1,
                ),
            }

        def _decode_strategy_row(self, row: dict) -> dict:
            result = dict(row)
            result["params"] = self._decode_json_field(result.get("params"), {})
            result["factor_weights"] = self._decode_json_field(result.get("factor_weights"), {})
            result["tags"] = self._decode_json_field(result.get("tags"), result.get("tags") or [])
            return result
