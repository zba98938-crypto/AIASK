"""LLM factor generation handler for quant_manager."""

from __future__ import annotations

from collections import Counter
import json
import os
import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import pandas as pd

from ...services.llm_alpha import LLMAlphaMiner
from .quant_mgr_helpers import _as_code_list, _safe_float


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _truncate_text(value: Any, limit: int = 220) -> str:
    text = _normalize_text(value)
    if len(text) <= max(1, int(limit)):
        return text
    return text[: max(1, int(limit)) - 3].rstrip() + "..."


def _extract_memory_similarity(candidate: dict[str, Any]) -> dict[str, Any]:
    trace = candidate.get("generation_trace") if isinstance(candidate.get("generation_trace"), dict) else {}
    similarity = trace.get("memory_similarity") if isinstance(trace.get("memory_similarity"), dict) else {}
    return {
        "top_artifact_id": similarity.get("top_artifact_id"),
        "top_status": similarity.get("top_status"),
        "similarity": _safe_float(similarity.get("similarity"), 0.0),
        "lexical_similarity": _safe_float(similarity.get("lexical_similarity"), 0.0),
        "embedding_similarity": _safe_float(similarity.get("embedding_similarity"), 0.0),
        "code_overlap": _safe_float(similarity.get("code_overlap"), 0.0),
        "edge_type": str(similarity.get("edge_type") or "").strip().lower(),
        "reason": _normalize_text(similarity.get("reason")),
    }


def _derive_research_theme(
    candidates: list[dict[str, Any]] | None,
    *,
    codes: list[str],
) -> str:
    family_counts = Counter(
        str((candidate or {}).get("family") or "").strip().lower()
        for candidate in list(candidates or [])
        if isinstance(candidate, dict) and str((candidate or {}).get("family") or "").strip()
    )
    top_family = family_counts.most_common(1)[0][0] if family_counts else "mixed"
    scoped_codes = [str(item).strip() for item in list(codes or []) if str(item).strip()]
    if not scoped_codes:
        return f"{top_family} factor research"
    head = ",".join(scoped_codes[:3])
    suffix = f"+{len(scoped_codes) - 3}" if len(scoped_codes) > 3 else ""
    return f"{top_family} factor research for {head}{suffix}"


def _build_hypothesis_summary(candidates: list[dict[str, Any]] | None) -> str:
    summary_parts: list[str] = []
    for candidate in list(candidates or [])[:3]:
        if not isinstance(candidate, dict):
            continue
        name = _normalize_text(candidate.get("name")) or "candidate"
        family = _normalize_text(candidate.get("family")) or "unknown"
        hypothesis = _truncate_text(candidate.get("hypothesis"), 120)
        if hypothesis:
            summary_parts.append(f"{name}({family}): {hypothesis}")
        else:
            summary_parts.append(f"{name}({family})")
    return "; ".join(summary_parts)


def _summarize_prompt_context(prompt: Any, memory_context: dict[str, Any]) -> dict[str, Any]:
    context_summary = prompt.context_summary if isinstance(getattr(prompt, "context_summary", None), dict) else {}
    request_payload = prompt.request_payload if isinstance(getattr(prompt, "request_payload", None), dict) else {}
    rows = [dict(item) for item in list(context_summary.get("rows") or []) if isinstance(item, dict)]
    market_dates = []
    total_headlines = 0
    kline_bars = []
    alternative_context_rows = 0
    row_codes = []
    for row in rows:
        row_code = str(row.get("code") or "").strip()
        if row_code:
            row_codes.append(row_code)
        kline_summary = row.get("kline_summary") if isinstance(row.get("kline_summary"), dict) else {}
        recent_headlines = row.get("recent_headlines") if isinstance(row.get("recent_headlines"), dict) else {}
        alternative_factors = row.get("alternative_factors") if isinstance(row.get("alternative_factors"), dict) else {}
        latest_date = str(kline_summary.get("latest_date") or "").strip()
        if latest_date:
            market_dates.append(latest_date)
        bars = int(kline_summary.get("bars", 0) or 0)
        if bars > 0:
            kline_bars.append(bars)
        total_headlines += sum(len(list(value or [])) for value in recent_headlines.values() if isinstance(value, list))
        if alternative_factors:
            alternative_context_rows += 1

    memory = memory_context if isinstance(memory_context, dict) else {}
    return {
        "row_count": len(rows),
        "row_codes": row_codes[:8],
        "field_hint_count": len(list(request_payload.get("field_hints") or [])),
        "avg_kline_bars": round(float(np.mean(kline_bars)), 2) if kline_bars else 0.0,
        "latest_market_dates": list(dict.fromkeys(market_dates))[:5],
        "headline_count": total_headlines,
        "alternative_context_row_count": alternative_context_rows,
        "memory_available": bool(memory.get("available")),
        "memory_success_examples": len(list(memory.get("success_examples") or [])),
        "memory_failure_examples": len(list(memory.get("failure_examples") or [])),
        "memory_duplicate_examples": len(list(memory.get("duplicate_examples") or [])),
    }


def _summarize_analysis(analysis: dict[str, Any] | None) -> dict[str, Any]:
    record = dict(analysis or {})
    return {
        "available": bool(record),
        "keys": sorted(str(key) for key in record.keys())[:12],
        "market_regime": record.get("market_regime"),
        "dominant_style": record.get("dominant_style"),
        "reasoning_focus": record.get("reasoning_focus"),
        "risk_bias": record.get("risk_bias"),
    }


def _summarize_startup_warmup(startup_warmup: dict[str, Any] | None) -> dict[str, Any]:
    record = dict(startup_warmup or {})
    return {
        "status": str(record.get("status") or "disabled"),
        "task_type": str(record.get("task_type") or ""),
        "force": bool(record.get("force")),
        "matched": int(record.get("matched", 0) or 0),
        "executed": int(record.get("executed", 0) or 0),
        "failed": int(record.get("failed", 0) or 0),
    }


def _summarize_blocked_candidates(blocked_candidates: list[dict[str, Any]] | None) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    sample_names: list[str] = []
    for item in list(blocked_candidates or []):
        if not isinstance(item, dict):
            continue
        reason = (
            _normalize_text(item.get("blocked_reason"))
            or _normalize_text(item.get("reason"))
            or "unknown"
        )
        reason_counts[reason] += 1
        name = _normalize_text(item.get("name"))
        if name:
            sample_names.append(name)
    return {
        "count": len([item for item in list(blocked_candidates or []) if isinstance(item, dict)]),
        "reason_counts": dict(reason_counts),
        "sample_names": sample_names[:6],
    }


def _summarize_memory_similarity(candidates: list[dict[str, Any]] | None) -> dict[str, Any]:
    similarities: list[float] = []
    artifact_ids: list[str] = []
    edge_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    duplicate_recommended_count = 0

    for candidate in list(candidates or []):
        if not isinstance(candidate, dict):
            continue
        memory_similarity = _extract_memory_similarity(candidate)
        similarity = _safe_float(memory_similarity.get("similarity"), 0.0)
        if similarity > 0:
            similarities.append(similarity)
        top_artifact_id = str(memory_similarity.get("top_artifact_id") or "").strip()
        if top_artifact_id:
            artifact_ids.append(top_artifact_id)
        edge_type = str(memory_similarity.get("edge_type") or "").strip().lower()
        if edge_type:
            edge_counts[edge_type] += 1
        top_status = str(memory_similarity.get("top_status") or "").strip().lower()
        if top_status:
            status_counts[top_status] += 1
        if bool(candidate.get("duplicate_block_recommended")):
            duplicate_recommended_count += 1

    return {
        "available": bool(candidates),
        "matched_candidate_count": len(similarities),
        "avg_similarity": round(float(np.mean(similarities)), 6) if similarities else 0.0,
        "max_similarity": round(float(max(similarities)), 6) if similarities else 0.0,
        "top_artifact_ids": list(dict.fromkeys(artifact_ids))[:8],
        "edge_type_counts": dict(edge_counts),
        "status_counts": dict(status_counts),
        "duplicate_block_recommended_count": duplicate_recommended_count,
    }


def _summarize_novelty(candidates: list[dict[str, Any]] | None) -> dict[str, Any]:
    novelty_scores: list[float] = []
    duplicate_risk_counts: Counter[str] = Counter()
    high_novelty_count = 0
    medium_novelty_count = 0

    for candidate in list(candidates or []):
        if not isinstance(candidate, dict):
            continue
        memory_similarity = _extract_memory_similarity(candidate)
        fallback_novelty_score = max(0.0, min(1.0, 1.0 - _safe_float(memory_similarity.get("similarity"), 0.0)))
        novelty_score = _safe_float(candidate.get("novelty_score"), fallback_novelty_score)
        novelty_scores.append(novelty_score)
        if novelty_score >= 0.8:
            high_novelty_count += 1
        elif novelty_score >= 0.5:
            medium_novelty_count += 1
        duplicate_risk = str(candidate.get("duplicate_risk") or "unknown").strip().lower()
        duplicate_risk_counts[duplicate_risk] += 1

    return {
        "avg_novelty_score": round(float(np.mean(novelty_scores)), 6) if novelty_scores else 0.0,
        "max_novelty_score": round(float(max(novelty_scores)), 6) if novelty_scores else 0.0,
        "high_novelty_count": high_novelty_count,
        "medium_novelty_count": medium_novelty_count,
        "duplicate_risk_counts": dict(duplicate_risk_counts),
    }


def _enrich_generation_candidates(
    candidates: list[dict[str, Any]] | None,
    *,
    artifact_id: str,
    research_theme: str,
    generation_mode: str,
    provider_name: str,
    model_name: str,
    codes: list[str],
) -> list[dict[str, Any]]:
    enriched_candidates: list[dict[str, Any]] = []
    for index, raw_candidate in enumerate(list(candidates or [])):
        if not isinstance(raw_candidate, dict):
            continue
        candidate = dict(raw_candidate)
        trace = dict(candidate.get("generation_trace") or {})
        memory_similarity = _extract_memory_similarity(candidate)
        novelty_score = _safe_float(
            candidate.get("novelty_score"),
            max(0.0, min(1.0, 1.0 - _safe_float(memory_similarity.get("similarity"), 0.0))),
        )
        trace.update(
            {
                "episode_id": artifact_id,
                "episode_theme": research_theme,
                "generation_mode": generation_mode,
                "provider": trace.get("provider") or provider_name,
                "model": trace.get("model") or model_name,
                "candidate_index": index,
                "target_codes": list(codes or [])[:8],
                "novelty": {
                    "novelty_score": round(novelty_score, 6),
                    "duplicate_risk": str(candidate.get("duplicate_risk") or "unknown").strip().lower(),
                    "top_memory_similarity": round(_safe_float(memory_similarity.get("similarity"), 0.0), 6),
                    "top_memory_artifact_id": memory_similarity.get("top_artifact_id"),
                },
            }
        )
        candidate["generation_trace"] = trace
        candidate["research_episode_id"] = artifact_id
        candidate["research_theme"] = research_theme
        candidate["novelty_score"] = round(novelty_score, 6)
        enriched_candidates.append(candidate)
    return enriched_candidates


async def _load_market_frame(
    *,
    one_code: str,
    bars: int,
    db: Any,
    missing_kline_fn: Callable[..., Any],
    sort_klines_ascending_fn: Callable[[list[dict]], list[dict]],
):
    fallback_reason = None
    source = "db.get_klines"
    klines = []
    try:
        klines = await db.get_klines(one_code, limit=max(120, int(bars)))
    except Exception as exc:
        fallback_reason = f"db.get_klines failed: {exc}"
        klines = []
    if not klines:
        missing_kline_fn(one_code, period="daily", limit=max(120, int(bars)))
    if not klines:
        return None, source, fallback_reason or "no kline data"
    ordered = sort_klines_ascending_fn(klines)
    frame = pd.DataFrame(ordered)
    return frame, source, fallback_reason


def _screen_compiler_candidates(
    candidates: list[dict] | None,
    *,
    compile_factor_candidate_fn: Callable[[dict], dict],
) -> dict:
    kept_candidates = []
    rejected_candidates = []
    for idx, raw_candidate in enumerate(list(candidates or [])):
        candidate = dict(raw_candidate or {})
        candidate_name = str(candidate.get("name") or f"candidate_{idx}").strip() or f"candidate_{idx}"
        try:
            compiled = compile_factor_candidate_fn(candidate)
        except Exception as exc:
            rejected_candidates.append(
                {
                    "name": candidate_name,
                    "reason": f"compile_exception:{exc}",
                    "unsupported_fields": [],
                    "unsupported_functions": [],
                }
            )
            continue

        if not compiled.get("valid"):
            rejected_candidates.append(
                {
                    "name": candidate_name,
                    "reason": "compiler_invalid",
                    "unsupported_fields": list(compiled.get("unsupported_fields") or []),
                    "unsupported_functions": list(compiled.get("unsupported_functions") or []),
                    "complexity_score": _safe_float(((compiled.get("complexity") or {}).get("score")), 0.0),
                }
            )
            continue

        kept_candidates.append(candidate)

    warnings = []
    if rejected_candidates:
        sample = []
        for item in rejected_candidates[:5]:
            parts = [str(item.get("name") or "candidate")]
            if item.get("unsupported_fields"):
                parts.append(f"fields={item.get('unsupported_fields')}")
            if item.get("unsupported_functions"):
                parts.append(f"functions={item.get('unsupported_functions')}")
            if item.get("reason") and not item.get("unsupported_fields") and not item.get("unsupported_functions"):
                parts.append(str(item.get("reason")))
            sample.append("|".join(parts))
        warnings.append(
            "compiler_screen_rejected="
            f"{len(rejected_candidates)}/{len(list(candidates or []))}: " + "; ".join(sample)
        )

    return {
        "kept_candidates": kept_candidates,
        "rejected_candidates": rejected_candidates,
        "summary": {
            "input_count": len(list(candidates or [])),
            "kept_count": len(kept_candidates),
            "rejected_count": len(rejected_candidates),
        },
        "warnings": warnings,
    }


async def _build_local_fallback_candidates(
    *,
    target_codes: list[str],
    candidate_count: int,
    db: Any,
    compile_factor_candidate_fn: Callable[[dict], dict],
    missing_kline_fn: Callable[..., Any],
    sort_klines_ascending_fn: Callable[[list[dict]], list[dict]],
    llm_alpha_miner_factory: Callable[[], LLMAlphaMiner],
):
    miner = llm_alpha_miner_factory()
    fallback_rows = []
    fallback_source_chain: list[str] = []
    fallback_reasons: list[str] = []
    scoped_codes = list(target_codes or [])[:5]
    per_code = max(1, int(np.ceil(max(1, int(candidate_count)) / max(1, len(scoped_codes)))))
    fallback_dsl_templates = {
        "momentum": ("zscore(momentum_20d, 20) + zscore(momentum_60d, 20)", ["momentum_20d", "momentum_60d"]),
        "trend": ("ts_mean(return_5d, 10) + zscore(momentum_20d, 20)", ["return_5d", "momentum_20d"]),
        "reversal": ("-return_5d", ["return_5d"]),
        "volatility": ("-zscore(volatility_20d, 20)", ["volatility_20d"]),
        "liquidity": ("zscore(volume_ratio_5_20, 10)", ["volume_ratio_5_20"]),
        "risk_adjusted": ("zscore(momentum_20d, 20) - zscore(volatility_20d, 20)", ["momentum_20d", "volatility_20d"]),
        "divergence": ("zscore(momentum_20d, 10) - zscore(momentum_60d, 20)", ["momentum_20d", "momentum_60d"]),
        "custom": (
            "zscore(momentum_20d, 20) + zscore(volume_ratio_5_20, 10) - zscore(volatility_20d, 20)",
            ["momentum_20d", "volume_ratio_5_20", "volatility_20d"],
        ),
    }

    for one_code in scoped_codes:
        frame, one_source, reason = await _load_market_frame(
            one_code=one_code,
            bars=180,
            db=db,
            missing_kline_fn=missing_kline_fn,
            sort_klines_ascending_fn=sort_klines_ascending_fn,
        )
        if reason:
            fallback_reasons.append(f"{one_code}: {reason}")
        if frame is None or frame.empty:
            continue
        fallback_source_chain.append(one_source)
        try:
            raw_candidates = miner.generate_factor_candidates(
                frame,
                news_data=None,
                num_candidates=min(8, per_code),
            )
        except Exception as exc:
            fallback_reasons.append(f"{one_code}: local fallback generation failed: {exc}")
            continue

        for item in raw_candidates:
            raw_formula = str(item.get("formula") or "").strip() or "close"
            category = str(item.get("category") or "custom").strip().lower() or "custom"
            expression_dsl = raw_formula
            inputs = ["close"]
            candidate_probe = {
                "name": str(item.get("name") or f"{one_code}_fallback_factor"),
                "hypothesis": "Local rule fallback candidate",
                "family": category,
                "inputs": ["close"],
                "expression_dsl": expression_dsl,
                "expected_holding_period": 10,
                "expected_regime": [],
                "complexity_hint": "medium",
                "novelty_rationale": "Generated from local fallback seed pool",
            }
            try:
                compiled_probe = compile_factor_candidate_fn(candidate_probe)
            except Exception:
                compiled_probe = {"valid": False}
            if compiled_probe.get("valid"):
                inputs = list(compiled_probe.get("referenced_fields") or ["close"])
            else:
                expression_dsl, inputs = fallback_dsl_templates.get(category, fallback_dsl_templates["custom"])

            fallback_rows.append(
                {
                    "name": str(item.get("name") or f"{one_code}_fallback_factor"),
                    "hypothesis": str(item.get("description") or item.get("rationale") or "Local rule fallback candidate").strip(),
                    "family": category,
                    "inputs": inputs,
                    "expression_dsl": expression_dsl,
                    "expected_holding_period": 10,
                    "expected_regime": [],
                    "complexity_hint": "medium",
                    "novelty_rationale": str(item.get("rationale") or "Generated from local fallback seed pool").strip(),
                    "generation_trace": {
                        "mode": "local_rule_fallback",
                        "engine": str(item.get("engine") or item.get("category") or "local_rule_v1"),
                        "code": one_code,
                    },
                    "source_model": "local_rule_fallback",
                }
            )
            if len(fallback_rows) >= max(1, int(candidate_count)):
                break
        if len(fallback_rows) >= max(1, int(candidate_count)):
            break

    if not fallback_rows:
        raise RuntimeError("local fallback produced no candidates")

    from ...services import validate_factor_generation_payload

    validated = validate_factor_generation_payload(
        {
            "candidates": fallback_rows[: max(1, int(candidate_count))],
            "warnings": ["local_rule_fallback"] + fallback_reasons[:10],
            "analysis": {
                "mode": "local_rule_fallback",
                "codes": scoped_codes,
            },
        },
        model="local_rule_fallback",
        provider="local_rule",
    )
    return validated, fallback_source_chain or ["services.llm_alpha"], fallback_reasons


async def handle_llm_factor_mining(
    *,
    kw: dict[str, Any],
    code: str | None,
    db: Any,
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    get_factor_llm_provider_fn: Callable[[], Any],
    memory_service_factory: Callable[[], Any],
    build_factor_mining_prompt_fn: Callable[..., Any],
    run_runtime_data_warmup_fn: Callable[..., Any],
    register_artifact_async_fn: Callable[..., Any],
    missing_kline_fn: Callable[..., Any],
    compile_factor_candidate_fn: Callable[[dict], dict],
    sort_klines_ascending_fn: Callable[[list[dict]], list[dict]],
    coerce_bool_fn: Callable[[Any, bool], bool],
    env_bool_fn: Callable[[str, bool], bool],
    llm_alpha_miner_factory: Callable[[], LLMAlphaMiner] = LLMAlphaMiner,
) -> dict:
    codes = _as_code_list(kw.get("codes"))
    if not codes and code:
        codes = [code]
    if not codes:
        return fail("需要提供 code 或 codes")

    requested_candidate_count = (
        kw.get("candidate_count")
        if kw.get("candidate_count") not in (None, "", [])
        else kw.get("max_candidates")
    )
    candidate_count = max(1, min(int(requested_candidate_count or 8), 16))
    lookback_bars = max(120, min(int(kw.get("lookback_bars", 180) or 180), 360))
    alternative_lookback_days = max(7, min(int(kw.get("alternative_lookback_days", 30) or 30), 90))
    allow_fallback = bool(kw.get("allow_fallback", True))
    persist_artifact = bool(kw.get("persist_artifact", True))
    dry_run = bool(kw.get("_dry_run") or kw.get("dry_run"))
    workflow_fast_mode = coerce_bool_fn(kw.get("workflow_fast_mode"), False)
    dedup_mode = str(kw.get("dedup_mode", "penalty") or "penalty").strip().lower()
    dedup_high_similarity_threshold = float(kw.get("dedup_high_similarity_threshold", 0.98) or 0.98)
    dedup_failure_similarity_threshold = float(kw.get("dedup_failure_similarity_threshold", 0.93) or 0.93)
    startup_warmup_enabled = coerce_bool_fn(
        kw.get("startup_warmup"),
        env_bool_fn("FACTOR_LLM_STARTUP_WARMUP_ENABLED", True),
    )
    startup_warmup_force = coerce_bool_fn(
        kw.get("startup_warmup_force"),
        env_bool_fn("FACTOR_LLM_STARTUP_WARMUP_FORCE", False),
    )
    startup_warmup_limit = max(
        1,
        min(
            int(
                kw.get("startup_warmup_limit")
                or os.getenv("FACTOR_LLM_STARTUP_WARMUP_LIMIT", "4")
                or 4
            ),
            20,
        ),
    )
    startup_warmup_task_type = (
        str(
            kw.get("startup_warmup_task_type")
            or os.getenv("FACTOR_LLM_STARTUP_WARMUP_TASK_TYPE", "core_market,factor_context")
            or "core_market,factor_context"
        )
        .strip()
        .lower()
        or "core_market,factor_context"
    )

    provider = get_factor_llm_provider_fn()
    memory_service = memory_service_factory()
    prompt = None
    generation = None
    warnings: list[str] = []
    source_chain = []
    memory_context = {}
    blocked_candidates: list[dict] = []
    startup_warmup = {
        "ok": True,
        "status": "disabled",
        "task_type": startup_warmup_task_type,
        "force": startup_warmup_force,
        "matched": 0,
        "executed": 0,
        "failed": 0,
        "executed_task_ids": [],
        "failed_schedule_ids": [],
        "schedules": [],
    }
    dedup_summary = {
        "mode": dedup_mode,
        "input_count": 0,
        "kept_count": 0,
        "blocked_count": 0,
        "blocked_ratio": 0.0,
    }

    if workflow_fast_mode:
        startup_warmup_enabled = False

    if not persist_artifact:
        if dry_run:
            warnings.append(
                "dry_run_artifact_not_persisted: validate by inline candidate or rerun with dry_run=false"
            )
        else:
            warnings.append("artifact_not_persisted")

    if startup_warmup_enabled:
        try:
            startup_warmup = await run_runtime_data_warmup_fn(
                task_type=startup_warmup_task_type,
                force=startup_warmup_force,
                limit=startup_warmup_limit,
                source="llm_factor_mining",
            )
            source_chain.append("tools.managers.data_sync_manager")
            if startup_warmup.get("status") in {"failed", "partial"}:
                warnings.append(
                    f"startup_warmup_{startup_warmup.get('status')}: "
                    f"matched={startup_warmup.get('matched', 0)} executed={startup_warmup.get('executed', 0)} failed={startup_warmup.get('failed', 0)}"
                )
        except Exception as exc:
            startup_warmup = {
                "ok": False,
                "status": "failed",
                "task_type": startup_warmup_task_type,
                "force": startup_warmup_force,
                "matched": 0,
                "executed": 0,
                "failed": 0,
                "executed_task_ids": [],
                "failed_schedule_ids": [],
                "schedules": [],
                "error": str(exc),
            }
            warnings.append(f"startup_warmup_failed: {exc}")

    try:
        memory_context = await memory_service.build_prompt_memory_context(
            codes=codes,
            limit=max(6, candidate_count),
            query_text=f"codes:{','.join(codes[:8])} factor research prompt",
        )
        source_chain.append("services.factor_research_memory")
    except Exception as exc:
        warnings.append(f"memory_context_failed: {exc}")
        memory_context = {}

    try:
        prompt = await build_factor_mining_prompt_fn(
            db=db,
            codes=codes,
            candidate_count=candidate_count,
            lookback_bars=lookback_bars,
            alternative_lookback_days=alternative_lookback_days,
            memory_context=memory_context,
        )
        source_chain.extend(list(prompt.source_chain or []))
        source_chain.append("services.factor_prompt_builder")
    except Exception as exc:
        warnings.append(f"prompt_build_failed: {exc}")
        if not allow_fallback:
            return fail(
                f"构建 LLM 因子研究上下文失败: {exc}",
                source_chain=["services.factor_prompt_builder"],
            )

    fallback_used = False
    fallback_reason = None
    if workflow_fast_mode and allow_fallback:
        fallback_reason = "workflow_fast_mode_prefer_local_fallback"
        warnings.append(fallback_reason)
    elif prompt is not None and provider.is_enabled():
        try:
            generation = await provider.generate_candidates(prompt, candidate_count=candidate_count)
            source_chain.append("services.factor_llm_provider")
        except Exception as exc:
            fallback_reason = str(exc)
            warnings.append(f"llm_generation_failed: {exc}")
    elif prompt is not None:
        fallback_reason = "factor llm provider not configured"
        warnings.append(fallback_reason)

    if generation is None:
        if not allow_fallback:
            return fail(
                fallback_reason or "factor llm generation unavailable",
                source_chain=source_chain or ["services.factor_llm_provider"],
            )
        try:
            generation, fallback_source_chain, fallback_reasons = await _build_local_fallback_candidates(
                target_codes=codes,
                candidate_count=candidate_count,
                db=db,
                compile_factor_candidate_fn=compile_factor_candidate_fn,
                missing_kline_fn=missing_kline_fn,
                sort_klines_ascending_fn=sort_klines_ascending_fn,
                llm_alpha_miner_factory=llm_alpha_miner_factory,
            )
            source_chain.extend(fallback_source_chain)
            fallback_used = True
            warnings.extend(fallback_reasons[:10])
        except Exception as exc:
            return fail(
                f"LLM 因子生成失败，且本地回退也失败: {exc}",
                source_chain=source_chain or ["services.factor_llm_provider", "services.llm_alpha"],
            )

    pre_dedup_candidate_count = int(generation.get("candidate_count") or len(generation.get("candidates") or []))
    compiler_screening = _screen_compiler_candidates(
        list(generation.get("candidates") or []),
        compile_factor_candidate_fn=compile_factor_candidate_fn,
    )
    generation["candidates"] = list(compiler_screening.get("kept_candidates") or [])
    warnings.extend(list(compiler_screening.get("warnings") or []))
    source_chain.append("services.factor_candidate_compiler")

    if not generation.get("candidates"):
        fallback_reason = "llm generated 0 compiler-valid candidates"
        if allow_fallback and not fallback_used:
            try:
                generation, fallback_source_chain, fallback_reasons = await _build_local_fallback_candidates(
                    target_codes=codes,
                    candidate_count=candidate_count,
                    db=db,
                    compile_factor_candidate_fn=compile_factor_candidate_fn,
                    missing_kline_fn=missing_kline_fn,
                    sort_klines_ascending_fn=sort_klines_ascending_fn,
                    llm_alpha_miner_factory=llm_alpha_miner_factory,
                )
                source_chain.extend(fallback_source_chain)
                fallback_used = True
                warnings.append(fallback_reason)
                warnings.extend(fallback_reasons[:10])
                compiler_screening = _screen_compiler_candidates(
                    list(generation.get("candidates") or []),
                    compile_factor_candidate_fn=compile_factor_candidate_fn,
                )
                generation["candidates"] = list(compiler_screening.get("kept_candidates") or [])
                warnings.extend(list(compiler_screening.get("warnings") or []))
            except Exception as exc:
                return fail(
                    f"LLM 产出的候选无法通过 compiler 校验，且本地回退也失败: {exc}",
                    source_chain=source_chain
                    or ["services.factor_llm_provider", "services.factor_candidate_compiler", "services.llm_alpha"],
                )

        if not generation.get("candidates"):
            return fail(
                "LLM generated 0 compiler-valid candidates",
                source_chain=source_chain or ["services.factor_llm_provider", "services.factor_candidate_compiler"],
            )

    try:
        memory_annotation = await memory_service.annotate_generated_candidates(
            list(generation.get("candidates") or []),
            codes=codes,
        )
        generation["candidates"] = list(memory_annotation.get("candidates") or generation.get("candidates") or [])
        dedup_summary.update(
            {
                "input_count": len(generation.get("candidates") or []),
                "kept_count": len(generation.get("candidates") or []),
                "blocked_count": 0,
                "blocked_ratio": 0.0,
            }
        )
        warnings.extend(list(memory_annotation.get("warnings") or []))
        source_chain.append("services.factor_research_memory")

        duplicate_policy = getattr(memory_service, "apply_duplicate_policy", None)
        if callable(duplicate_policy):
            policy_result = duplicate_policy(
                list(generation.get("candidates") or []),
                dedup_mode=dedup_mode,
                high_similarity_threshold=dedup_high_similarity_threshold,
                failure_similarity_threshold=dedup_failure_similarity_threshold,
            )
            generation["candidates"] = list(policy_result.get("kept_candidates") or generation.get("candidates") or [])
            blocked_candidates = list(policy_result.get("blocked_candidates") or [])
            dedup_summary = dict(policy_result.get("summary") or dedup_summary)
            dedup_summary["mode"] = policy_result.get("mode") or dedup_mode
            warnings.extend(list(policy_result.get("warnings") or []))
    except Exception as exc:
        warnings.append(f"memory_annotation_failed: {exc}")

    artifact_id = str(kw.get("artifact_id") or f"factor_llm_{int(time.time())}_{uuid4().hex[:8]}")
    provider_name = generation.get("provider") or getattr(provider.config, "provider", "openai_compatible")
    model_name = generation.get("model") or getattr(provider.config, "model", "")
    generation_mode = "local_rule_fallback" if fallback_used else "llm_provider"
    research_theme = _derive_research_theme(list(generation.get("candidates") or []), codes=codes)
    generation["candidates"] = _enrich_generation_candidates(
        list(generation.get("candidates") or []),
        artifact_id=artifact_id,
        research_theme=research_theme,
        generation_mode=generation_mode,
        provider_name=str(provider_name),
        model_name=str(model_name),
        codes=codes,
    )
    combined_blocked_candidates = [
        *list(compiler_screening.get("rejected_candidates") or []),
        *blocked_candidates,
    ]
    research_episode = {
        "episode_id": artifact_id,
        "theme": research_theme,
        "hypothesis_summary": _build_hypothesis_summary(list(generation.get("candidates") or [])),
        "target_codes": list(codes or [])[:16],
        "candidate_count_requested": candidate_count,
        "candidate_count_generated": len(list(generation.get("candidates") or [])),
        "candidate_count_blocked": len(combined_blocked_candidates),
        "generation_mode": generation_mode,
        "provider": provider_name,
        "model": model_name,
        "novelty_summary": _summarize_novelty(list(generation.get("candidates") or [])),
        "memory_similarity_summary": _summarize_memory_similarity(list(generation.get("candidates") or [])),
        "blocked_candidate_summary": _summarize_blocked_candidates(combined_blocked_candidates),
        "dedup_summary": dict(dedup_summary),
        "startup_warmup_summary": _summarize_startup_warmup(startup_warmup),
        "prompt_context_summary": _summarize_prompt_context(prompt, memory_context),
        "analysis_summary": _summarize_analysis(dict(generation.get("analysis") or {})),
        "lineage_summary": {
            "artifact_id": artifact_id,
            "schema_path": getattr(prompt, "schema_path", ""),
            "source_chain": list(dict.fromkeys(str(item) for item in list(source_chain or []) if str(item).strip())),
            "fallback_used": bool(fallback_used),
        },
        "created_at": datetime.now().isoformat(),
    }
    payload = {
        "artifact_id": artifact_id,
        "action": "llm_factor_mining",
        "codes": codes,
        "generation_mode": generation_mode,
        "provider_enabled": bool(provider.is_enabled()),
        "provider": provider_name,
        "model": model_name,
        "candidate_count": len(generation.get("candidates") or []),
        "pre_dedup_candidate_count": pre_dedup_candidate_count,
        "compiler_screening": compiler_screening.get("summary") or {},
        "requested_candidate_count": candidate_count,
        "candidates": list(generation.get("candidates") or []),
        "blocked_candidates": combined_blocked_candidates,
        "dedup_summary": dedup_summary,
        "research_episode": research_episode,
        "analysis": dict(generation.get("analysis") or {}),
        "warnings": list(dict.fromkeys([*list(generation.get("warnings") or []), *warnings]))[:20],
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "artifact_persisted": bool(persist_artifact),
        "artifact_reusable": bool(persist_artifact),
        "dry_run": bool(dry_run),
        "degraded": bool(fallback_used or warnings or blocked_candidates),
        "startup_warmup": startup_warmup,
        "prompt_context": prompt.context_summary if (prompt is not None and kw.get("explain", True)) else None,
        "memory_context": memory_context if kw.get("explain", True) else {"available": bool(memory_context)},
        "schema_path": getattr(prompt, "schema_path", ""),
        "params": {
            "candidate_count": candidate_count,
            "max_candidates": kw.get("max_candidates"),
            "lookback_bars": lookback_bars,
            "alternative_lookback_days": alternative_lookback_days,
            "allow_fallback": allow_fallback,
            "workflow_fast_mode": workflow_fast_mode,
            "dedup_mode": dedup_mode,
            "dedup_high_similarity_threshold": dedup_high_similarity_threshold,
            "dedup_failure_similarity_threshold": dedup_failure_similarity_threshold,
            "startup_warmup_enabled": startup_warmup_enabled,
            "startup_warmup_force": startup_warmup_force,
            "startup_warmup_limit": startup_warmup_limit,
            "startup_warmup_task_type": startup_warmup_task_type,
        },
    }
    if kw.get("explain", True) and prompt is not None:
        payload["prompt_preview"] = {
            "system_prompt": prompt.system_prompt,
            "user_prompt": prompt.user_prompt,
        }

    if persist_artifact:
        await register_artifact_async_fn(
            {
                "artifact_id": artifact_id,
                "strategy": "quant_llm_factor_mining",
                "strategy_version": "p0.v1",
                "code": ",".join(codes[:5]),
                "payload": payload,
                "created_at": datetime.now().isoformat(),
            }
        )
        source_chain.append("services.artifact_registry")

    return ok(
        payload,
        source_chain=source_chain or ["services.factor_prompt_builder", "services.factor_llm_provider"],
    )
