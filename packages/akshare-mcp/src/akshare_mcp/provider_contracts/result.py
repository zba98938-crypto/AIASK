"""Unified internal financial result adapter.

The public tool envelope and ``data`` payload remain unchanged. This object is
for platform diagnostics, lineage, quality gates, and future standard-model
normalization.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import Field

from .base import ContractBaseModel


class AIASKFinancialResult(ContractBaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    standard_data: Any = None
    provider_extra: dict[str, Any] = Field(default_factory=dict)
    raw_snapshot: Any = None
    quality: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    side_effect: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_tool_result(
        cls,
        result: dict[str, Any],
        *,
        standard_data: Any = None,
        raw_snapshot: Any = None,
        provider_extra: dict[str, Any] | None = None,
    ) -> "AIASKFinancialResult":
        meta = dict(result.get("meta") or {}) if isinstance(result, dict) else {}
        return cls(
            success=bool(result.get("success")) if isinstance(result, dict) else False,
            data=deepcopy(result.get("data")) if isinstance(result, dict) else None,
            error=result.get("error") if isinstance(result, dict) else "invalid result",
            standard_data=standard_data,
            provider_extra=dict(provider_extra or {}),
            raw_snapshot=deepcopy(raw_snapshot if raw_snapshot is not None else result),
            quality=dict(meta.get("quality") or result.get("data_quality") or {}) if isinstance(result, dict) else {},
            lineage=dict(meta.get("lineage") or {}) if isinstance(meta, dict) else {},
            side_effect=dict(meta.get("side_effect") or {}) if isinstance(meta, dict) else {},
            meta=meta,
        )

    def to_tool_envelope(self) -> dict[str, Any]:
        payload = {
            "success": bool(self.success),
            "data": deepcopy(self.data),
            "error": self.error,
            "meta": deepcopy(self.meta),
        }
        return payload
