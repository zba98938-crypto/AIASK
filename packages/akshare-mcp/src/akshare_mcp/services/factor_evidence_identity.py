"""Bind mined factor history to its formula and evaluation contract, not its label."""
from __future__ import annotations

import hashlib
import json
from typing import Any

VERSION = "factor_evidence.v1"
KEY_PREFIX = "factor_evidence:v1:"


def requires_bound_evidence(candidate: dict[str, Any]) -> bool:
    """Mined/DSL records require a formula-bound key before pool consumption.

    Built-in factor rows are evaluated by their own calculators and remain
    outside this contract.  A mined display label (``gp_factor_6`` etc.) is
    never sufficient evidence because labels have historically been reused.
    """
    name = str(candidate.get("name") or "").strip().lower()
    engine = str(candidate.get("generation_engine") or "").strip().lower()
    return (
        name.startswith(("gp_", "mcts_", "rl_"))
        or engine.startswith(("gp", "mcts", "rl"))
        or str(candidate.get("expression_dsl") or "").strip() != ""
        and str(candidate.get("family") or "").strip().lower() == "custom"
    )


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _formula(compiled: dict[str, Any]) -> dict[str, Any]:
    if not compiled.get("valid") or not compiled.get("expression_ast"):
        raise ValueError("factor_evidence_compile_invalid")
    return {
        "expression_ast": compiled["expression_ast"],
        "declared_inputs": sorted(set(compiled.get("declared_inputs") or [])),
        "referenced_fields": sorted(set(compiled.get("referenced_fields") or [])),
    }


def build_factor_evidence_identity(compiled: dict[str, Any], *, codes: list[str],
                                  horizon_days: int, lookback_bars: int,
                                  max_dates: int, min_cross_section: int) -> dict[str, Any]:
    contract = {
        "version": VERSION,
        "formula": _formula(compiled),
        "evaluation": {
            "horizon_days": int(horizon_days), "lookback_bars": int(lookback_bars),
            "max_dates": max(20, int(max_dates)), "min_cross_section": max(3, int(min_cross_section or 3)),
            "codes": sorted(set(str(code).strip() for code in codes if str(code).strip())),
        },
    }
    digest = _digest(contract)
    return {"status": "bound", "contract": contract, "sha256": digest, "factor_key": KEY_PREFIX + digest}


def resolve_factor_evidence_identity(candidate: dict[str, Any], evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reject absent, tampered or different-formula history without alias fallback."""
    from .factor_candidate_compiler import compile_factor_candidate

    result = evidence if evidence is not None else candidate.get("validation_summary") or {}
    if not isinstance(result, dict) or not isinstance(result.get("persisted_outputs", {}), dict):
        return {"status": "invalid", "reason": "factor_evidence_identity_invalid"}
    outputs = result.get("persisted_outputs") or {}
    identity = outputs.get("evidence_identity") or {}
    if not identity:
        return {"status": "unknown_legacy", "reason": "factor_evidence_identity_missing"}
    if not isinstance(identity, dict) or not isinstance(identity.get("contract"), dict):
        return {"status": "invalid", "reason": "factor_evidence_identity_invalid"}
    contract = identity["contract"]
    digest = _digest(contract)
    if (contract.get("version") != VERSION or identity.get("status") != "bound"
            or identity.get("sha256") != digest or identity.get("factor_key") != KEY_PREFIX + digest
            or outputs.get("factor_key") != KEY_PREFIX + digest):
        return {"status": "invalid", "reason": "factor_evidence_identity_invalid"}
    try:
        compile_payload = {**candidate, "hypothesis": candidate.get("hypothesis") or "evidence identity check"}
        if contract.get("formula") != _formula(compile_factor_candidate(compile_payload)):
            return {"status": "mismatched", "reason": "factor_evidence_formula_mismatch"}
        evaluation = dict(contract.get("evaluation") or {})
        period = int(evaluation["horizon_days"])
        if (period <= 0 or not evaluation.get("codes")
                or int(evaluation["lookback_bars"]) <= 0
                or int(evaluation["max_dates"]) < 20
                or int(evaluation["min_cross_section"]) < 3):
            raise ValueError("invalid evaluation")
    except (TypeError, ValueError, KeyError):
        return {"status": "invalid", "reason": "factor_evidence_identity_invalid"}
    return {**identity, "period": str(period), "reason": None}
