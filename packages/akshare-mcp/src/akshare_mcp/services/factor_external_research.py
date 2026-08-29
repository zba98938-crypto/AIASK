"""External factor research ingestion for candidate evidence and review records."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .artifact_registry import get_artifact_async, register_artifact_async
from .factor_candidate_compiler import compile_factor_candidate
from .factor_candidate_storage import (
    get_factor_candidate_record_async,
    save_factor_candidate_record_async,
)

FACTOR_EXTERNAL_RESEARCH_STRATEGY = "factor_external_research_evidence"
FACTOR_EXTERNAL_RESEARCH_VERSION = "p1.v1"


DEFAULT_PUBLIC_FACTOR_RESEARCH_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source": "Oxford Academic RFS",
        "url": "https://academic.oup.com/rfs/article/29/1/5/1843824",
        "title": "... and the Cross-Section of Expected Returns",
        "published_at": "2016-01-01",
        "summary": (
            "Harvey, Liu and Zhu document factor-zoo and multiple-testing risk; "
            "candidate factors need local out-of-sample and multiplicity checks."
        ),
        "extracted_factor_idea": "momentum and reversal candidates require strict local validation",
        "factor_family": "momentum",
        "license": "public_metadata_only",
    },
    {
        "source": "SSRN",
        "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3341728",
        "title": "A Census of the Factor Zoo",
        "published_at": "2019-02-25",
        "summary": (
            "The factor-zoo literature is broad and redundant; provenance, de-duplication, "
            "and validation gates are required before promotion."
        ),
        "extracted_factor_idea": "liquidity, momentum and low-volatility families need de-duplication",
        "factor_family": "liquidity",
        "license": "public_metadata_only",
    },
    {
        "source": "NBER",
        "url": "https://www.nber.org/papers/w31719",
        "title": "High-Dimensional Factor Models and the Factor Zoo",
        "published_at": "2023-09-01",
        "summary": (
            "High-dimensional factor research motivates separating candidate discovery "
            "from governed validation and production admission."
        ),
        "extracted_factor_idea": "volatility and trend families should be treated as candidates",
        "factor_family": "volatility",
        "license": "public_metadata_only",
    },
)

_MAX_METADATA_BYTES = 192_000
_USER_AGENT = "aiask-factor-research-metadata/1.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: Any, *, limit: int = 2000) -> str:
    text = " ".join(html.unescape(str(value or "")).strip().split())
    return text[: max(1, int(limit))]


def _normalize_codes(codes: Any) -> list[str]:
    if isinstance(codes, str):
        raw = re.split(r"[,;|\s]+", codes)
    elif isinstance(codes, (list, tuple, set)):
        raw = list(codes)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _decode_sources(raw_sources: Any) -> list[dict[str, Any]]:
    if raw_sources is None or raw_sources == "":
        return [deepcopy(item) for item in DEFAULT_PUBLIC_FACTOR_RESEARCH_SOURCES]
    if isinstance(raw_sources, str):
        text = raw_sources.strip()
        if not text:
            return [deepcopy(item) for item in DEFAULT_PUBLIC_FACTOR_RESEARCH_SOURCES]
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [
                {"url": item.strip()}
                for item in re.split(r"[\n,]+", text)
                if item.strip()
            ]
    else:
        parsed = raw_sources
    if isinstance(parsed, dict):
        parsed = [parsed]
    sources: list[dict[str, Any]] = []
    for item in list(parsed or []):
        if isinstance(item, str):
            item = {"url": item}
        if isinstance(item, dict):
            sources.append(deepcopy(item))
    return sources


def _stable_id(prefix: str, *parts: Any, size: int = 16) -> str:
    raw = "\n".join(_normalize_text(part, limit=4000) for part in parts if part is not None)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[: max(8, int(size))]
    return f"{prefix}_{digest}"


def _extract_html_metadata(raw_html: str) -> dict[str, str]:
    text = str(raw_html or "")
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        title = _normalize_text(re.sub(r"<[^>]+>", " ", match.group(1)), limit=240)

    meta: dict[str, str] = {}
    for key in (
        "description",
        "og:description",
        "twitter:description",
        "citation_title",
        "citation_publication_date",
    ):
        pattern = (
            r"<meta[^>]+(?:name|property)=['\"]"
            + re.escape(key)
            + r"['\"][^>]+content=['\"]([^'\"]+)['\"][^>]*>"
        )
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            meta[key] = _normalize_text(m.group(1), limit=600)

    return {
        "title": meta.get("citation_title") or title,
        "summary": meta.get("description") or meta.get("og:description") or meta.get("twitter:description") or "",
        "published_at": meta.get("citation_publication_date") or "",
    }


def _fetch_public_metadata_sync(url: str, *, timeout_sec: float) -> dict[str, Any]:
    normalized_url = str(url or "").strip()
    if not normalized_url.startswith(("https://", "http://")):
        return {"available": False, "error": "unsupported_url_scheme"}
    request = urllib.request.Request(
        normalized_url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_sec or 8.0))) as response:
            content_type = str(response.headers.get("content-type") or "").lower()
            if "html" not in content_type and "text/" not in content_type:
                return {"available": False, "error": f"unsupported_content_type:{content_type[:80]}"}
            raw = response.read(_MAX_METADATA_BYTES)
    except urllib.error.HTTPError as exc:
        return {"available": False, "error": f"http_error:{exc.code}"}
    except Exception as exc:
        return {"available": False, "error": f"{exc.__class__.__name__}:{str(exc)[:160]}"}
    try:
        decoded = raw.decode("utf-8", errors="replace")
    except Exception:
        decoded = str(raw)
    metadata = _extract_html_metadata(decoded)
    return {
        "available": bool(metadata.get("title") or metadata.get("summary")),
        "error": "",
        "metadata": metadata,
        "bytes_read": len(raw),
        "body_stored": False,
    }


async def _fetch_public_metadata(url: str, *, timeout_sec: float) -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_public_metadata_sync, url, timeout_sec=timeout_sec)


def _normalize_evidence_source(source: dict[str, Any], *, fetched: dict[str, Any] | None = None) -> dict[str, Any]:
    fetched = fetched if isinstance(fetched, dict) else {}
    fetched_meta = fetched.get("metadata") if isinstance(fetched.get("metadata"), dict) else {}
    title = _normalize_text(source.get("title") or fetched_meta.get("title"), limit=240)
    summary = _normalize_text(source.get("summary") or fetched_meta.get("summary"), limit=1000)
    idea = _normalize_text(source.get("extracted_factor_idea") or source.get("factor_idea") or summary or title, limit=500)
    url = _normalize_text(source.get("url"), limit=500)
    evidence_id = str(source.get("evidence_id") or "").strip() or _stable_id(
        "factor_external_research",
        url,
        title,
        idea,
        size=16,
    )
    family = _normalize_text(source.get("factor_family") or source.get("family") or "", limit=60).lower()
    provenance = dict(source.get("provenance") or {})
    provenance.update(
        {
            "access": _normalize_text(source.get("access") or "public", limit=40),
            "body_stored": False,
            "stored_fields": ["source", "url", "title", "published_at", "summary", "extracted_factor_idea"],
            "network_fetch": {
                "attempted": bool(fetched),
                "available": bool(fetched.get("available")),
                "error": str(fetched.get("error") or ""),
                "bytes_read": int(fetched.get("bytes_read") or 0),
            },
        }
    )
    return {
        "evidence_id": evidence_id,
        "source": _normalize_text(source.get("source") or source.get("provider") or "public_web", limit=120),
        "url": url,
        "title": title or url,
        "published_at": _normalize_text(source.get("published_at") or fetched_meta.get("published_at"), limit=80),
        "summary": summary,
        "extracted_factor_idea": idea,
        "factor_family": family,
        "license": _normalize_text(source.get("license") or "public_metadata_only", limit=120),
        "provenance": provenance,
        "candidate": deepcopy(source.get("candidate")) if isinstance(source.get("candidate"), dict) else None,
        "expression_dsl": _normalize_text(source.get("expression_dsl"), limit=600),
        "inputs": list(source.get("inputs") or []) if isinstance(source.get("inputs"), (list, tuple, set)) else [],
        "tags": list(source.get("tags") or []) if isinstance(source.get("tags"), (list, tuple, set)) else [],
        "ingested_at": _now_iso(),
    }


async def collect_external_factor_research(
    *,
    sources: Any = None,
    limit: Any = 20,
    allow_network: Any = True,
    timeout_sec: Any = 8.0,
) -> dict[str, Any]:
    """Collect public metadata for factor research sources without storing article bodies."""

    resolved_sources = _decode_sources(sources)
    resolved_limit = max(1, min(int(limit or 20), 100))
    resolved_timeout = max(1.0, min(float(timeout_sec or 8.0), 30.0))
    network_enabled = _as_bool(allow_network, True)
    evidences: list[dict[str, Any]] = []
    errors: list[str] = []

    for source in resolved_sources[:resolved_limit]:
        if not isinstance(source, dict):
            continue
        fetched: dict[str, Any] = {}
        url = str(source.get("url") or "").strip()
        if network_enabled and url and _as_bool(source.get("fetch_metadata", True), True):
            fetched = await _fetch_public_metadata(url, timeout_sec=resolved_timeout)
            if fetched.get("error"):
                errors.append(f"{url}:{fetched.get('error')}")
        evidence = _normalize_evidence_source(source, fetched=fetched)
        if evidence.get("title") or evidence.get("summary") or evidence.get("url"):
            evidences.append(evidence)

    return {
        "evidence": evidences,
        "count": len(evidences),
        "allow_network": network_enabled,
        "timeout_sec": resolved_timeout,
        "errors": errors[:20],
    }


def build_external_factor_evidence_artifact(evidence: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(evidence if isinstance(evidence, dict) else {})
    evidence_id = str(payload.get("evidence_id") or "").strip() or _stable_id(
        "factor_external_research",
        payload.get("url"),
        payload.get("title"),
        payload.get("extracted_factor_idea"),
        size=16,
    )
    payload["evidence_id"] = evidence_id
    payload["artifact_id"] = evidence_id
    payload["record_type"] = "factor_external_research_evidence"
    payload["status"] = "evidence_only"
    payload.setdefault("created_at", _now_iso())
    payload["updated_at"] = _now_iso()
    return {
        "artifact_id": evidence_id,
        "strategy": FACTOR_EXTERNAL_RESEARCH_STRATEGY,
        "strategy_version": FACTOR_EXTERNAL_RESEARCH_VERSION,
        "code": "",
        "payload": payload,
        "created_at": payload.get("created_at"),
    }


def _keywords(evidence: dict[str, Any]) -> set[str]:
    text = " ".join(
        [
            str(evidence.get("factor_family") or ""),
            str(evidence.get("title") or ""),
            str(evidence.get("summary") or ""),
            str(evidence.get("extracted_factor_idea") or ""),
        ]
    ).lower()
    tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", text))
    if "low" in tokens and "volatility" in tokens:
        tokens.add("low_volatility")
    return tokens


def _template_for_evidence(evidence: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    explicit_candidate = evidence.get("candidate")
    if isinstance(explicit_candidate, dict) and explicit_candidate.get("expression_dsl"):
        candidate = deepcopy(explicit_candidate)
        candidate.setdefault("source_model", "external_research_ingest")
        return candidate, "explicit_candidate"

    explicit_expression = str(evidence.get("expression_dsl") or "").strip()
    explicit_inputs = [str(item).strip() for item in list(evidence.get("inputs") or []) if str(item).strip()]
    if explicit_expression and explicit_inputs:
        candidate = {
            "name": _normalize_text(evidence.get("candidate_name") or evidence.get("title") or "external_factor_candidate", limit=72),
            "family": _normalize_text(evidence.get("factor_family") or "external", limit=40) or "external",
            "hypothesis": _normalize_text(evidence.get("extracted_factor_idea") or evidence.get("summary"), limit=360),
            "inputs": explicit_inputs,
            "expression_dsl": explicit_expression,
            "expected_holding_period": int(evidence.get("expected_holding_period") or 10),
            "expected_regime": ["external_research"],
            "complexity_hint": "medium",
            "novelty_rationale": "Normalized from authorized external factor research metadata.",
            "source_model": "external_research_ingest",
        }
        return candidate, "explicit_expression"

    tokens = _keywords(evidence)
    templates: list[tuple[set[str], dict[str, Any], str]] = [
        (
            {"reversal", "反转"},
            {
                "family": "reversal",
                "inputs": ["return_5d"],
                "expression_dsl": "-return_5d",
                "expected_holding_period": 5,
            },
            "reversal_proxy",
        ),
        (
            {"momentum", "trend", "动量", "趋势"},
            {
                "family": "momentum",
                "inputs": ["momentum_20d", "momentum_60d"],
                "expression_dsl": "zscore(momentum_20d, 20) + zscore(momentum_60d, 20)",
                "expected_holding_period": 20,
            },
            "momentum_proxy",
        ),
        (
            {"low_volatility", "volatility", "risk", "波动", "低波"},
            {
                "family": "volatility",
                "inputs": ["volatility_20d"],
                "expression_dsl": "-zscore(volatility_20d, 20)",
                "expected_holding_period": 20,
            },
            "low_volatility_proxy",
        ),
        (
            {"liquidity", "volume", "turnover", "流动性", "成交"},
            {
                "family": "liquidity",
                "inputs": ["volume_ratio_5_20"],
                "expression_dsl": "zscore(volume_ratio_5_20, 10)",
                "expected_holding_period": 10,
            },
            "liquidity_proxy",
        ),
    ]
    for required, template, reason in templates:
        if tokens & required:
            suffix = hashlib.sha1(str(evidence.get("evidence_id") or evidence.get("url") or "").encode("utf-8")).hexdigest()[:8]
            family = template["family"]
            candidate = {
                "name": f"external_{family}_{suffix}",
                "family": family,
                "hypothesis": _normalize_text(
                    evidence.get("extracted_factor_idea")
                    or evidence.get("summary")
                    or f"External research suggests a {family} candidate for local validation.",
                    limit=360,
                ),
                "inputs": list(template["inputs"]),
                "expression_dsl": str(template["expression_dsl"]),
                "expected_holding_period": int(template["expected_holding_period"]),
                "expected_regime": [family, "external_research", "requires_validation"],
                "complexity_hint": "low",
                "novelty_rationale": "External evidence was normalized into a supported local DSL proxy; validation decides promotion.",
                "generation_trace": {
                    "mode": "external_factor_research_ingest",
                    "template_reason": reason,
                    "source": evidence.get("source"),
                    "url": evidence.get("url"),
                    "evidence_id": evidence.get("evidence_id"),
                    "body_stored": False,
                },
                "source_model": "external_research_ingest",
            }
            return candidate, reason
    return None, "no_supported_local_dsl_mapping"


def _external_evidence_preview(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": evidence.get("evidence_id"),
        "source": evidence.get("source"),
        "url": evidence.get("url"),
        "title": evidence.get("title"),
        "published_at": evidence.get("published_at"),
        "license": evidence.get("license"),
        "summary": _normalize_text(evidence.get("summary"), limit=500),
        "extracted_factor_idea": _normalize_text(evidence.get("extracted_factor_idea"), limit=300),
        "provenance": deepcopy(evidence.get("provenance") or {}),
    }


def build_external_factor_candidate_record(
    evidence: dict[str, Any],
    *,
    codes: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    candidate, mapping_reason = _template_for_evidence(evidence)
    if not candidate:
        return None, {
            "valid": False,
            "reason": mapping_reason,
            "compiler_valid": False,
            "compiler_warnings": [],
        }
    trace = dict(candidate.get("generation_trace") or {})
    trace.update(
        {
            "mode": "external_factor_research_ingest",
            "source": evidence.get("source"),
            "url": evidence.get("url"),
            "evidence_id": evidence.get("evidence_id"),
            "mapping_reason": mapping_reason,
            "body_stored": False,
        }
    )
    candidate["generation_trace"] = trace
    try:
        compiled = compile_factor_candidate(candidate)
    except Exception as exc:
        compiled = {
            "valid": False,
            "warnings": [f"compile_exception:{type(exc).__name__}:{str(exc)[:120]}"],
            "unsupported_fields": [],
            "unsupported_functions": [],
        }
    if not bool(compiled.get("valid")):
        return None, {
            "valid": False,
            "reason": "compiler_invalid",
            "compiler_valid": False,
            "compiler_warnings": list(compiled.get("warnings") or []),
            "unsupported_fields": list(compiled.get("unsupported_fields") or []),
            "unsupported_functions": list(compiled.get("unsupported_functions") or []),
            "mapping_reason": mapping_reason,
        }
    artifact_id = _stable_id(
        "factor_memory_external",
        evidence.get("evidence_id"),
        candidate.get("family"),
        candidate.get("name"),
        candidate.get("expression_dsl"),
        size=16,
    )
    status = "review"
    record = {
        "artifact_id": artifact_id,
        "status": status,
        "codes": _normalize_codes(codes),
        "candidate": candidate,
        "family": candidate.get("family"),
        "tags": [
            "external_factor_research",
            str(candidate.get("family") or "external").strip().lower(),
            "requires_validation",
            "provenance_recorded",
        ],
        "rating": {
            "grade": "C",
            "recommendation": "review",
            "reason": "External research evidence only; local validation gates promotion.",
        },
        "metrics": {
            "rank_ic_mean": 0.0,
            "external_evidence_count": 1,
        },
        "external_evidence": [_external_evidence_preview(evidence)],
        "memory_flags": {
            "seeded_from_external_research": True,
            "requires_validation": True,
            "validation_gate_status": "review",
            "active_pool_eligible": False,
            "active_pool_block_reasons": ["requires_local_validation"],
            "compiler_valid": bool(compiled.get("valid")),
            "compiler_warnings": list(compiled.get("warnings") or []),
            "unsupported_fields": list(compiled.get("unsupported_fields") or []),
            "unsupported_functions": list(compiled.get("unsupported_functions") or []),
            "mapping_reason": mapping_reason,
            "body_stored": False,
        },
        "source_chain": [
            "services.factor_external_research",
            "services.factor_candidate_storage",
        ],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    return record, {
        "valid": bool(compiled.get("valid")),
        "reason": mapping_reason,
        "compiler_valid": bool(compiled.get("valid")),
        "compiler_warnings": list(compiled.get("warnings") or []),
        "unsupported_fields": list(compiled.get("unsupported_fields") or []),
        "unsupported_functions": list(compiled.get("unsupported_functions") or []),
    }


async def ingest_external_factor_research(
    db: Any = None,
    *,
    sources: Any = None,
    limit: Any = 20,
    codes: Any = None,
    allow_network: Any = True,
    timeout_sec: Any = 8.0,
    create_candidates: Any = True,
    rebuild_existing: Any = False,
    dry_run: Any = False,
) -> dict[str, Any]:
    """Ingest external research as evidence and review-only factor candidate memory."""

    del db  # Persistence goes through the artifact registry, matching candidate memory.
    resolved_dry_run = _as_bool(dry_run, False)
    resolved_rebuild = _as_bool(rebuild_existing, False)
    resolved_create_candidates = _as_bool(create_candidates, True)
    collected = await collect_external_factor_research(
        sources=sources,
        limit=limit,
        allow_network=allow_network,
        timeout_sec=timeout_sec,
    )
    result: dict[str, Any] = {
        "source": "external_factor_research",
        "limit": max(1, min(int(limit or 20), 100)),
        "allow_network": bool(collected.get("allow_network")),
        "dry_run": resolved_dry_run,
        "create_candidates": resolved_create_candidates,
        "evidence_records": 0,
        "saved_evidence_records": 0,
        "skipped_existing_evidence_records": 0,
        "candidate_records": 0,
        "saved_candidate_records": 0,
        "skipped_existing_candidate_records": 0,
        "skipped_candidate_records": 0,
        "compile_valid_records": 0,
        "compile_degraded_records": 0,
        "errors": list(collected.get("errors") or []),
        "skipped_reasons": {},
        "evidence_ids": [],
        "candidate_artifact_ids": [],
    }
    for evidence in list(collected.get("evidence") or []):
        evidence_artifact = build_external_factor_evidence_artifact(evidence)
        evidence_id = str(evidence_artifact.get("artifact_id") or "")
        result["evidence_records"] += 1
        result["evidence_ids"].append(evidence_id)
        try:
            existing_evidence = await get_artifact_async(evidence_id)
            if existing_evidence and not resolved_rebuild:
                result["skipped_existing_evidence_records"] += 1
            elif not resolved_dry_run:
                await register_artifact_async(evidence_artifact)
                result["saved_evidence_records"] += 1
            else:
                result["saved_evidence_records"] += 1
        except Exception as exc:
            result["errors"].append(f"{evidence_id}:{type(exc).__name__}:{str(exc)[:160]}")
            continue

        if not resolved_create_candidates:
            continue
        record, diagnostics = build_external_factor_candidate_record(evidence, codes=codes)
        if not record:
            reason = str(diagnostics.get("reason") or "candidate_not_built")
            result["skipped_candidate_records"] += 1
            result["skipped_reasons"][reason] = int(result["skipped_reasons"].get(reason, 0)) + 1
            continue
        result["candidate_records"] += 1
        artifact_id = str(record.get("artifact_id") or "")
        result["candidate_artifact_ids"].append(artifact_id)
        if bool(diagnostics.get("compiler_valid")):
            result["compile_valid_records"] += 1
        else:
            result["compile_degraded_records"] += 1
        try:
            existing_candidate = await get_factor_candidate_record_async(artifact_id)
            if existing_candidate and not resolved_rebuild:
                result["skipped_existing_candidate_records"] += 1
                continue
            if not resolved_dry_run:
                await save_factor_candidate_record_async(record, artifact_id=artifact_id)
            result["saved_candidate_records"] += 1
        except Exception as exc:
            result["errors"].append(f"{artifact_id}:{type(exc).__name__}:{str(exc)[:160]}")

    if len(result["errors"]) > 20:
        total = len(result["errors"])
        result["errors"] = list(result["errors"][:20]) + [f"...and {total - 20} more"]
    return result


__all__ = [
    "FACTOR_EXTERNAL_RESEARCH_STRATEGY",
    "FACTOR_EXTERNAL_RESEARCH_VERSION",
    "DEFAULT_PUBLIC_FACTOR_RESEARCH_SOURCES",
    "build_external_factor_candidate_record",
    "build_external_factor_evidence_artifact",
    "collect_external_factor_research",
    "ingest_external_factor_research",
]
