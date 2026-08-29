"""策略注册表 — 动态策略注册与实例化"""

import logging
from typing import Dict, List, Optional, Tuple, Type

from .strategy_base import IStrategy

logger = logging.getLogger(__name__)

_EXECUTABLE_DSL_REQUIRED_TYPES = {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"}


class StrategyRegistry:
    """全局策略注册表。内置策略在 __init__.py 中自动注册。"""

    _strategies: Dict[str, Type[IStrategy]] = {}

    @classmethod
    def register(cls, strategy_class: Type[IStrategy]) -> Type[IStrategy]:
        """注册策略类。可作为装饰器使用。"""
        name = strategy_class.name()
        cls._strategies[name] = strategy_class
        logger.info("Registered strategy: %s", name)
        return strategy_class

    @classmethod
    def get(cls, name: str) -> Optional[Type[IStrategy]]:
        return cls._strategies.get(name)

    @classmethod
    def create(cls, name: str, params: Optional[dict] = None) -> IStrategy:
        klass = cls._strategies.get(name)
        if klass is None:
            raise KeyError(f"Strategy not registered: {name}")
        instance = klass()
        if params:
            instance.set_parameters(params)
        return instance

    @classmethod
    def create_runtime_strategy(
        cls,
        name: str,
        params: Optional[dict] = None,
    ) -> Tuple[Optional[IStrategy], str]:
        payload = dict(params or {})
        normalized_name = str(name or "").strip().lower()
        dsl = payload.get("dsl")
        fallback_klass = cls.get(normalized_name)
        fallback_mode = (
            "missing_executable_contract"
            if normalized_name in _EXECUTABLE_DSL_REQUIRED_TYPES
            else "builtin_legacy"
        )
        if isinstance(dsl, dict) and dsl:
            try:
                from .dsl_strategy import DslRuleStrategy

                instance = DslRuleStrategy()
                instance.set_parameters(payload)
                return instance, "compiled_dsl"
            except Exception as exc:
                logger.warning("Failed to initialize compiled DSL strategy for %s: %s", normalized_name, exc)
                if fallback_klass is None:
                    return None, "missing_executable_contract"
                fallback = fallback_klass()
                if payload:
                    fallback.set_parameters(payload)
                return fallback, fallback_mode
        if fallback_klass is None:
            return None, "missing_executable_contract"
        instance = fallback_klass()
        if payload:
            instance.set_parameters(payload)
        return instance, fallback_mode

    @classmethod
    def list_all(cls) -> List[str]:
        return list(cls._strategies.keys())

    @classmethod
    def list_details(cls) -> List[dict]:
        return [
            {"name": k, "description": v.description()}
            for k, v in cls._strategies.items()
        ]
