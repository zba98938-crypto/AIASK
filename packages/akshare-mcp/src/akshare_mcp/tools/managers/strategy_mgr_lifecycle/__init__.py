"""strategy_mgr_lifecycle package facade (split from single module)."""

from . import _lifecycle_support as _lifecycle_support
from . import handlers as _handlers

# Preserve the original single-module surface: re-export every public + private
# name (handlers, helpers, and backward-compatible aliases like _lifecycle_scan).
for _mod in (_lifecycle_support, _handlers):
    globals().update(
        {name: getattr(_mod, name) for name in dir(_mod) if not name.startswith("__")}
    )
