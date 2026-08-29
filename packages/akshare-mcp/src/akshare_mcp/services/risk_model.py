"""Compatibility wrapper for shared AIASK quant risk primitives."""

from aiask_quant_core.risk_model import *  # noqa: F401,F403
from aiask_quant_core.risk_model import _shrunk_cov as _shrunk_cov

try:
    from aiask_quant_core.risk_model import __all__ as __all__
except ImportError:
    __all__ = [name for name in globals() if not name.startswith("_")]