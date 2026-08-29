"""Shared quant primitives for AIASK packages."""

from __future__ import annotations

from typing import Any

__all__ = [
    "RiskModel",
    "build_cross_section_summary",
    "compute_signal_hit_rate",
    "normalize_klines",
    "risk_model",
]


def __getattr__(name: str) -> Any:
    if name in {"RiskModel", "risk_model"}:
        from .risk_model import RiskModel, risk_model

        return {"RiskModel": RiskModel, "risk_model": risk_model}[name]
    if name in {"build_cross_section_summary", "compute_signal_hit_rate", "normalize_klines"}:
        from .data_pipeline import build_cross_section_summary, compute_signal_hit_rate, normalize_klines

        return {
            "build_cross_section_summary": build_cross_section_summary,
            "compute_signal_hit_rate": compute_signal_hit_rate,
            "normalize_klines": normalize_klines,
        }[name]
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
