"""Compatibility wrapper for `aiask_quant_core.backtest._engine_support`."""

from __future__ import annotations

from akshare_mcp.services._quant_core_compat import export_quant_core_module as _export_quant_core_module

_module = _export_quant_core_module(globals(), "aiask_quant_core.backtest._engine_support")


def __getattr__(name: str):
    return getattr(_module, name)