"""Compatibility wrapper for shared AIASK quant validation primitives."""

from aiask_quant_core.validation import *  # noqa: F401,F403

try:
    from aiask_quant_core.validation import __all__ as __all__
except ImportError:
    __all__ = [name for name in globals() if not name.startswith("_")]