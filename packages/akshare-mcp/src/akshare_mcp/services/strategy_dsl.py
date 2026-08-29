"""Compatibility wrapper for shared AIASK quant strategy DSL primitives."""

from aiask_quant_core.strategy_dsl import *  # noqa: F401,F403

try:
    from aiask_quant_core.strategy_dsl import __all__ as __all__
except ImportError:
    __all__ = [name for name in globals() if not name.startswith("_")]