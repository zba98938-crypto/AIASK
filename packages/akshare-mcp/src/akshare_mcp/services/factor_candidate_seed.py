"""Seed auditable factor-candidate source records from the local factor library."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..tools.quant_definitions import SUPPORTED_FACTORS
from .factor_candidate_compiler import compile_factor_candidate
from .factor_candidate_storage import (
    get_factor_candidate_record_async,
    save_factor_candidate_record_async,
)


_CORE_PRIORITY = (
    "momentum",
    "trend",
    "reversal",
    "volatility",
    "volume_ratio",
    "turnover_20d",
    "mom_5d",
    "mom_10d",
    "mom_60d",
    "vol_10d",
    "vol_60d",
    "atr_20",
    "value",
    "quality",
    "growth",
    "size",
    "pe_ttm",
    "pb_mrq",
    "roe_ttm",
    "revenue_growth_yoy",
    "capital_flow",
    "sentiment_score",
)

_EXPRESSION_BY_FACTOR = {
    "momentum": ("zscore(momentum_20d, 20) + zscore(momentum_60d, 20)", ["momentum_20d", "momentum_60d"]),
    "trend": ("ts_mean(returns_1d, 20) + zscore(momentum_20d, 20)", ["returns_1d", "momentum_20d"]),
    "reversal": ("-return_5d", ["return_5d"]),
    "volatility": ("-zscore(volatility_20d, 20)", ["volatility_20d"]),
    "volume_ratio": ("zscore(volume_ratio_5_20, 10)", ["volume_ratio_5_20"]),
    "turnover_20d": ("zscore(volume_ratio_5_20, 20)", ["volume_ratio_5_20"]),
    "mom_1d": ("returns_1d", ["returns_1d"]),
    "mom_5d": ("return_5d", ["return_5d"]),
    "mom_10d": ("zscore(return_5d, 10)", ["return_5d"]),
    "mom_60d": ("momentum_60d", ["momentum_60d"]),
    "vol_5d": ("-ts_std(returns_1d, 5)", ["returns_1d"]),
    "vol_10d": ("-ts_std(returns_1d, 10)", ["returns_1d"]),
    "vol_60d": ("-ts_std(returns_1d, 60)", ["returns_1d"]),
    "atr_14": ("(high - low) / close", ["high", "low", "close"]),
    "atr_20": ("ts_mean((high - low) / close, 20)", ["high", "low", "close"]),
    "bollinger_width": ("ts_std(close, 20) / ts_mean(close, 20)", ["close"]),
    "downside_vol": ("-ts_std(min(returns_1d, 0), 20)", ["returns_1d"]),
    "capital_flow": ("zscore(volume_ratio_5_20, 10) + zscore(momentum_20d, 20)", ["volume_ratio_5_20", "momentum_20d"]),
    "sentiment_score": ("zscore(momentum_20d, 20) - zscore(volatility_20d, 20)", ["momentum_20d", "volatility_20d"]),
}

_CATEGORY_EXPRESSION = {
    "technical": ("zscore(momentum_20d, 20) + zscore(momentum_60d, 20)", ["momentum_20d", "momentum_60d"]),
    "risk": ("-zscore(volatility_20d, 20)", ["volatility_20d"]),
    "volume": ("zscore(volume_ratio_5_20, 10)", ["volume_ratio_5_20"]),
    "alternative": ("zscore(momentum_20d, 20) + zscore(volume_ratio_5_20, 10)", ["momentum_20d", "volume_ratio_5_20"]),
    "fundamental": ("zscore(momentum_20d, 20) - zscore(volatility_20d, 20)", ["momentum_20d", "volatility_20d"]),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_codes(codes: Any) -> list[str]:
    if isinstance(codes, str):
        raw = codes.replace("|", ",").replace(";", ",").split(",")
    elif isinstance(codes, (list, tuple, set)):
        raw = list(codes)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        code = str(item or "").strip()
        if code and code not in out:
            out.append(code)
    return out


async def _sample_codes(db, *, limit: int = 12) -> list[str]:
    if not hasattr(db, "acquire"):
        return []
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT stock_code AS code
                FROM stocks
                WHERE stock_code IS NOT NULL AND stock_code != ''
                ORDER BY stock_code
                LIMIT $1
                """,
                max(1, min(int(limit or 12), 100)),
            )
        return [str(row.get("code") or "").strip() for row in rows if str(row.get("code") or "").strip()]
    except Exception:
        return []


def _ordered_factor_names(limit: int) -> list[str]:
    ordered: list[str] = []
    for name in _CORE_PRIORITY:
        if name in SUPPORTED_FACTORS and name not in ordered:
            ordered.append(name)
    for name in sorted(SUPPORTED_FACTORS):
        if name not in ordered:
            ordered.append(name)
    return ordered[: max(1, min(int(limit or len(ordered)), len(ordered)))]


def _candidate_for_factor(name: str, meta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    category = str(meta.get("category") or "custom").strip().lower() or "custom"
    expression, inputs = _EXPRESSION_BY_FACTOR.get(name) or _CATEGORY_EXPRESSION.get(category) or _CATEGORY_EXPRESSION["technical"]
    candidate = {
        "name": f"library_{name}",
        "family": category,
        "hypothesis": (
            f"{meta.get('description') or name} 可作为候选信号进入后续横截面验证；"
            "该记录由本地因子库派生，尚未代表验证通过。"
        ),
        "inputs": list(inputs),
        "expression_dsl": expression,
        "expected_holding_period": 20 if category in {"fundamental", "risk"} else 10,
        "expected_regime": [category, "factor_library_seed"],
        "complexity_hint": "low" if len(inputs) <= 1 else "medium",
        "novelty_rationale": "Seeded from SUPPORTED_FACTORS to provide an auditable candidate source for vector memory.",
        "generation_trace": {
            "mode": "factor_library_seed",
            "source": "SUPPORTED_FACTORS",
            "factor_name": name,
            "category": category,
            "requires_financials": bool(meta.get("requires_financials")),
        },
        "source_model": "local_factor_library_seed",
    }
    try:
        compiled = compile_factor_candidate(candidate)
    except Exception as exc:
        compiled = {"valid": False, "warnings": [f"compile_exception:{type(exc).__name__}"]}
    return candidate, compiled


async def seed_factor_candidate_records(
    db,
    *,
    limit: Any = 32,
    codes: Any = None,
    rebuild_existing: Any = False,
    dry_run: Any = False,
) -> dict[str, Any]:
    """Persist factor-library candidates as source records for factor vector backfill."""

    resolved_limit = max(1, min(int(limit or 32), len(SUPPORTED_FACTORS)))
    resolved_codes = _normalize_codes(codes) or await _sample_codes(db, limit=12)
    resolved_rebuild = bool(rebuild_existing)
    resolved_dry_run = bool(dry_run)
    result: dict[str, Any] = {
        "source": "SUPPORTED_FACTORS",
        "limit": resolved_limit,
        "codes": resolved_codes,
        "candidate_records": 0,
        "saved_records": 0,
        "skipped_existing_records": 0,
        "compile_valid_records": 0,
        "compile_degraded_records": 0,
        "dry_run": resolved_dry_run,
        "errors": [],
    }

    for factor_name in _ordered_factor_names(resolved_limit):
        meta = dict(SUPPORTED_FACTORS.get(factor_name) or {})
        artifact_id = f"factor_memory_seed_{factor_name}"
        result["candidate_records"] += 1
        if not resolved_rebuild and await get_factor_candidate_record_async(artifact_id):
            result["skipped_existing_records"] += 1
            continue
        candidate, compiled = _candidate_for_factor(factor_name, meta)
        if bool(compiled.get("valid")):
            result["compile_valid_records"] += 1
        else:
            result["compile_degraded_records"] += 1
        record = {
            "artifact_id": artifact_id,
            "status": "review",
            "codes": resolved_codes,
            "candidate": candidate,
            "family": candidate.get("family"),
            "tags": [
                "factor_library_seed",
                str(meta.get("category") or "custom").strip().lower(),
                "requires_validation",
            ],
            "rating": {
                "grade": "C",
                "recommendation": "review",
                "reason": "Seed source record only; run validate_factor_candidate before promotion.",
            },
            "metrics": {
                "rank_ic_mean": 0.0,
                "source_sample_codes": len(resolved_codes),
            },
            "memory_flags": {
                "seeded_from_factor_library": True,
                "requires_validation": True,
                "compiler_valid": bool(compiled.get("valid")),
                "compiler_warnings": list(compiled.get("warnings") or []),
                "unsupported_fields": list(compiled.get("unsupported_fields") or []),
                "unsupported_functions": list(compiled.get("unsupported_functions") or []),
            },
            "source_chain": ["tools.quant_definitions.SUPPORTED_FACTORS", "services.factor_candidate_seed"],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        if resolved_dry_run:
            result["saved_records"] += 1
            continue
        try:
            await save_factor_candidate_record_async(record, artifact_id=artifact_id)
            result["saved_records"] += 1
        except Exception as exc:
            result["errors"].append(f"{artifact_id}:{type(exc).__name__}")

    if len(result["errors"]) > 20:
        total = len(result["errors"])
        result["errors"] = list(result["errors"][:20]) + [f"...and {total - 20} more"]
    return result

