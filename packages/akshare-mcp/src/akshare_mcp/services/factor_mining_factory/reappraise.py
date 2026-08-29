"""Quarantine factor re-appraisal (盘活存量).

诊断结论：因子晋升 evidence 门历史上用"单轮验证窗口新增 IC 行数"判 min_ic_history_rows,
导致已积累充足历史、质量达标的因子被误降级到 quarantine 后再无晋升路径(鸡生蛋死锁)。
quality.evaluate_validation_evidence 已修为用累计历史,但因子挖掘每轮只验证新候选,
不会重新评估 quarantine 存量。本模块提供基于 DB 累计 IC 历史的存量复评:
合格(strict 门)的 quarantine 因子转回 active。

纯基于已持久化的 factor_ic_history 累计数据复评,不重跑因子计算,安全且可复算。
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone
from typing import Any

from .quality import QUALITY_THRESHOLDS

logger = logging.getLogger(__name__)


async def _load_factor_ic_rows(db: Any, factor_name: str) -> list[dict[str, Any]]:
    """读某因子全部已持久化 IC 行(不限 period),用于累计质量复评。"""
    if not hasattr(db, "acquire"):
        return []
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT rank_ic, ic_value, stock_count FROM factor_ic_history WHERE factor_name = $1",
            factor_name,
        )
    return [dict(r) for r in rows or []]


def _summarize_ic(rows: list[dict[str, Any]]) -> dict[str, float]:
    rics = [float(r["rank_ic"]) for r in rows if r.get("rank_ic") is not None]
    scs = [int(r["stock_count"]) for r in rows if r.get("stock_count")]
    if len(rics) < 2:
        return {"rows": len(rics), "rank_ic_mean": 0.0, "rank_ic_ir": 0.0, "avg_cross": 0.0, "positive_ratio": 0.0}
    mean = statistics.mean(rics)
    sd = statistics.pstdev(rics) or 1e-9
    return {
        "rows": len(rics),
        "rank_ic_mean": mean,
        "rank_ic_ir": mean / sd,
        "avg_cross": (sum(scs) / len(scs)) if scs else 0.0,
        "positive_ratio": sum(1 for x in rics if x > 0) / len(rics),
    }


def _passes_strict(summary: dict[str, float], thresholds: dict[str, float]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if summary["rows"] < thresholds["min_ic_history_rows"]:
        reasons.append("ic_history_rows_below_min")
    if abs(summary["rank_ic_ir"]) < thresholds["min_rank_ic_ir"]:
        reasons.append("rank_ic_ir_below_min")
    if abs(summary["rank_ic_mean"]) < thresholds["min_abs_rank_ic_mean"]:
        reasons.append("rank_ic_mean_below_min")
    if summary["avg_cross"] < thresholds["min_avg_cross_section_n"]:
        reasons.append("avg_cross_section_n_below_min")
    if summary["positive_ratio"] < thresholds["min_positive_ratio"]:
        reasons.append("positive_ratio_below_min")
    return (not reasons), reasons


def _is_reappraisal_candidate(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    validation_summary = dict(row.get("validation_summary") or {})
    quality_status = str(validation_summary.get("quality_status") or "").strip().lower()
    return status == "quarantine" or (status == "active" and quality_status == "quarantine")


async def reappraise_quarantine_factors(
    db: Any,
    *,
    limit: int = 200,
    dry_run: bool = False,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """复评 quarantine 存量因子,基于累计 IC 历史把达标因子转回 active。

    Returns: {scanned, promoted, kept_quarantine, dry_run, items}
    """
    from .pool.storage import load_factor_pool_from_db, save_factor_to_pool

    th = {**QUALITY_THRESHOLDS, **dict(thresholds or {})}
    rows = [
        row
        for row in await load_factor_pool_from_db(
            db,
            statuses=("active", "quarantine"),
            limit=limit,
        )
        if _is_reappraisal_candidate(dict(row or {}))
    ]
    scanned = 0
    promoted = 0
    kept = 0
    items: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for row in rows or []:
        scanned += 1
        name = str(row.get("name") or "").strip()
        if not name:
            kept += 1
            continue
        ic_rows = await _load_factor_ic_rows(db, name)
        summary = _summarize_ic(ic_rows)
        ok, reasons = _passes_strict(summary, th)
        item = {
            "factor_id": row.get("factor_id"),
            "name": name,
            "ic_rows": summary["rows"],
            "rank_ic_ir": round(summary["rank_ic_ir"], 4),
            "rank_ic_mean": round(summary["rank_ic_mean"], 5),
            "promoted": False,
            "reasons": reasons,
        }
        if ok:
            promoted += 1
            item["promoted"] = True
            if not dry_run:
                record = dict(row)
                record["status"] = "active"
                # P2-1: 同时把 validation_summary.quality_status 写成 promoted。
                # 原 bug:只翻 status=active 不动 quality_status(仍 quarantine)→ 消费侧
                # (active_pool.get_active_factors / factor_research_builder)要求 quality_status≠quarantine,
                # 导致 reappraise 转正的因子永远取不到(半转正僵态,因子实质无法进策略)。
                validation_summary = dict(record.get("validation_summary") or {})
                validation_summary["quality_status"] = "promoted"
                validation_summary["promoted_via"] = "quarantine_reappraisal"
                validation_summary["promotion_block_reasons"] = []
                record["validation_summary"] = validation_summary
                record["quality_status"] = "promoted"
                record["current_ic"] = round(summary["rank_ic_mean"], 6)
                record.setdefault("admission_ic", round(summary["rank_ic_mean"], 6))
                record.setdefault("admission_grade", "B")
                record["admission_date"] = record.get("admission_date") or now
                record["last_evaluated_at"] = now
                trace = dict(record.get("generation_trace") or {})
                trace["reappraised_from_quarantine_at"] = now
                trace["reappraisal_ic_rows"] = summary["rows"]
                trace["reappraisal_rank_ic_ir"] = round(summary["rank_ic_ir"], 4)
                record["generation_trace"] = trace
                try:
                    await save_factor_to_pool(db, record)
                except Exception as exc:  # noqa: BLE001
                    item["promoted"] = False
                    item["error"] = f"{type(exc).__name__}: {exc}"
                    promoted -= 1
                    kept += 1
        else:
            kept += 1
        items.append(item)

    logger.info(
        "QuarantineReappraisal: scanned=%d promoted=%d kept=%d dry_run=%s",
        scanned, promoted, kept, dry_run,
    )
    return {
        "scanned": scanned,
        "promoted": promoted,
        "kept_quarantine": kept,
        "dry_run": bool(dry_run),
        "items": items[:50],
    }
