"""Single authority for Strategy Factory submission admission decisions."""

from __future__ import annotations

from typing import Any, Callable, Optional


ADMISSION_DECISION_CONTRACT_VERSION = "strategy_factory.admission_decision.v1"


def _canonical_formal_blocker(reason: Any) -> str:
    token = str(reason or "").strip()
    if not token:
        return ""
    normalized = token.lower()
    if normalized in {
        "diagnostic_only",
        "diagnostic_only_runtime",
        "diagnostic_only_observe",
    } or normalized.startswith("diagnostic_only_not_allowed_for_"):
        return "diagnostic_only_not_allowed_for_incubation"
    if normalized == "missing_executable_contract":
        return "execution_readiness_tier:missing_executable_contract"
    if normalized == "execution_readiness_tier:missing":
        return "execution_readiness_tier:missing_executable_contract"
    return token


def _canonical_formal_blockers(reasons: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            item
            for item in (_canonical_formal_blocker(reason) for reason in reasons)
            if item
        )
    )


class SubmissionAdmissionAuthority:
    """Resolve the submission lane, final status, and public admission decision."""

    @classmethod
    def resolve(
        cls,
        gate: dict,
        *,
        candidate: Optional[dict[str, Any]] = None,
        refresh_existing: bool,
        existing_status: str,
        incubation_budget_track: str,
        runtime_bootstrap_resolver: Callable[..., dict[str, Any]],
        read_only: bool = False,
    ) -> dict[str, Any]:
        normalized_gate = dict(gate or {})
        runtime_bootstrap = runtime_bootstrap_resolver(
            normalized_gate,
            candidate=candidate,
        )
        gate_a_decision = str(normalized_gate.get("gate_a_decision") or "").strip().lower()
        readiness_fields = (
            "research_candidate_ready",
            "incubation_candidate_ready",
            "live_candidate_ready",
            "research_only_due_to_trade_audit_gap",
        )
        if bool(normalized_gate.get("passed")) and not any(field in normalized_gate for field in readiness_fields):
            normalized_gate["research_candidate_ready"] = True
            normalized_gate["incubation_candidate_ready"] = True
        admission_block_reasons = list(normalized_gate.get("admission_block_reasons") or normalized_gate.get("reasons") or [])
        strict_formal_ready = bool(normalized_gate.get("strict_incubation_ready")) or (
            str(normalized_gate.get("incubation_pass_mode") or "").strip().lower() == "strict"
        )
        candidate_payload = dict(candidate or {})
        candidate_params = dict(candidate_payload.get("params") or {})
        candidate_budget = dict(candidate_payload.get("incubation_budget") or {})
        observe_first_intake_requested = bool(
            candidate_payload.get("observe_first_intake")
            or candidate_params.get("observe_first_intake")
            or candidate_budget.get("observe_first_intake")
        )
        explicit_formal_track_requested = str(incubation_budget_track or "").strip().lower() == "formal_incubation"
        candidate_budget_track = str(candidate_budget.get("track") or "").strip().lower()
        requested_track = str(incubation_budget_track or "").strip().lower()
        deferred_budget_requested = requested_track in {
            "deferred_budget_queue",
            "deferred_submission",
        }
        formal_auto_correction_source_track = candidate_budget_track or requested_track
        formal_auto_correction_allowed = bool(
            observe_first_intake_requested
            or formal_auto_correction_source_track
            in {
                "observe_incubation",
                "deferred_budget_queue",
                "deferred_submission",
            }
        )
        explicit_formal_budget_requested = bool(
            explicit_formal_track_requested
            and candidate_budget_track == "formal_incubation"
        )
        readiness_tier = (
            str(runtime_bootstrap.get("execution_readiness_tier") or "").strip().lower() or "unknown"
        )
        formal_runtime_ready = (
            bool(runtime_bootstrap.get("runtime_bootstrap_eligible"))
            and not bool(runtime_bootstrap.get("execution_semantic_gap"))
            and bool(runtime_bootstrap.get("semantic_runtime_match"))
            and not bool(runtime_bootstrap.get("proxy_runtime_used"))
            and not bool(runtime_bootstrap.get("diagnostic_only"))
            and readiness_tier == "formal_runtime_ready"
            and str(runtime_bootstrap.get("trade_prediction_contract_status") or "").strip().lower() == "ready"
            and not bool(runtime_bootstrap.get("trade_prediction_contract_observation_gap"))
        )
        formal_track_auto_corrected = bool(
            not explicit_formal_track_requested
            and formal_auto_correction_allowed
            and bool(normalized_gate.get("passed"))
            and strict_formal_ready
            and not bool(normalized_gate.get("live_candidate_ready"))
            and formal_runtime_ready
        )
        formal_track_requested = bool(explicit_formal_track_requested or formal_track_auto_corrected)
        formal_track_blockers: list[str] = []
        if formal_track_requested:
            if not bool(normalized_gate.get("passed")):
                formal_track_blockers.append("quality_gate_pass_required_for_formal_track")
            if not bool(runtime_bootstrap.get("runtime_bootstrap_eligible")):
                formal_track_blockers.append(
                    str(runtime_bootstrap.get("runtime_bootstrap_reason") or "runtime_bootstrap_not_eligible")
                )
            if bool(runtime_bootstrap.get("execution_semantic_gap")):
                formal_track_blockers.extend(
                    list(runtime_bootstrap.get("execution_semantic_gap_reasons") or [])
                    or ["execution_semantic_gap"]
                )
            if not bool(runtime_bootstrap.get("semantic_runtime_match")):
                formal_track_blockers.append("semantic_runtime_mismatch")
            if bool(runtime_bootstrap.get("proxy_runtime_used")):
                formal_track_blockers.append("proxy_runtime_not_allowed_for_formal_incubation")
            if bool(runtime_bootstrap.get("diagnostic_only")):
                formal_track_blockers.append("diagnostic_only_not_allowed_for_incubation")
            if readiness_tier != "formal_runtime_ready":
                formal_track_blockers.append(f"execution_readiness_tier:{readiness_tier}")
            if not strict_formal_ready:
                formal_track_blockers.append("strict_incubation_pass_required_for_formal_track")
        formal_track_blockers = _canonical_formal_blockers(formal_track_blockers)
        formal_track_eligible = bool(formal_track_requested and not formal_track_blockers)
        if refresh_existing:
            action_type = "refresh_existing"
            submission_lane = "refresh_existing"
            final_status = str(existing_status or "draft")
            trigger = "existing_strategy_refresh"
            gaps = []
            fallback_conditions = ["manual_review_if_contract_changes"]
            next_step = "existing_status_preserved"
            completed = True
        elif gate_a_decision == "revise" and not bool(normalized_gate.get("passed")):
            action_type = "research_only"
            submission_lane = "deferred_submission"
            final_status = "draft"
            trigger = "gate_a_revision_required"
            gaps = admission_block_reasons
            fallback_conditions = ["supply_required_research_protocol_fields_and_replay_submission"]
            next_step = "research"
            completed = True
        elif gate_a_decision == "reject" and not bool(normalized_gate.get("passed")):
            action_type = "research_only"
            submission_lane = "rejected"
            final_status = "rejected"
            trigger = "gate_a_reject"
            gaps = admission_block_reasons
            fallback_conditions = ["restore_required_research_protocol_fields_before_submission_replay"]
            next_step = "research"
            completed = True
        elif deferred_budget_requested and not formal_track_auto_corrected:
            action_type = "research_only"
            submission_lane = "deferred_submission"
            final_status = "submitted" if bool(normalized_gate.get("research_candidate_ready") or normalized_gate.get("passed")) else "rejected"
            trigger = "budget_deferred_research_only"
            gaps = list(dict.fromkeys([*admission_block_reasons, trigger]))
            fallback_conditions = [
                "re_enter_budget_selection_after_feedback_controls_clear",
                "promote_to_formal_if_strict_runtime_ready",
            ]
            next_step = "incubation"
            completed = True
        elif (
            not bool(normalized_gate.get("passed"))
            and bool(runtime_bootstrap.get("wide_intake_admitted"))
            and not explicit_formal_budget_requested
        ):
            # === INVERT-DESIGN P1 改动A: Layer 1 宽进准入 ===
            # Gate-3 未通过,但结构合法且 wide_intake toggle 开启 → 进零资本 observe 观察,
            # 而非直接 rejected。让 ForwardVerifier 用向前真实数据测量它,而不是回测前置拍死。
            action_type = "paper"
            submission_lane = "observe_incubation"
            final_status = "submitted"
            trigger = str(
                runtime_bootstrap.get("runtime_bootstrap_reason")
                or "wide_intake_observe_gate3_not_required"
            )
            gaps = list(dict.fromkeys([*admission_block_reasons, "wide_intake_observe"]))
            fallback_conditions = [
                "retire_if_forward_skill_lcb_negative_after_min_observation_window",
                "promote_to_formal_if_forward_skill_significant_across_regimes",
            ]
            next_step = "runtime_review"
            completed = False
        elif not bool(normalized_gate.get("passed")):
            action_type = "research_only"
            submission_lane = "rejected"
            final_status = "rejected"
            trigger = "quality_gate_failed"
            gaps = admission_block_reasons
            fallback_conditions = ["re_enter_after_quality_gaps_closed"]
            next_step = "incubation"
            completed = True
        elif bool(normalized_gate.get("live_candidate_ready")) and not bool(runtime_bootstrap.get("execution_semantic_gap")):
            formal_runtime_ready = (
                bool(runtime_bootstrap.get("semantic_runtime_match"))
                and not bool(runtime_bootstrap.get("proxy_runtime_used"))
                and not bool(runtime_bootstrap.get("diagnostic_only"))
                and str(runtime_bootstrap.get("execution_readiness_tier") or "").strip().lower() == "formal_runtime_ready"
            )
            if not formal_runtime_ready:
                action_type = "paper"
                submission_lane = "observe_incubation"
                final_status = "submitted"
                trigger = str(
                    runtime_bootstrap.get("runtime_bootstrap_reason")
                    or "formal_lane_requires_semantic_runtime_match"
                )
                gaps = list(dict.fromkeys([*admission_block_reasons, trigger]))
                fallback_conditions = [
                    "promote_after_runtime_data_source_and_semantic_contract_are_repaired",
                ]
                next_step = "runtime_review"
                completed = False
            else:
                action_type = "runtime_review"
                submission_lane = "live_ready_review"
                final_status = "submitted"
                trigger = "live_candidate_ready"
                gaps = []
                fallback_conditions = [
                    "downgrade_to_paper_if_runtime_review_fails",
                    "return_to_research_if_runtime_alerts_fire",
                ]
                next_step = "pool_admission"
                completed = False
        elif formal_track_eligible:
            action_type = "incubation"
            submission_lane = "formal_incubation"
            final_status = "incubating"
            trigger = (
                "strict_incubation_ready_and_observe_first_formal_correction"
                if formal_track_auto_corrected and observe_first_intake_requested
                else "strict_incubation_ready_and_runtime_formal_correction"
                if formal_track_auto_corrected
                else "strict_incubation_ready_and_budget_formal"
            )
            gaps = admission_block_reasons
            fallback_conditions = [
                "downgrade_to_observe_if_signal_quality_or_execution_evidence_weakens",
                "return_to_research_if_runtime_risk_accumulates",
            ]
            next_step = "paper"
            completed = False
        elif (
            bool(runtime_bootstrap.get("runtime_bootstrap_eligible"))
            and not explicit_formal_budget_requested
            and (
                not formal_track_requested
                or bool(runtime_bootstrap.get("wide_intake_admitted"))
                or str(runtime_bootstrap.get("runtime_bootstrap_reason") or "").strip().lower()
                == "d_grade_observe_only_micro_budget"
            )
        ):
            action_type = "paper"
            submission_lane = "observe_incubation"
            final_status = "submitted"
            trigger = "runtime_bootstrap_observe"
            gaps = admission_block_reasons
            fallback_conditions = [
                "promote_to_formal_after_signal_quality_and_execution_conversion_improve",
                "return_to_research_if_runtime_bootstrap_evidence_turns_negative",
            ]
            next_step = "runtime_review"
            completed = False
        elif formal_track_requested:
            action_type = "research_only"
            submission_lane = "deferred_submission"
            final_status = "submitted" if bool(normalized_gate.get("research_candidate_ready")) else "rejected"
            trigger = "formal_incubation_requires_bootstrap_or_strict_gate"
            gaps = _canonical_formal_blockers([*list(admission_block_reasons), *formal_track_blockers])
            fallback_conditions = [
                "promote_after_runtime_contract_is_repaired_or_quality_improves",
                "observe_track_allowed_after_runtime_bootstrap_eligibility_is_restored",
            ]
            next_step = "research"
            completed = True
        elif str(incubation_budget_track or "").strip().lower() == "observe_incubation":
            action_type = "research_only"
            submission_lane = "deferred_submission"
            final_status = "submitted" if bool(normalized_gate.get("research_candidate_ready")) else "rejected"
            trigger = str(runtime_bootstrap.get("runtime_bootstrap_reason") or "observe_track_runtime_contract_incomplete")
            gaps = list(dict.fromkeys([*admission_block_reasons, trigger]))
            fallback_conditions = ["repair_runtime_contract_and_replay_submission"]
            next_step = "research"
            completed = True
        else:
            action_type = "research_only"
            submission_lane = "deferred_submission"
            final_status = "submitted"
            trigger = str(runtime_bootstrap.get("runtime_bootstrap_reason") or "budget_deferred_research_only")
            gaps = list(dict.fromkeys([*admission_block_reasons, trigger]))
            fallback_conditions = ["promote_to_observe_after_runtime_bootstrap_eligibility_is_restored"]
            next_step = "incubation"
            completed = True

        planned_submission_lane = (
            submission_lane
            if refresh_existing
            else "formal_incubation"
            if formal_track_requested
            else "observe_incubation"
            if requested_track == "observe_incubation"
            else submission_lane
        )
        planned_final_status = (
            "incubating"
            if planned_submission_lane == "formal_incubation"
            else "submitted"
            if planned_submission_lane == "observe_incubation"
            else final_status
        )
        admission_decision = cls._decision(
            gate=normalized_gate,
            action_type=action_type,
            submission_lane=submission_lane,
            final_status=final_status,
            read_only=read_only,
        )
        plan = {
            "type": action_type,
            "trigger_reason": trigger,
            "gaps": list(gaps),
            "fallback_conditions": list(fallback_conditions),
            "next_step": next_step,
            "submission_lane": submission_lane,
            "final_status": final_status,
            "planned_submission_lane": planned_submission_lane,
            "planned_final_status": planned_final_status,
            "completed": bool(completed),
            "incubation_budget_track": requested_track,
            "formal_track_requested": formal_track_requested,
            "formal_track_auto_corrected": formal_track_auto_corrected,
            "formal_track_eligible": formal_track_eligible,
            "formal_track_blockers": list(formal_track_blockers),
            "observe_first_intake_requested": observe_first_intake_requested,
            "formal_auto_correction_source_track": formal_auto_correction_source_track,
            "admission_decision_contract_version": ADMISSION_DECISION_CONTRACT_VERSION,
            "admission_decision": admission_decision,
            **runtime_bootstrap,
        }
        return {
            "submission_action": plan,
            "submission_action_type": action_type,
            "submission_action_trigger": trigger,
            "submission_action_gaps": list(gaps),
            "submission_action_fallback_conditions": list(fallback_conditions),
            "submission_action_next_step": next_step,
            "submission_action_completed": bool(completed),
            "submission_lane": submission_lane,
            "final_status": final_status,
            "planned_submission_lane": planned_submission_lane,
            "planned_final_status": planned_final_status,
            "incubation_budget_track": requested_track,
            "formal_track_requested": formal_track_requested,
            "formal_track_auto_corrected": formal_track_auto_corrected,
            "formal_track_eligible": formal_track_eligible,
            "formal_track_blockers": list(formal_track_blockers),
            "observe_first_intake_requested": observe_first_intake_requested,
            "formal_auto_correction_source_track": formal_auto_correction_source_track,
            "admission_decision_contract_version": ADMISSION_DECISION_CONTRACT_VERSION,
            "admission_decision": admission_decision,
            **runtime_bootstrap,
        }

    @staticmethod
    def _decision(
        *,
        gate: dict[str, Any],
        action_type: str,
        submission_lane: str,
        final_status: str,
        read_only: bool,
    ) -> str:
        if read_only or action_type == "read_only" or submission_lane == "read_only":
            return "read_only"
        if str(gate.get("gate_a_decision") or "").strip().lower() == "revise" and not bool(gate.get("passed")):
            return "revise"
        if submission_lane == "diagnostic_observation" or action_type == "diagnostic":
            return "diagnostic"
        if bool(gate.get("provisional_pass")):
            return "provisional"
        # INVERT-DESIGN P1 改动A: observe_incubation lane 即使 gate 未 passed(宽进)也属观察,
        # 不应被下面的 not passed → reject 误判。
        if submission_lane == "observe_incubation":
            return "observe_only"
        if final_status == "rejected" or submission_lane == "rejected" or not bool(gate.get("passed")):
            return "reject"
        if submission_lane in {"formal_incubation", "live_ready_review", "refresh_existing"}:
            return "accept"
        return "observe_only"


__all__ = [
    "ADMISSION_DECISION_CONTRACT_VERSION",
    "SubmissionAdmissionAuthority",
]
