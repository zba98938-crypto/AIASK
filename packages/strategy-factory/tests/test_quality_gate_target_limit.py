"""PR-E (Phase 3, 2026-05-24) — target contract v2 quality-gate tests.

Drives ``strategy_factory.application.quality_gates.pre_gate_screen``
(the public surface that wraps ``evaluation.py`` fragments) and asserts
that the new dynamic ``target_symbol_count_exceeds_limit:<N>`` reason
appears with the correct numeric limit for each scenario.

Coverage:
    - 5a default (DYNAMIC_TARGET_COUNT_ENABLED=False):
        * snapshot capped at 12
        * event_driven also capped at 12 (no 5a/5b mismatch)
    - 5b enabled (DYNAMIC_TARGET_COUNT_ENABLED=True):
        * snapshot still capped at 12
        * event_driven allowed up to TARGET_COUNT_MAX (default 30)
    - target_alignment_contract.max_candidate_target_symbols overrides
      the resolver fallback (priority #1).
    - research_task.target_symbol_limit overrides the resolver fallback
      when no contract value is present (priority #2).
    - The reject reason carries the *actual* limit so operators do not
      see a stale "12" after enabling 5b.
"""

from __future__ import annotations

from strategy_factory.application.quality_gates import pre_gate_screen


def _build_candidate(*, task_source: str, target_count: int, contract_cap: int = 0,
                     explicit_limit: int = 0, validation_focus: str | None = None,
                     strategy_type: str = "momentum") -> dict:
    target_codes = [f"60{i:04d}" for i in range(target_count)]
    research_task: dict = {
        "task_source": task_source,
        "target_symbols": target_codes,
    }
    if validation_focus:
        research_task["validation_focus"] = validation_focus
    if contract_cap > 0:
        research_task["target_alignment_contract"] = {
            "max_candidate_target_symbols": contract_cap,
        }
    if explicit_limit > 0:
        research_task["target_symbol_limit"] = explicit_limit

    return {
        "strategy_type": strategy_type,
        "target_symbols": target_codes,
        "research_task": research_task,
    }


def _evaluate_reasons(candidate: dict) -> list[str]:
    """Drive the gate evaluation and return the reason list.

    PR-E note: ``resolve_target_symbol_limit`` reads env vars on every
    call (not cached at module load), so ``monkeypatch.setenv`` flips
    take effect without any importlib.reload dance.
    """

    result = pre_gate_screen(candidate, family_counts={}, stock_counts={})
    return list(getattr(result, "reasons", []) or [])


def _has_limit_reason(reasons: list[str], expected_limit: int) -> bool:
    """True if ``target_symbol_count_exceeds_limit:<N>`` matches ``expected_limit``."""
    needle = f"target_symbol_count_exceeds_limit:{expected_limit}"
    return needle in reasons


# ---------------------------------------------------------------------------
# 5a default: DYNAMIC_TARGET_COUNT_ENABLED off
# ---------------------------------------------------------------------------


def test_5a_snapshot_capped_at_12(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", raising=False)
    candidate = _build_candidate(task_source="snapshot", target_count=15)
    reasons = _evaluate_reasons(candidate)
    assert _has_limit_reason(reasons, 12), reasons


def test_5a_event_driven_also_capped_at_12(monkeypatch):
    """5a 灰度防护核心：event_driven 不能比 snapshot 宽松，避免
    "上游 30 / Gate 12" 的灰度矛盾."""
    monkeypatch.delenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", raising=False)
    candidate = _build_candidate(task_source="event_driven", target_count=15)
    reasons = _evaluate_reasons(candidate)
    assert _has_limit_reason(reasons, 12), reasons


def test_5a_event_driven_at_12_passes_limit_check(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", raising=False)
    candidate = _build_candidate(task_source="event_driven", target_count=12)
    reasons = _evaluate_reasons(candidate)
    # No exceeds_limit reason (other reasons may exist — we only assert this one is absent).
    assert not any(r.startswith("target_symbol_count_exceeds_limit:") for r in reasons), reasons


# ---------------------------------------------------------------------------
# 5b enabled: DYNAMIC_TARGET_COUNT_ENABLED on
# ---------------------------------------------------------------------------


def test_5b_event_driven_allowed_up_to_30(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_TARGET_COUNT_MAX", "30")
    candidate = _build_candidate(task_source="event_driven", target_count=20)
    reasons = _evaluate_reasons(candidate)
    assert not any(r.startswith("target_symbol_count_exceeds_limit:") for r in reasons), reasons


def test_5b_event_driven_blocked_above_30(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_TARGET_COUNT_MAX", "30")
    candidate = _build_candidate(task_source="event_driven", target_count=35)
    reasons = _evaluate_reasons(candidate)
    assert _has_limit_reason(reasons, 30), reasons


def test_5b_snapshot_still_capped_at_12(monkeypatch):
    """Even with 5b on, snapshot must keep the 12-cap; only event_driven scales."""
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    candidate = _build_candidate(task_source="snapshot", target_count=15)
    reasons = _evaluate_reasons(candidate)
    assert _has_limit_reason(reasons, 12), reasons


# ---------------------------------------------------------------------------
# Resolution priority: contract > target_symbol_limit > resolver
# ---------------------------------------------------------------------------


def test_target_alignment_contract_overrides_resolver(monkeypatch):
    """优先级 #1: target_alignment_contract.max_candidate_target_symbols."""
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    candidate = _build_candidate(
        task_source="event_driven",
        target_count=15,
        contract_cap=10,  # tighter than the dynamic 30
    )
    reasons = _evaluate_reasons(candidate)
    # contract_cap=10 should be the limit shown in the reject reason
    assert _has_limit_reason(reasons, 10), reasons


def test_explicit_limit_used_when_no_contract(monkeypatch):
    """优先级 #2: research_task.target_symbol_limit when contract absent."""
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    candidate = _build_candidate(
        task_source="event_driven",
        target_count=15,
        explicit_limit=8,
    )
    reasons = _evaluate_reasons(candidate)
    assert _has_limit_reason(reasons, 8), reasons


def test_contract_wins_over_explicit_limit(monkeypatch):
    """同时给 contract_cap 与 target_symbol_limit 时，contract_cap 优先."""
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    candidate = _build_candidate(
        task_source="event_driven",
        target_count=15,
        contract_cap=6,
        explicit_limit=20,
    )
    reasons = _evaluate_reasons(candidate)
    assert _has_limit_reason(reasons, 6), reasons


# ---------------------------------------------------------------------------
# Reason format: must carry the actual limit, not a stale "12"
# ---------------------------------------------------------------------------


def test_reason_format_includes_actual_limit(monkeypatch):
    """方案 §6 Phase 3 验收: Gate 拒绝原因里展示实际 limit，而不是固定 12."""
    monkeypatch.setenv("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", "1")
    candidate = _build_candidate(
        task_source="event_driven",
        target_count=22,
        contract_cap=18,
    )
    reasons = _evaluate_reasons(candidate)
    # Must NOT carry the legacy reason string.
    assert "target_symbol_count_exceeds_12" not in reasons, reasons
    # Must carry the dynamic reason with the contract cap.
    assert "target_symbol_count_exceeds_limit:18" in reasons, reasons
