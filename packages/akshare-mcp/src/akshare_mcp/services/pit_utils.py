"""PIT (Point-In-Time) 时点一致性工具

提供统一的时点切片约束，确保研究、预测、情绪、决策、回测过程中
不会使用 available_time > as_of_time 的未来数据。

核心概念
--------
- event_time:     数据描述的时间点（如财报报告期）
- ingest_time:    数据被录入系统的时间
- available_time: 市场参与者实际可见该数据的时间（通常 = ingest_time + 公告延迟）
- as_of_time:     研究/决策的基准时间点，所有数据必须满足 available_time <= as_of_time

使用方式
--------
```python
from akshare_mcp.services.pit_utils import PITContext, pit_filter_records, as_of_now

ctx = PITContext(as_of="2025-12-31")  # 或 as_of_now()
safe_records = pit_filter_records(records, ctx, available_time_key="available_time")
```
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_datetime(value: str | datetime.datetime | None) -> datetime.datetime | None:
    """将字符串或 datetime 解析为 aware datetime（UTC）。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y%m%d",
    ):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


# ── PITContext ────────────────────────────────────────────────────────────────

@dataclass
class PITContext:
    """时点一致性上下文。

    Parameters
    ----------
    as_of:
        基准时间点。None 表示当前时间（不做未来信息截断）。
        支持 ISO 字符串、date 字符串（被扩展为当天 23:59:59）或 datetime。
    strict:
        True 时对缺失 available_time 的记录报告警告（不阻断）。
        False 时静默跳过（默认）。
    """

    as_of: str | datetime.datetime | None = None
    strict: bool = False
    _as_of_dt: datetime.datetime | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        if self.as_of is None:
            self._as_of_dt = None  # 不截断，使用当前时间
        else:
            dt = _parse_datetime(self.as_of)
            if dt is None:
                raise ValueError(f"无法解析 as_of 时间：{self.as_of!r}")
            # 如果只有日期部分，扩展到当天末尾
            if isinstance(self.as_of, str) and len(self.as_of.strip()) == 10:
                dt = dt.replace(hour=23, minute=59, second=59)
            self._as_of_dt = dt

    @property
    def as_of_datetime(self) -> datetime.datetime:
        """返回基准时间，None 时返回当前时间。"""
        return self._as_of_dt if self._as_of_dt is not None else _now_utc()

    def is_available(self, available_time: str | datetime.datetime | None) -> bool:
        """检查某条记录的 available_time 是否满足 PIT 约束。

        Returns True if available_time <= as_of，或 available_time 为空（宽松模式）。
        """
        if available_time is None:
            return not self.strict
        dt = _parse_datetime(available_time)
        if dt is None:
            return not self.strict
        return dt <= self.as_of_datetime

    def summary(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of_datetime.isoformat(),
            "strict": self.strict,
        }


def as_of_now(strict: bool = False) -> PITContext:
    """返回以当前时间为基准的 PITContext（不截断）。"""
    return PITContext(as_of=None, strict=strict)


def as_of(date_or_dt: str | datetime.datetime, strict: bool = False) -> PITContext:
    """返回以指定时间为基准的 PITContext。"""
    return PITContext(as_of=date_or_dt, strict=strict)


# ── 过滤函数 ──────────────────────────────────────────────────────────────────

def pit_filter_records(
    records: list[dict[str, Any]],
    ctx: PITContext,
    available_time_key: str = "available_time",
) -> list[dict[str, Any]]:
    """对记录列表做 PIT 过滤，移除 available_time > as_of 的未来记录。

    Parameters
    ----------
    records:
        原始记录列表。
    ctx:
        PITContext 实例。
    available_time_key:
        记录中表示可见时间的字段名，支持多候选（逗号分隔）。

    Returns
    -------
    满足 PIT 约束的记录列表（顺序保持）。
    """
    if not records:
        return records

    keys = [k.strip() for k in available_time_key.split(",")]

    def get_available_time(record: dict[str, Any]) -> str | None:
        for k in keys:
            if k in record and record[k] is not None:
                return str(record[k])
        return None

    return [r for r in records if ctx.is_available(get_available_time(r))]


def pit_filter_dict(
    data: dict[str, Any],
    ctx: PITContext,
    available_time_key: str = "available_time",
) -> dict[str, Any] | None:
    """对单条记录做 PIT 检查，不满足时返回 None。"""
    if not isinstance(data, dict):
        return data
    at = data.get(available_time_key)
    if not ctx.is_available(at):
        return None
    return data


def annotate_pit_compliance(
    records: list[dict[str, Any]],
    ctx: PITContext,
    available_time_key: str = "available_time",
) -> list[dict[str, Any]]:
    """在每条记录上添加 _pit_ok 和 _pit_as_of 字段，不删除记录。"""
    keys = [k.strip() for k in available_time_key.split(",")]
    as_of_str = ctx.as_of_datetime.isoformat()

    result = []
    for r in records:
        at = next((str(r[k]) for k in keys if k in r and r[k] is not None), None)
        ok = ctx.is_available(at)
        result.append({**r, "_pit_ok": ok, "_pit_as_of": as_of_str})
    return result


# ── PIT 审计 ──────────────────────────────────────────────────────────────────

@dataclass
class PITAuditResult:
    """PIT 合规审计结果。"""

    total: int
    compliant: int
    violated: int
    missing_available_time: int
    violation_examples: list[dict[str, Any]]
    compliance_rate: float
    risk_level: str  # 'low' / 'medium' / 'high'
    as_of: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "compliant": self.compliant,
            "violated": self.violated,
            "missing_available_time": self.missing_available_time,
            "violation_examples": self.violation_examples,
            "compliance_rate": self.compliance_rate,
            "risk_level": self.risk_level,
            "as_of": self.as_of,
            "summary": self.summary,
        }


def pit_audit(
    records: list[dict[str, Any]],
    ctx: PITContext,
    available_time_key: str = "available_time",
    max_examples: int = 5,
) -> PITAuditResult:
    """对数据集做 PIT 合规审计，返回详细报告。"""
    keys = [k.strip() for k in available_time_key.split(",")]
    total = len(records)
    compliant = 0
    violated = 0
    missing = 0
    examples: list[dict[str, Any]] = []

    for r in records:
        at = next((str(r[k]) for k in keys if k in r and r[k] is not None), None)
        if at is None:
            missing += 1
            # 宽松模式：缺失 available_time 计为合规
            compliant += 1
            continue
        if ctx.is_available(at):
            compliant += 1
        else:
            violated += 1
            if len(examples) < max_examples:
                examples.append({"available_time": at, "record_snippet": {k: v for k, v in list(r.items())[:4]}})

    rate = round(compliant / total, 4) if total > 0 else 1.0
    risk = "low" if violated == 0 else "medium" if rate >= 0.95 else "high"

    summary_parts = [f"共 {total} 条记录，合规 {compliant} 条，违规 {violated} 条"]
    if missing > 0:
        summary_parts.append(f"缺失 available_time {missing} 条（宽松模式下视为合规）")
    if violated > 0:
        summary_parts.append(f"存在未来信息泄露风险，建议检查数据管道")

    return PITAuditResult(
        total=total,
        compliant=compliant,
        violated=violated,
        missing_available_time=missing,
        violation_examples=examples,
        compliance_rate=rate,
        risk_level=risk,
        as_of=ctx.as_of_datetime.isoformat(),
        summary="；".join(summary_parts),
    )
