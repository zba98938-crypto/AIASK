"""调度器可观测性指标。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class SchedulerMetrics:
    """结构化调度器运行指标，支持 API 暴露和监控告警。"""

    total_cycles: int = 0
    successful_cycles: int = 0
    failed_cycles: int = 0
    skipped_cycles: int = 0
    circuit_breaker_trips: int = 0
    circuit_breaker_half_open_probes: int = 0
    avg_cycle_duration_sec: float = 0.0
    last_cycle_duration_sec: float = 0.0
    max_cycle_duration_sec: float = 0.0
    current_state: str = "idle"  # idle / running / circuit_open / half_open / shutting_down
    uptime_start: Optional[str] = None
    family_diversity_index: float = 0.0  # Shannon entropy
    last_error_type: Optional[str] = None
    last_error_time: Optional[str] = None
    last_success_time: Optional[str] = None
    consecutive_failures: int = 0
    ema_feedback_family_count: int = 0
    ema_feedback_persisted: bool = False

    # 内部累积（不暴露）
    _duration_sum: float = field(default=0.0, repr=False)

    def record_cycle_start(self) -> None:
        self.current_state = "running"

    def record_cycle_success(self, duration_sec: float, now: datetime) -> None:
        self.total_cycles += 1
        self.successful_cycles += 1
        self.last_cycle_duration_sec = round(duration_sec, 2)
        self.max_cycle_duration_sec = max(self.max_cycle_duration_sec, duration_sec)
        self._duration_sum += duration_sec
        self.avg_cycle_duration_sec = round(self._duration_sum / self.total_cycles, 2)
        self.last_success_time = now.isoformat()
        self.consecutive_failures = 0
        self.current_state = "idle"

    def record_cycle_failure(self, exc: BaseException, now: datetime) -> None:
        self.total_cycles += 1
        self.failed_cycles += 1
        self.last_error_type = type(exc).__name__
        self.last_error_time = now.isoformat()
        self.consecutive_failures += 1
        self.current_state = "idle"

    def record_cycle_skipped(self) -> None:
        self.skipped_cycles += 1

    def record_circuit_trip(self) -> None:
        self.circuit_breaker_trips += 1
        self.current_state = "circuit_open"

    def record_half_open_probe(self) -> None:
        self.circuit_breaker_half_open_probes += 1
        self.current_state = "half_open"

    def update_family_diversity(self, family_feedback: Dict[str, Dict[str, float]]) -> None:
        """计算 family 分布的 Shannon 熵作为多样性指标。"""
        self.ema_feedback_family_count = len(family_feedback)
        values = [
            max(0.001, data.get("ema_submit_count", 0.0))
            for data in family_feedback.values()
        ]
        if not values:
            self.family_diversity_index = 0.0
            return
        total = sum(values)
        if total <= 0:
            self.family_diversity_index = 0.0
            return
        entropy = -sum((v / total) * math.log2(v / total) for v in values if v > 0)
        max_entropy = math.log2(len(values)) if len(values) > 1 else 1.0
        self.family_diversity_index = round(entropy / max_entropy, 4) if max_entropy > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        """导出为可序列化的字典（排除内部字段）。"""
        result = asdict(self)
        result.pop("_duration_sum", None)
        return result
