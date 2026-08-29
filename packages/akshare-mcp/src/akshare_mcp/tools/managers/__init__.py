"""Manager工具模块 - 统一注册入口（自动发现）"""

from importlib import import_module
from pkgutil import iter_modules
from typing import Callable, List, Optional, Sequence, Tuple

Registrar = Callable[[object], None]


def discover_manager_registrars(
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
) -> List[Tuple[str, Registrar]]:
    """自动发现并返回 manager 注册函数列表。

    Args:
        include: 可选，仅包含这些 manager 模块名（不含 .py）
        exclude: 可选，排除这些 manager 模块名（不含 .py）

    Returns:
        [(module_name, register_func), ...]，按 module_name 排序
    """
    include_set = set(include or [])
    exclude_set = set(exclude or [])

    discovered: List[Tuple[str, Registrar]] = []
    for module_info in sorted(iter_modules(__path__), key=lambda x: x.name):
        module_name = module_info.name
        if not module_name.endswith("_manager"):
            continue
        if include_set and module_name not in include_set:
            continue
        if module_name in exclude_set:
            continue

        module = import_module(f"{__name__}.{module_name}")
        register_name = f"register_{module_name}"
        register_func = getattr(module, register_name, None)
        if callable(register_func):
            discovered.append((module_name, register_func))

    return discovered


def register(
    mcp,
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    strict: bool = False,
):
    """注册所有Manager工具。

    向后兼容：保留 `register(mcp)` 旧调用方式。

    Args:
        mcp: FastMCP 实例
        include: 可选，仅注册给定 manager 模块名列表
        exclude: 可选，排除给定 manager 模块名列表
        strict: 为 True 时，发现模块但缺少注册函数将抛异常
    """
    registrars = discover_manager_registrars(include=include, exclude=exclude)

    if strict:
        include_set = set(include or [])
        for module_name in include_set:
            if module_name.endswith("_manager") and not any(n == module_name for n, _ in registrars):
                raise ValueError(f"Manager registrar not found: {module_name}")

    for _, registrar in registrars:
        registrar(mcp)
