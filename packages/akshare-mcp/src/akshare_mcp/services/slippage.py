"""Compatibility wrapper for shared AIASK quant slippage primitives."""

from aiask_quant_core.slippage import *  # noqa: F401,F403
from aiask_quant_core.slippage import slippage_calculator

try:
    from aiask_quant_core.slippage import __all__ as __all__
except ImportError:
    __all__ = [name for name in globals() if not name.startswith("_")]