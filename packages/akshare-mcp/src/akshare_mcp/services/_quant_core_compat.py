"""Compatibility helpers for re-exporting shared quant-core modules."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


def _public_or_compat_names(module: ModuleType) -> list[str]:
    exported = list(getattr(module, "__all__", []) or [])
    names = [name for name in dir(module) if not (name.startswith("__") and name.endswith("__"))]
    return list(dict.fromkeys(exported + names))


def export_quant_core_module(globals_dict: dict[str, Any], target_module: str) -> ModuleType:
    module = import_module(target_module)
    names = _public_or_compat_names(module)
    globals_dict.update({name: getattr(module, name) for name in names})
    globals_dict["__all__"] = names
    globals_dict["__doc__"] = module.__doc__
    return module
