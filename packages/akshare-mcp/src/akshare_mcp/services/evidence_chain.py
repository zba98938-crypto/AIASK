"""P1-A: 决策证据链 — 结构化证据入库 + 审计追踪。

设计:
- EvidenceItem: 单条证据（因子/估值/风险/技术面），含来源、权重、得分贡献
- EvidenceChain: 一次决策的完整证据链，含 trace_id、股票代码、时间戳
- 存储: 内存缓存 + 异步 DB 持久化（与 artifact_registry 同模式）
- 查询: 按 trace_id / 股票代码 / 日期范围
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from .background_tasks import track_background_task

logger = logging.getLogger(__name__)

# ── 内存缓存（LRU 淘汰）───────────────────────────────────────────
_MAX_CHAINS: int = 1000  # 最多缓存的证据链数量
_CHAINS: OrderedDict[str, dict[str, Any]] = OrderedDict()  # trace_id -> chain
_CODE_INDEX: dict[str, list[str]] = {}            # code -> [trace_id, ...]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db():
    """延迟导入，避免循环依赖。"""
    try:
        from ..storage.sqlite import get_db
        return get_db()
    except Exception:
        return None


# ── EvidenceItem 构造 ─────────────────────────────────────

def make_evidence(
    evidence_type: str,
    source_module: str,
    metric_name: str,
    raw_value: Any,
    score: float,
    weight: float,
    *,
    score_contribution: float | None = None,
    confidence: float | None = None,
    benchmark: Any = None,
    detail: dict | None = None,
) -> dict:
    """构造单条证据项。

    Args:
        evidence_type: factor / valuation / risk / technical / sentiment
        source_module: 来源模块名，如 'decision.should_i_buy'
        metric_name: 指标名，如 'pe_ratio', 'rsi_14', 'ma_trend'
        raw_value: 原始值
        score: 该维度得分 (0-100)
        weight: 在综合评分中的权重
        score_contribution: 对总分的贡献（= score * weight），可自动计算
        confidence: 置信度 (0-1)，None 表示未评估
        benchmark: 参考基准值（行业均值等）
        detail: 附加明细
    """
    if score_contribution is None:
        score_contribution = round(score * weight, 4)
    return {
        "evidence_type": evidence_type,
        "source_module": source_module,
        "metric_name": metric_name,
        "raw_value": raw_value,
        "score": float(score),
        "weight": float(weight),
        "score_contribution": float(score_contribution),
        "confidence": float(confidence) if confidence is not None else None,
        "benchmark": benchmark,
        "detail": detail or {},
        "timestamp": _now_iso(),
    }


# ── EvidenceChain 管理 ────────────────────────────────────

def create_chain(
    trace_id: str,
    code: str,
    action: str,
    *,
    tool_version: str = "v1.1",
    extra: dict | None = None,
) -> dict:
    """创建一条新的证据链。"""
    chain = {
        "trace_id": trace_id,
        "code": code,
        "action": action,
        "tool_version": tool_version,
        "created_at": _now_iso(),
        "evidences": [],
        "conclusion": None,
        "extra": extra or {},
    }
    return chain


def add_evidence(chain: dict, evidence: dict) -> dict:
    """向证据链追加一条证据。"""
    chain["evidences"].append(evidence)
    return chain


def set_conclusion(
    chain: dict,
    recommendation: str,
    total_score: float,
    raw_total_score: float,
    reason: str,
    *,
    confidence: float | None = None,
    data_quality: dict | None = None,
) -> dict:
    """设置证据链的最终结论。"""
    chain["conclusion"] = {
        "recommendation": recommendation,
        "total_score": float(total_score),
        "raw_total_score": float(raw_total_score),
        "reason": reason,
        "confidence": confidence,
        "data_quality": data_quality,
        "concluded_at": _now_iso(),
    }
    return chain


# ── 持久化 ────────────────────────────────────────────────

def save_chain(chain: dict) -> dict:
    """保存证据链：内存缓存 + 异步 DB。"""
    tid = chain.get("trace_id", "")
    if not tid:
        raise ValueError("trace_id is required")

    payload = deepcopy(chain)
    payload["updated_at"] = _now_iso()

    # 内存缓存（LRU 淘汰）
    _CHAINS[tid] = deepcopy(payload)
    _CHAINS.move_to_end(tid)  # 标记为最近使用
    while len(_CHAINS) > _MAX_CHAINS:
        _CHAINS.popitem(last=False)  # 淘汰最老的条目

    # 代码索引
    code = payload.get("code", "")
    if code:
        if code not in _CODE_INDEX:
            _CODE_INDEX[code] = []
        if tid not in _CODE_INDEX[code]:
            _CODE_INDEX[code].append(tid)

    # 异步 DB
    db = _get_db()
    if db is not None:
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                track_background_task(_safe_save_chain(db, payload), name=f"evidence-save:{tid}")
        except RuntimeError:
            logger.debug("No event loop; chain %s cached in memory only", tid)

    return deepcopy(payload)


async def _safe_save_chain(db, payload: dict) -> None:
    """安全写入 DB。"""
    try:
        if hasattr(db, "save_evidence_chain"):
            await db.save_evidence_chain(payload)
        else:
            # DB 尚未实现该方法时，降级为 artifact 存储
            artifact = {
                "artifact_id": f"evidence:{payload['trace_id']}",
                "artifact_type": "evidence_chain",
                "strategy_version": payload.get("tool_version", ""),
                "code": payload.get("code", ""),
                "payload": payload,
                "registered_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
            }
            if hasattr(db, "save_artifact"):
                await db.save_artifact(artifact)
    except Exception as exc:
        logger.warning("Failed to persist evidence chain %s: %s",
                       payload.get("trace_id"), exc)


# ── 查询 ──────────────────────────────────────────────────

def get_chain(trace_id: str) -> dict | None:
    """按 trace_id 查询证据链。"""
    item = _CHAINS.get(trace_id)
    return deepcopy(item) if item else None


def query_chains_by_code(code: str, limit: int = 20) -> list[dict]:
    """按股票代码查询证据链（最近优先）。"""
    tids = _CODE_INDEX.get(code, [])
    chains = [_CHAINS[t] for t in tids if t in _CHAINS]
    chains.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return [deepcopy(c) for c in chains[:max(1, int(limit))]]


def query_chains_by_date(
    start_date: str | None = None,
    end_date: str | None = None,
    code: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """按日期范围查询证据链。"""
    results = []
    for chain in _CHAINS.values():
        created = chain.get("created_at", "")
        if start_date and created < start_date:
            continue
        if end_date and created > end_date:
            continue
        if code and chain.get("code") != code:
            continue
        results.append(chain)

    results.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return [deepcopy(c) for c in results[:max(1, int(limit))]]


def list_chains(limit: int = 20) -> list[dict]:
    """返回最近的证据链摘要。"""
    chains = list(_CHAINS.values())
    chains.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    out = []
    for c in chains[:max(1, int(limit))]:
        conclusion = c.get("conclusion") or {}
        out.append({
            "trace_id": c.get("trace_id"),
            "code": c.get("code"),
            "action": c.get("action"),
            "recommendation": conclusion.get("recommendation"),
            "total_score": conclusion.get("total_score"),
            "evidence_count": len(c.get("evidences", [])),
            "created_at": c.get("created_at"),
        })
    return out


# ── 审计摘要 ──────────────────────────────────────────────

def summarize_chain(chain: dict) -> dict:
    """生成证据链的审计摘要（用于返回给调用方）。"""
    evidences = chain.get("evidences", [])
    conclusion = chain.get("conclusion") or {}

    by_type: dict[str, list[dict]] = {}
    for ev in evidences:
        t = ev.get("evidence_type", "unknown")
        by_type.setdefault(t, []).append({
            "metric": ev.get("metric_name"),
            "value": ev.get("raw_value"),
            "score": ev.get("score"),
            "weight": ev.get("weight"),
            "contribution": ev.get("score_contribution"),
            "confidence": ev.get("confidence"),
        })

    return {
        "trace_id": chain.get("trace_id"),
        "code": chain.get("code"),
        "action": chain.get("action"),
        "evidence_summary": by_type,
        "evidence_count": len(evidences),
        "conclusion": conclusion,
        "created_at": chain.get("created_at"),
    }
