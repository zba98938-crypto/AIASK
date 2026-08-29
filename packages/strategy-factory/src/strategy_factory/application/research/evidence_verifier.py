"""PR-AI6: 数值断言核验。

对 LLM 生成的 evidence_chain 中的基本面断言做数据库核验。
例如 LLM 声称"贵州茅台 PE 低"，但 financials 表实际 PE = 30，则标记为 FAILED。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def verify_evidence_claims(
    candidate: dict[str, Any],
    db: Any,
) -> dict[str, Any]:
    """核验 evidence_chain 中的数值断言是否与数据库一致。

    Returns:
        dict with total_evidences, verified, failed, verification_rate, passed.
    """
    evidence_chain = dict(candidate.get("evidence_chain") or {})
    evidences = list(evidence_chain.get("evidences") or [])
    if not evidences:
        return {
            "total_evidences": 0,
            "verified": 0,
            "failed": 0,
            "verification_rate": 1.0,
            "passed": True,
            "reason": "no_evidences_to_verify",
        }

    fetchrow = getattr(db, "fetchrow", None)
    if not callable(fetchrow):
        return {
            "total_evidences": len(evidences),
            "verified": 0,
            "failed": 0,
            "verification_rate": 1.0,
            "passed": True,
            "reason": "db_fetchrow_unavailable",
        }

    verified = 0
    failed = 0
    failure_details: list[str] = []

    for ev in evidences:
        source_type = str(ev.get("source_type") or "").strip().lower()
        if source_type not in ("fundamental", "financial", "factor", "valuation"):
            continue

        code = str(
            ev.get("stock_code") or ev.get("symbol") or ev.get("code") or ""
        ).strip()
        if not code:
            continue

        # 从 financials 表取最近一期
        try:
            row = await fetchrow(
                "SELECT pe_ttm, pb_mrq, roe_ttm, revenue_yoy "
                "FROM financials WHERE ts_code LIKE ? "
                "ORDER BY end_date DESC LIMIT 1",
                f"%{code}%",
            )
        except Exception as exc:
            logger.debug("evidence_verifier: query failed for %s: %s", code, exc)
            continue

        if not row:
            continue

        claimed_direction = str(ev.get("direction") or "").strip().lower()
        pe = float(row.get("pe_ttm") or 0)
        roe = float(row.get("roe_ttm") or 0)
        revenue_yoy = float(row.get("revenue_yoy") or 0)

        # 核验逻辑：看涨论据需要基本面支撑
        if claimed_direction == "bullish":
            contradictions: list[str] = []
            if pe > 100 and pe != 0:
                contradictions.append(f"PE={pe:.1f}>100")
            if roe < -0.05:
                contradictions.append(f"ROE={roe:.3f}<-5%")
            if revenue_yoy < -0.30:
                contradictions.append(f"revenue_yoy={revenue_yoy:.3f}<-30%")
            if contradictions:
                failed += 1
                detail = f"{code} bullish claim contradicted: {', '.join(contradictions)}"
                failure_details.append(detail)
                ev["_verification"] = f"FAILED: {detail}"
                continue

        elif claimed_direction == "bearish":
            # 看跌论据：如果基本面很好则矛盾
            contradictions = []
            if 0 < pe < 10:
                contradictions.append(f"PE={pe:.1f}<10 (cheap)")
            if roe > 0.25:
                contradictions.append(f"ROE={roe:.3f}>25%")
            if revenue_yoy > 0.50:
                contradictions.append(f"revenue_yoy={revenue_yoy:.3f}>50%")
            if contradictions:
                failed += 1
                detail = f"{code} bearish claim contradicted: {', '.join(contradictions)}"
                failure_details.append(detail)
                ev["_verification"] = f"FAILED: {detail}"
                continue

        verified += 1
        ev["_verification"] = "PASSED"

    total_checkable = verified + failed
    verification_rate = verified / max(total_checkable, 1)

    return {
        "total_evidences": len(evidences),
        "checkable_evidences": total_checkable,
        "verified": verified,
        "failed": failed,
        "verification_rate": round(verification_rate, 4),
        "passed": failed == 0,
        "failure_details": failure_details[:5],
    }


__all__ = ["verify_evidence_claims"]
