"""Skill registry metadata and discovery helpers."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Mapping, Optional


_FALLBACK_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "momentum_screen",
        "name": "Momentum Screen",
        "category": "screening",
        "description": "fallback demo",
    },
    {
        "id": "value_screen",
        "name": "Value Screen",
        "category": "screening",
        "description": "fallback demo",
    },
    {
        "id": "trend_follow",
        "name": "Trend Follow",
        "category": "strategy",
        "description": "fallback demo",
    },
]

_SKILL_STATUS_VALUES = {"registered", "executable", "deprecated"}
_QUOTED_VALUE_PATTERN = re.compile(r"""^(?P<quote>["'])(?P<body>.*)(?P=quote)$""")
_ORCHESTRATED_SKILL_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["completed", "partial_failed", "unsupported_task"],
        },
        "steps": {"type": "array", "items": {"type": "object"}},
        "summary": {"type": "object"},
    },
    "required": ["task", "status", "steps", "summary"],
    "additionalProperties": True,
}

_SKILL_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "akshare-market": {
        "supported_tasks": ["smoke_test", "quick_scan", "quote_only"],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "enum": ["smoke_test", "quick_scan", "quote_only"],
                },
                "code": {"type": "string", "description": "6-digit stock code"},
                "daily_limit": {"type": "integer", "minimum": 1},
                "minute_limit": {"type": "integer", "minimum": 1},
                "minute_period": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
    },
    "akshare-fund-manager-pro": {
        "supported_tasks": ["full_cycle", "daily_brief", "smoke_test"],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "enum": ["full_cycle", "daily_brief", "smoke_test"],
                },
                "codes": {"type": "array", "items": {"type": "string"}},
                "code": {"type": "string"},
                "lookback_days": {"type": "integer", "minimum": 30},
                "total_capital": {"type": "number"},
                "method": {"type": "string"},
                "confidence": {"type": "number"},
                "benchmark": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
    },
}

_SKILL_CONTRACTS.update(
    {
        "akshare-asset-allocation": {
            "supported_tasks": ["allocation_plan", "rebalance_plan", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["allocation_plan", "rebalance_plan", "smoke_test"]},
                    "risk_profile": {"type": "string"},
                    "horizon_years": {"type": "number"},
                    "total_capital": {"type": "number"},
                    "liquidity_buffer": {"type": "number"},
                    "max_drawdown": {"type": "number"},
                    "asset_scope": {"type": "array", "items": {"type": "string"}},
                    "rebalance_frequency": {"type": "string"},
                    "rebalance_threshold": {"type": "number"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-fee-costs": {
            "supported_tasks": ["cost_sensitivity", "single_backtest", "batch_backtest", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "enum": ["cost_sensitivity", "single_backtest", "batch_backtest", "smoke_test"],
                    },
                    "code": {"type": "string"},
                    "codes": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "initial_capital": {"type": "number"},
                    "commission": {"type": "number"},
                    "slippage": {"type": "number"},
                    "turnover_per_year": {"type": "number"},
                    "years": {"type": "integer"},
                    "annual_return_assumption": {"type": "number"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-factor-mining": {
            "supported_tasks": ["candidate_pipeline", "candidate_generation", "candidate_registry", "scheduler_check", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "enum": ["candidate_pipeline", "candidate_generation", "candidate_registry", "scheduler_check", "smoke_test"],
                    },
                    "code": {"type": "string"},
                    "codes": {"type": "array", "items": {"type": "string"}},
                    "stock_codes": {"type": "array", "items": {"type": "string"}},
                    "artifact_id": {"type": "string"},
                    "candidate_index": {"type": "integer", "minimum": 0},
                    "candidate_count": {"type": "integer", "minimum": 1},
                    "lookback_bars": {"type": "integer", "minimum": 120},
                    "horizon_days": {"type": "integer", "minimum": 3},
                    "max_dates": {"type": "integer", "minimum": 20},
                    "limit": {"type": "integer", "minimum": 1},
                    "candidate": {"type": "object"},
                    "op": {"type": "string"},
                    "memory_op": {"type": "string"},
                    "run_scheduler_now": {"type": "boolean"},
                    "persist_artifact": {"type": "boolean"},
                    "write_memory": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-fund-news": {
            "supported_tasks": ["news_digest", "research_digest", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["news_digest", "research_digest", "smoke_test"]},
                    "code": {"type": "string"},
                    "keyword": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "news_limit": {"type": "integer", "minimum": 1},
                    "research_limit": {"type": "integer", "minimum": 1},
                    "market_news_limit": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-fundamental": {
            "supported_tasks": ["fundamental_snapshot", "financials_only", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["fundamental_snapshot", "financials_only", "smoke_test"]},
                    "code": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-investor-protection": {
            "supported_tasks": ["protection_brief", "audit_log", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["protection_brief", "audit_log", "smoke_test"]},
                    "region": {"type": "string"},
                    "broker_region": {"type": "string"},
                    "recommendation_context": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-ips-discipline": {
            "supported_tasks": ["draft_ips", "discipline_checklist", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["draft_ips", "discipline_checklist", "smoke_test"]},
                    "goal": {"type": "string"},
                    "horizon_years": {"type": "number"},
                    "risk_profile": {"type": "string"},
                    "max_drawdown": {"type": "number"},
                    "liquidity_need": {"type": "string"},
                    "rebalance_frequency": {"type": "string"},
                    "rebalance_threshold": {"type": "number"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-macro-options-alerts": {
            "supported_tasks": ["macro_options_brief", "alert_blueprint", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["macro_options_brief", "alert_blueprint", "smoke_test"]},
                    "indicator": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "underlying": {"type": "string"},
                    "expiry_month": {"type": "string"},
                    "alert_name": {"type": "string"},
                    "threshold": {"type": "number"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-performance-attribution": {
            "supported_tasks": ["attribution_report", "benchmark_frame", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["attribution_report", "benchmark_frame", "smoke_test"]},
                    "holdings": {"type": "array", "items": {"type": "object"}},
                    "portfolio_return": {"type": "number"},
                    "benchmark_return": {"type": "number"},
                    "risk_sources": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-portfolio": {
            "supported_tasks": ["portfolio_backtest", "batch_backtest", "allocation_snapshot", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["portfolio_backtest", "batch_backtest", "allocation_snapshot", "smoke_test"]},
                    "holdings": {"type": "array", "items": {"type": "object"}},
                    "codes": {"type": "array", "items": {"type": "string"}},
                    "strategy": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "initial_capital": {"type": "number"},
                    "benchmark": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-portfolio-manager-core": {
            "supported_tasks": ["closed_loop_plan", "execution_gate", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["closed_loop_plan", "execution_gate", "smoke_test"]},
                    "holdings": {"type": "array", "items": {"type": "object"}},
                    "benchmark": {"type": "string"},
                    "cash_ratio": {"type": "number"},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-quant": {
            "supported_tasks": ["factor_inventory", "signal_research", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["factor_inventory", "signal_research", "smoke_test"]},
                    "code": {"type": "string"},
                    "factors": {"type": "array", "items": {"type": "string"}},
                    "lookback_days": {"type": "integer"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-quant-data-engineering": {
            "supported_tasks": ["quality_check", "warmup_blueprint", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["quality_check", "warmup_blueprint", "smoke_test"]},
                    "dataset": {"type": "string"},
                    "codes": {"type": "array", "items": {"type": "string"}},
                    "lookback_days": {"type": "integer"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-quant-methods-foundation": {
            "supported_tasks": ["risk_metrics", "correlation_frame", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["risk_metrics", "correlation_frame", "smoke_test"]},
                    "returns": {"type": "array", "items": {"type": "number"}},
                    "benchmark_returns": {"type": "array", "items": {"type": "number"}},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-quant-ml-signals": {
            "supported_tasks": ["signal_guardrails", "research_card", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["signal_guardrails", "research_card", "smoke_test"]},
                    "features": {"type": "array", "items": {"type": "string"}},
                    "model_type": {"type": "string"},
                    "label_definition": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-quant-research-process": {
            "supported_tasks": ["stage_gate", "backtest_gate", "smoke_test"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "enum": ["stage_gate", "backtest_gate", "smoke_test"]},
                    "candidate": {"type": "object"},
                    "validation_metrics": {"type": "object"},
                    "backtest_metrics": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-strategy-factory": {
            "supported_tasks": [
                "factory_cycle",
                "strategy_review",
                "submission_gate",
                "incubation_pipeline",
                "runtime_governance",
                "vector_governance",
                "domain_projection",
                "ai_generation",
                "smoke_test",
            ],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "enum": [
                            "factory_cycle",
                            "strategy_review",
                            "submission_gate",
                            "incubation_pipeline",
                            "runtime_governance",
                            "vector_governance",
                            "domain_projection",
                            "ai_generation",
                            "smoke_test",
                        ],
                    },
                    "strategy_id": {"type": "string"},
                    "id": {"type": "string"},
                    "strategy_ids": {"type": "array", "items": {"type": "string"}},
                    "run_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "status": {"type": "string"},
                    "statuses": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "signal_date": {"type": "string"},
                    "pipeline_stage": {"type": "string"},
                    "pipeline_status": {"type": "string"},
                    "source": {"type": "string"},
                    "parent_strategy_id": {"type": "string"},
                    "generated_strategy_id": {"type": "string"},
                    "task_name": {"type": "string"},
                    "task_scope": {"type": "string"},
                    "task_status": {"type": "string"},
                    "task_run_id": {"type": "integer", "minimum": 1},
                    "index_name": {"type": "string"},
                    "index_version": {"type": "string"},
                    "profile_type": {"type": "string"},
                    "similar_to": {"type": "string"},
                    "candidate_limit": {"type": "integer", "minimum": 1},
                    "limit_profiles": {"type": "integer", "minimum": 1},
                    "vector_method": {"type": "string"},
                    "keep_versions": {"type": "integer", "minimum": 0},
                    "protect_versions": {"type": "array", "items": {"type": "string"}},
                    "dry_run": {"type": "boolean"},
                    "trigger_factory_run": {"type": "boolean"},
                    "trigger_runtime_cycle": {"type": "boolean"},
                    "trigger_review_report_recheck": {"type": "boolean"},
                    "trigger_submission_replay": {"type": "boolean"},
                    "trigger_submit": {"type": "boolean"},
                    "trigger_incubation_sync": {"type": "boolean"},
                    "trigger_incubation_pipeline_run": {"type": "boolean"},
                    "trigger_promotion_review": {"type": "boolean"},
                    "trigger_risk_scan": {"type": "boolean"},
                    "trigger_risk_recovery": {"type": "boolean"},
                    "trigger_resolve_risk_event": {"type": "boolean"},
                    "trigger_runtime_alert_dispatch": {"type": "boolean"},
                    "trigger_runtime_alert_ack": {"type": "boolean"},
                    "trigger_runtime_control_set": {"type": "boolean"},
                    "trigger_vector_reconcile": {"type": "boolean"},
                    "trigger_vector_rebuild": {"type": "boolean"},
                    "trigger_vector_cleanup": {"type": "boolean"},
                    "trigger_domain_projection_rebuild": {"type": "boolean"},
                    "trigger_ai_generate": {"type": "boolean"},
                    "runtime_alert_limit": {"type": "integer", "minimum": 1},
                    "event_id": {"type": "integer", "minimum": 1},
                    "alert_id": {"type": "integer", "minimum": 1},
                    "recheck_reports": {"type": "boolean"},
                    "auto_apply": {"type": "boolean"},
                    "auto_apply_review": {"type": "boolean"},
                    "auto_submit": {"type": "boolean"},
                    "control_mode": {"type": "string"},
                    "reason": {"type": "string"},
                    "resolution": {"type": "string"},
                    "acknowledged_by": {"type": "string"},
                    "trigger_event_type": {"type": "string"},
                    "enforce_actions": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-stock-deep-analysis": {
            "supported_tasks": ["quick_scan", "deep_analysis", "recover_gaps", "rebuild_report", "trade_plan"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "enum": ["quick_scan", "deep_analysis", "recover_gaps", "rebuild_report", "trade_plan"],
                    },
                    "code": {"type": "string"},
                    "stock_code": {"type": "string"},
                    "symbol": {"type": "string"},
                    "run_id": {"type": "string"},
                    "investment_style": {"type": "string"},
                    "user_id": {"type": "string"},
                    "market": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
        "akshare-trading-decision": {
            "supported_tasks": ["trade_plan", "quick_scan", "deep_analysis", "recover_gaps", "rebuild_report"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "enum": ["trade_plan", "quick_scan", "deep_analysis", "recover_gaps", "rebuild_report"],
                    },
                    "code": {"type": "string"},
                    "stock_code": {"type": "string"},
                    "symbol": {"type": "string"},
                    "run_id": {"type": "string"},
                    "investment_style": {"type": "string"},
                    "style": {"type": "string"},
                    "user_id": {"type": "string"},
                    "market": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "output_schema": _ORCHESTRATED_SKILL_OUTPUT_SCHEMA,
        },
    }
)


def _find_repo_skills_root() -> Path | None:
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        candidate = parent / ".codex" / "skills"
        if candidate.is_dir():
            return candidate
    return None


def _find_repo_root() -> Path | None:
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        if (parent / ".codex" / "skills").is_dir():
            return parent
    return None


def _list_skill_roots() -> List[Path]:
    roots: List[Path] = []
    repo_root = _find_repo_skills_root()
    if repo_root is not None:
        roots.append(repo_root)

    home_root = Path.home() / ".codex" / "skills"
    if home_root.is_dir() and home_root not in roots:
        roots.append(home_root)
    return roots


def _filter_visible_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    visible: List[Dict[str, Any]] = []
    for item in list(skills or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("id") or "").strip()
        if not skill_id:
            continue
        visible.append(dict(item))
    return visible


def _parse_bool_flag(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _normalize_skill_status(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in _SKILL_STATUS_VALUES:
        return text
    return None


def _strip_wrapping_quotes(value: str) -> str:
    match = _QUOTED_VALUE_PATTERN.match(value.strip())
    if not match:
        return value.strip()
    return match.group("body").strip()


def _parse_frontmatter_value(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return ""

    if text[0] in "[{":
        try:
            return json.loads(text)
        except Exception:
            try:
                return ast.literal_eval(text)
            except Exception:
                return text

    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None

    if _QUOTED_VALUE_PATTERN.match(text):
        return _strip_wrapping_quotes(text)

    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except Exception:
            return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except Exception:
            return text

    return text


def _parse_skill_md(md_path: Path) -> Dict[str, Any]:
    skill_id = md_path.parent.name
    name = skill_id
    description = ""
    status: Optional[str] = None
    deprecated: Optional[bool] = None
    frontmatter: Dict[str, Any] = {}

    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                stripped = line.strip()
                if stripped == "---":
                    break
                if ":" not in stripped:
                    continue
                key, raw_value = stripped.split(":", 1)
                key = key.strip().lower()
                value = _parse_frontmatter_value(raw_value.strip())
                frontmatter[key] = value
                if key == "name":
                    name = str(value or "").strip() or name
                elif key == "description":
                    description = str(value or "").strip()
                elif key == "status":
                    status = _normalize_skill_status(value)
                elif key == "deprecated":
                    deprecated = _parse_bool_flag(value)
    except Exception:
        pass

    category = "general"
    if "-" in skill_id:
        parts = skill_id.split("-")
        if len(parts) >= 2:
            category = parts[1]

    payload = {
        "id": skill_id,
        "name": name,
        "category": category,
        "description": description,
        "path": str(md_path),
    }
    for key, value in frontmatter.items():
        if key in {"name", "description", "status", "deprecated"}:
            continue
        payload[key] = value
    if status is not None:
        payload["status"] = status
    if deprecated is not None:
        payload["deprecated"] = deprecated
    return payload


def _load_skills() -> List[Dict[str, Any]]:
    skills_roots = _list_skill_roots()
    deduped: Dict[str, Dict[str, Any]] = {}
    for skills_root in skills_roots or []:
        for md in skills_root.glob("*/SKILL.md"):
            if md.parent.name.startswith("_"):
                continue
            parsed = _parse_skill_md(md)
            skill_id = str(parsed.get("id") or "")
            if not skill_id or skill_id in deduped:
                continue
            deduped[skill_id] = parsed

    skills = list(deduped.values())
    if not skills:
        skills = _filter_visible_skills(_FALLBACK_SKILLS)

    for skill_id, contract in _SKILL_CONTRACTS.items():
        if skill_id in deduped:
            continue
        skills.append(
            {
                "id": skill_id,
                "name": skill_id,
                "category": skill_id.split("-", 2)[1] if "-" in skill_id else "general",
                "description": "Runtime-orchestrated AKShare MCP skill",
                "runtime_status": "executable",
                "status": str(contract.get("status") or "executable"),
                "source": "runtime_contract",
            }
        )

    skills.sort(key=lambda x: x.get("id", ""))
    return list(skills or [])


def _load_skill_coverage_audit() -> Dict[str, Any] | None:
    repo_root = _find_repo_root()
    if repo_root is None:
        return None
    audit_path = repo_root / "skill_tool_coverage_runtime.json"
    if not audit_path.is_file():
        return None
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _load_skill_capability_audit() -> Dict[str, Any] | None:
    repo_root = _find_repo_root()
    if repo_root is None:
        return None
    audit_path = repo_root / ".codex" / "skills" / "_meta" / "skill_capability_audit.json"
    if not audit_path.is_file():
        return None
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _is_repo_local_skill(skill: Dict[str, Any]) -> bool:
    repo_root = _find_repo_root()
    path = str(skill.get("path") or "")
    if repo_root is None or not path:
        return False
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False
    repo_skills_root = repo_root / ".codex" / "skills"
    try:
        resolved.relative_to(repo_skills_root)
        return True
    except Exception:
        return False


def _resolve_skill_status(skill: Dict[str, Any], *, handler_available: bool, contract: Dict[str, Any]) -> str:
    if contract.get("deprecated") or skill.get("deprecated"):
        return "deprecated"

    configured_status = _normalize_skill_status(contract.get("status")) or _normalize_skill_status(skill.get("status"))
    if configured_status == "deprecated":
        return "deprecated"
    return "executable" if handler_available else "registered"


def _enrich_skills(
    skills: List[Dict[str, Any]],
    *,
    available_handlers: Mapping[str, Callable[..., Any]],
    skill_contracts: Mapping[str, Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    contracts = skill_contracts or _SKILL_CONTRACTS
    enriched: List[Dict[str, Any]] = []
    for skill in skills:
        skill_id = str(skill.get("id") or "")
        contract = dict(contracts.get(skill_id) or {})
        handler_available = skill_id in available_handlers
        status = _resolve_skill_status(skill, handler_available=handler_available, contract=contract)
        executable = status == "executable" and handler_available
        execution_mode = "deprecated" if status == "deprecated" else ("orchestrated" if executable else "no_handler")
        enriched.append(
            {
                **skill,
                "status": status,
                "runtime_status": str(skill.get("runtime_status") or status),
                "executable": executable,
                "deprecated": status == "deprecated",
                "handler_available": handler_available,
                "execution_mode": execution_mode,
                "repo_local": _is_repo_local_skill(skill),
                "input_schema": contract.get("input_schema") or skill.get("input_schema"),
                "output_schema": contract.get("output_schema") or skill.get("output_schema"),
                "supported_tasks": list(contract.get("supported_tasks") or skill.get("supported_tasks") or []),
            }
        )
    return enriched


def _build_skill_registry_summary(
    skills: List[Dict[str, Any]],
    *,
    available_handlers: Mapping[str, Callable[..., Any]],
) -> Dict[str, Any]:
    total = len(list(skills or []))
    executable = len([skill for skill in skills if skill.get("executable")])
    deprecated = len([skill for skill in skills if skill.get("status") == "deprecated"])
    registered_only = len([skill for skill in skills if skill.get("status") == "registered"])
    execution_gap = [
        {
            "id": skill.get("id"),
            "name": skill.get("name"),
            "status": skill.get("status"),
            "execution_mode": skill.get("execution_mode"),
        }
        for skill in skills
        if not skill.get("executable") and skill.get("status") != "deprecated"
    ][:20]
    audit = _load_skill_coverage_audit() or {}
    capability_audit = _load_skill_capability_audit() or {}
    tool_reference_coverage = dict(audit.get("coverage") or {})
    tool_reference_coverage.update(
        {
            "tool_count": int(audit.get("tool_count") or 0),
            "runtime_tool_count": int(audit.get("runtime_tool_count") or audit.get("tool_count") or 0),
            "skills_count": int(audit.get("skills_count") or 0),
            "generated_at": audit.get("generated_at"),
            "tool_coverage_source": audit.get("tool_coverage_source"),
        }
    )
    executors = dict(audit.get("executors") or {})
    repo_local_skills = [skill for skill in skills if skill.get("repo_local")]
    repo_local_skill_ids = sorted(str(skill.get("id") or "") for skill in repo_local_skills if skill.get("id"))
    runtime_contract_count = len([skill_id for skill_id in repo_local_skill_ids if skill_id in _SKILL_CONTRACTS])
    runtime_executor_count = len([skill_id for skill_id in repo_local_skill_ids if skill_id in available_handlers])
    capability_tier_breakdown: Dict[str, int] = {}
    role_tag_breakdown: Dict[str, int] = {}
    for skill in repo_local_skills:
        tier = str(skill.get("capability_tier") or "unspecified").strip() or "unspecified"
        capability_tier_breakdown[tier] = capability_tier_breakdown.get(tier, 0) + 1
        for role in list(skill.get("role_tags") or []):
            role_name = str(role or "").strip()
            if not role_name:
                continue
            role_tag_breakdown[role_name] = role_tag_breakdown.get(role_name, 0) + 1

    meta_conflicts = list(capability_audit.get("meta_conflicts") or [])
    stale_meta_detected = bool(capability_audit.get("stale_meta_detected"))
    if not capability_audit:
        if runtime_contract_count != len(repo_local_skill_ids):
            meta_conflicts.append(
                {
                    "type": "repo_local_contract_mismatch",
                    "actual": len(repo_local_skill_ids),
                    "runtime_contract_count": runtime_contract_count,
                }
            )
        if runtime_executor_count != len(repo_local_skill_ids):
            meta_conflicts.append(
                {
                    "type": "repo_local_executor_mismatch",
                    "actual": len(repo_local_skill_ids),
                    "runtime_executor_count": runtime_executor_count,
                }
            )
        stale_meta_detected = bool(meta_conflicts)
    return {
        "total_count": total,
        "executable_count": executable,
        "registered_only_count": registered_only,
        "deprecated_count": deprecated,
        "executor_coverage_ratio": round(executable / total, 4) if total else 0.0,
        "executable_skill_ids": [skill.get("id") for skill in skills if skill.get("executable")],
        "execution_gap": execution_gap,
        "repo_local_skill_count": len(repo_local_skill_ids),
        "repo_local_skill_ids": repo_local_skill_ids,
        "runtime_contract_count": runtime_contract_count,
        "runtime_executor_count": runtime_executor_count,
        "stale_meta_detected": stale_meta_detected,
        "meta_conflicts": meta_conflicts,
        "capability_tier_breakdown": capability_tier_breakdown,
        "role_tag_breakdown": role_tag_breakdown,
        "available_handlers": sorted(available_handlers.keys()),
        "tool_reference_coverage": tool_reference_coverage or None,
        "executor_audit": executors or None,
        "capability_audit": capability_audit or None,
    }


def _skills_source(skills: List[Dict[str, Any]]) -> str:
    return "codex_registry" if skills and any(skill.get("path") for skill in skills) else "fallback_demo"
