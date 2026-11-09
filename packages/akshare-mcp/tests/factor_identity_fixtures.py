"""Explicit prospective identities for synthetic factor admission fixtures."""
from akshare_mcp.services.factor_candidate_compiler import compile_factor_candidate
from akshare_mcp.services.factor_evidence_identity import build_factor_evidence_identity


def identity_outputs(expression, inputs=None):
    identity = build_factor_evidence_identity(
        compile_factor_candidate({"name": "synthetic", "hypothesis": "isolated admission fixture",
                                  "expression_dsl": expression, "inputs": inputs or ["close"]}),
        codes=["000001", "000002", "000003"], horizon_days=10,
        lookback_bars=220, max_dates=60, min_cross_section=30,
    )
    return {"factor_key": identity["factor_key"], "evidence_identity": identity}


def bind_candidate_fixture(candidate):
    result = dict(candidate.validation_result or {})
    result["persisted_outputs"] = {
        **dict(result.get("persisted_outputs") or {}),
        **identity_outputs(candidate.expression_dsl, candidate.inputs),
    }
    candidate.validation_result = result
    return candidate


def bind_record_fixture(record):
    summary = dict(record.get("validation_summary") or {})
    summary["persisted_outputs"] = {
        **dict(summary.get("persisted_outputs") or {}),
        **identity_outputs(record["expression_dsl"], record.get("inputs")),
    }
    return {**record, "validation_summary": summary}
