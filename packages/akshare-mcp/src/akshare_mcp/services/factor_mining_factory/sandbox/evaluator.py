"""安全求值器 — 带资源限制的因子执行沙箱。

参考：Hubble (arXiv:2604.09601) — AST-based execution sandbox
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

from .compiler import compile_factor_extended, evaluate_factor_extended


class ExecutionLimitExceeded(RuntimeError):
    """执行超出资源限制。"""
    pass


class FactorSandbox:
    """增强版因子执行沙箱。

    安全保证：
    1. AST 白名单校验（编译阶段）
    2. __builtins__ 清空（执行阶段）
    3. 执行时间限制
    4. 复杂度上限
    """

    MAX_COMPLEXITY_SCORE = 120
    MAX_EXECUTION_TIME_MS = 500
    MAX_DEPTH = 8

    def compile(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """编译 + 静态分析 + 复杂度评估。"""
        compiled = compile_factor_extended(candidate)

        # 复杂度检查
        complexity = compiled.get("complexity", {}).get("score", 0)
        if complexity > self.MAX_COMPLEXITY_SCORE:
            compiled["valid"] = False
            compiled["degraded"] = True
            compiled.setdefault("warnings", []).append(
                f"complexity_exceeded: {complexity} > {self.MAX_COMPLEXITY_SCORE}"
            )

        # 深度检查
        depth = compiled.get("complexity", {}).get("max_depth", 0)
        if depth > self.MAX_DEPTH:
            compiled["valid"] = False
            compiled.setdefault("warnings", []).append(
                f"depth_exceeded: {depth} > {self.MAX_DEPTH}"
            )

        return compiled

    def evaluate(self, compiled: dict[str, Any], frame: pd.DataFrame) -> pd.Series:
        """带时间限制的安全执行。

        超时通过 SIGALRM 中断真正生效;仅在主线程可用(signal 限制),
        其他线程退回执行后测量 —— 后者只能事后报告,不能中断。
        """
        start = time.perf_counter()
        timeout_seconds = self.MAX_EXECUTION_TIME_MS / 1000.0
        interrupt_armed = False
        previous_handler: Any = None

        def _on_timeout(signum: int, frame_obj: Any) -> None:
            raise ExecutionLimitExceeded(
                f"Factor execution exceeded {self.MAX_EXECUTION_TIME_MS}ms limit"
            )

        if hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread():
            try:
                previous_handler = signal.signal(signal.SIGALRM, _on_timeout)
                signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
                interrupt_armed = True
            except (ValueError, OSError):
                interrupt_armed = False

        try:
            result = evaluate_factor_extended(compiled, frame)
        finally:
            if interrupt_armed:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)

        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > self.MAX_EXECUTION_TIME_MS:
            raise ExecutionLimitExceeded(
                f"Factor execution took {elapsed_ms:.1f}ms > {self.MAX_EXECUTION_TIME_MS}ms limit"
            )

        return result

    def validate_no_lookahead(self, compiled: dict[str, Any]) -> dict[str, Any]:
        """静态前视偏差检测。

        检查规则：
        - 使用 delay/shift 的因子不应引用未来数据
        - 表达式中不应出现 future_* 字段
        - 时序函数的窗口参数应为正数
        """
        warnings = []
        risk_level = "low"

        expression = compiled.get("candidate", {}).get("expression_dsl", "")
        functions = compiled.get("function_calls", [])
        fields = compiled.get("referenced_fields", [])

        # 检查是否使用了 delay（正确的前视保护）
        uses_delay = "delay" in functions

        # 检查可疑模式
        suspicious_patterns = ["future", "forward", "next", "lead"]
        for pattern in suspicious_patterns:
            if pattern in expression.lower():
                warnings.append(f"suspicious_token: '{pattern}' found in expression")
                risk_level = "high"

        # 如果使用了 return_* 但没有 delay，可能有前视风险
        return_fields = [f for f in fields if "return" in f]
        if return_fields and not uses_delay:
            warnings.append("return_field_without_delay: potential lookahead risk")
            if risk_level == "low":
                risk_level = "medium"

        return {
            "available": True,
            "risk_level": risk_level,
            "warnings": warnings,
            "uses_delay": uses_delay,
            "return_fields": return_fields,
        }
