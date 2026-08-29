"""外部 AI 策略生成 provider。"""



from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pandas as pd
from strategy_factory.api.semantic_contract import resolve_candidate_validation_profile
from strategy_factory.api.semantic_contract import validate_precompile_candidate_contract
from strategy_factory.api.semantic_contract import synthesize_confidence_contract
from strategy_factory.api.semantic_contract import ensure_candidate_semantic_contract
from strategy_factory.api.semantic_contract import (
    apply_target_symbol_policy,
    build_target_alignment_contract,
    normalize_research_task_contract,
)

from ..env_loader import load_mcp_env
from .strategy_open_dsl import compile_open_dsl_candidate, is_open_dsl_candidate
from .strategy_spec import (
    _default_holding_horizon,
    _default_position_sizing,
    _default_rebalance_rule,
    _default_risk_rules,
    _default_targeting_policy,
    _default_trade_plan,
)


class StrategyLLMRequestError(RuntimeError):
    def __init__(self, message: str, *, metrics: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


@dataclass
class StrategyLLMConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: float = 30.0
    connect_timeout_sec: float = 8.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    temperature: float = 0.3
    max_tokens: int = 900
    retry_count: int = 2
    retry_backoff_sec: float = 1.0
    initial_compact_level: int = 0
    # 与 public/runtime 口径对齐:单次抖动不应把整轮 LLM 锁死。
    recent_timeout_minimal_streak: int = 3
    recent_timeout_cooldown_sec: float = 120.0
    max_concurrency: int = 3
    strict: bool = False

    @classmethod
    def from_env(cls) -> "StrategyLLMConfig":
        load_mcp_env(override=False, only_prefixes=('STRATEGY_LLM_',))
        enabled = str(os.getenv("STRATEGY_LLM_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
        timeout_sec = float(os.getenv("STRATEGY_LLM_TIMEOUT_SEC", "30") or 30)
        initial_compact_level = max(0, min(2, int(os.getenv("STRATEGY_LLM_INITIAL_COMPACT_LEVEL", "0") or 0)))
        recent_timeout_minimal_streak = max(1, min(8, int(os.getenv("STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK", "3") or 3)))
        recent_timeout_cooldown_sec = max(0.0, float(os.getenv("STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC", "120") or 120))
        return cls(
            enabled=enabled,
            provider=str(os.getenv("STRATEGY_LLM_PROVIDER", "openai_compatible") or "openai_compatible"),
            base_url=str(os.getenv("STRATEGY_LLM_BASE_URL", "") or "").strip(),
            api_key=str(os.getenv("STRATEGY_LLM_API_KEY", "") or "").strip(),
            model=str(os.getenv("STRATEGY_LLM_MODEL", "") or "").strip(),
            timeout_sec=timeout_sec,
            connect_timeout_sec=float(os.getenv("STRATEGY_LLM_CONNECT_TIMEOUT_SEC", str(min(timeout_sec, 8.0))) or min(timeout_sec, 8.0)),
            write_timeout_sec=float(os.getenv("STRATEGY_LLM_WRITE_TIMEOUT_SEC", str(min(timeout_sec, 10.0))) or min(timeout_sec, 10.0)),
            pool_timeout_sec=float(os.getenv("STRATEGY_LLM_POOL_TIMEOUT_SEC", str(min(timeout_sec, 5.0))) or min(timeout_sec, 5.0)),
            temperature=float(os.getenv("STRATEGY_LLM_TEMPERATURE", "0.3") or 0.3),
            max_tokens=max(128, int(os.getenv("STRATEGY_LLM_MAX_TOKENS", "900") or 900)),
            retry_count=max(0, int(os.getenv("STRATEGY_LLM_RETRY_COUNT", "2") or 2)),
            retry_backoff_sec=max(0.0, float(os.getenv("STRATEGY_LLM_RETRY_BACKOFF_SEC", "1.0") or 1.0)),
            initial_compact_level=initial_compact_level,
            recent_timeout_minimal_streak=recent_timeout_minimal_streak,
            recent_timeout_cooldown_sec=recent_timeout_cooldown_sec,
            max_concurrency=max(1, min(16, int(os.getenv("STRATEGY_LLM_MAX_CONCURRENCY", "3") or 3))),
            strict=str(os.getenv("STRATEGY_LLM_STRICT_MODE", "")).strip().lower() in {"1", "true", "yes", "on"},
        )

class _StrategyLLMProviderNormalizeMixin:
        _LEGACY_PORTFOLIO_REQUIRED_KEYS = ("position_assumption", "target_weight_scheme")
        _LEGACY_EXECUTION_REQUIRED_KEYS = ("commission_rate", "slippage_bps", "tradability_filter", "slippage_model")
        _LEGACY_VALIDATION_REQUIRED_KEYS = ("profile", "validation_focus", "primary_validation_layer")

        @staticmethod
        def _openai_compatible_api_base(base_url: str) -> str:
            base = str(base_url or "").rstrip("/")
            if not base:
                return base
            lowered = base.lower()
            if lowered.endswith(("/chat/completions", "/responses", "/messages")):
                return base
            remainder = base.split("://", 1)[1] if "://" in base else base
            path = remainder.split("/", 1)[1] if "/" in remainder else ""
            if not path:
                return f"{base}/v1"
            return base

        def _endpoint(self) -> str:
            base = self.config.base_url.rstrip("/")
            lowered = base.lower()
            if lowered.endswith("/chat/completions"):
                return base
            if lowered.endswith("/responses"):
                return base
            if lowered.endswith("/messages"):
                return base
            api_base = self._openai_compatible_api_base(base)
            provider = str(getattr(self.config, "provider", "") or "").strip().lower()
            if provider == "openai_responses":
                return f"{api_base}/responses"
            if provider in {"anthropic_messages", "anthropic"}:
                return f"{api_base}/messages"
            return f"{api_base}/chat/completions"

        def _adapt_payload_for_endpoint(self, payload: dict) -> dict:
            """Transform chat-completions payloads for provider-specific APIs."""
            provider = str(getattr(self.config, "provider", "") or "").strip().lower()
            if provider in {"anthropic_messages", "anthropic"}:
                messages = payload.get("messages") or []
                system_parts: list[str] = []
                adapted_messages: list[dict[str, Any]] = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    role = str(msg.get("role") or "user").strip().lower()
                    content = msg.get("content", "")
                    if role == "system":
                        if content not in (None, ""):
                            system_parts.append(str(content))
                        continue
                    if role not in {"user", "assistant"}:
                        role = "user"
                    adapted_messages.append({"role": role, "content": content if content is not None else ""})
                if not adapted_messages:
                    adapted_messages.append({"role": "user", "content": ""})
                raw_max_tokens = payload.get("max_tokens") or payload.get("max_output_tokens") or self.config.max_tokens or 1024
                try:
                    max_tokens = max(1, int(raw_max_tokens))
                except Exception:
                    max_tokens = 1024
                adapted = {
                    "model": payload.get("model", self.config.model),
                    "messages": adapted_messages,
                    "max_tokens": max_tokens,
                }
                if system_parts:
                    adapted["system"] = "\n\n".join(system_parts)
                if payload.get("temperature") is not None:
                    adapted["temperature"] = payload["temperature"]
                return adapted
            if provider != "openai_responses":
                return payload
            # Convert chat completions format → Responses API format
            messages = payload.get("messages") or []
            # Build input from messages
            input_parts = []
            instructions = None
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    instructions = content
                else:
                    input_parts.append({"role": role, "content": content})
            # Use last user message as input if simple
            if len(input_parts) == 1 and input_parts[0]["role"] == "user":
                input_value = input_parts[0]["content"]
            else:
                input_value = input_parts
            adapted = {
                "model": payload.get("model", self.config.model),
                "input": input_value,
                "store": False,
            }
            if instructions:
                adapted["instructions"] = instructions
            if payload.get("max_tokens"):
                adapted["max_output_tokens"] = payload["max_tokens"]
            if payload.get("temperature") is not None:
                adapted["temperature"] = payload["temperature"]
            return adapted

        @staticmethod
        def _error_text(exc: Exception) -> str:
            text = str(exc or "").strip()
            return text or exc.__class__.__name__

        @staticmethod
        def _round_number(value: Any, digits: int = 6) -> Any:
            try:
                if isinstance(value, bool):
                    return value
                if isinstance(value, int):
                    return value
                if isinstance(value, float):
                    return round(value, digits)
            except Exception:
                return value
            return value

        @staticmethod
        def _compact_text(value: Any, limit: int = 120) -> str:
            text = str(value or '').strip()
            if len(text) <= limit:
                return text
            return text[: max(0, limit - 1)] + '...'

        @staticmethod
        def _extract_json_text(text: str) -> str:
            raw = str(text or "").strip()
            if not raw:
                return raw

            def balanced_slice(value: str, start: int) -> str:
                if start < 0 or start >= len(value) or value[start] not in "{[":
                    return ""
                stack: list[str] = []
                in_string = False
                escaped = False
                pairs = {"{": "}", "[": "]"}
                for index in range(start, len(value)):
                    char = value[index]
                    if in_string:
                        if escaped:
                            escaped = False
                        elif char == "\\":
                            escaped = True
                        elif char == '"':
                            in_string = False
                        continue
                    if char == '"':
                        in_string = True
                        continue
                    if char in pairs:
                        stack.append(pairs[char])
                        continue
                    if char in "}]":
                        if not stack or char != stack[-1]:
                            return ""
                        stack.pop()
                        if not stack:
                            return value[start : index + 1].strip()
                return ""

            def first_balanced_json(value: str) -> str:
                for index, char in enumerate(value):
                    if char in "{[":
                        candidate = balanced_slice(value, index)
                        if candidate:
                            return candidate
                return ""

            fence_blocks = re.findall(r"```[^\n`]*\n?(.*?)```", raw, flags=re.S)
            for block in fence_blocks:
                candidate = first_balanced_json(str(block or "").strip())
                if candidate:
                    return candidate
            if raw.startswith("{") or raw.startswith("["):
                candidate = balanced_slice(raw, 0)
                if candidate:
                    return candidate
                return raw
            candidate = first_balanced_json(raw)
            if candidate:
                return candidate
            return raw

        @staticmethod
        def _extract_content(payload: dict[str, Any]) -> str:
            anthropic_content = payload.get("content")
            if isinstance(anthropic_content, list):
                parts = []
                for item in anthropic_content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if text:
                            parts.append(str(text))
                    elif item not in (None, ""):
                        parts.append(str(item))
                if parts:
                    return "\n".join(parts)
            if isinstance(anthropic_content, str) and anthropic_content:
                return anthropic_content
            choices = list(payload.get("choices") or [])
            if choices:
                first_choice = dict(choices[0] or {})
                message = dict(first_choice.get("message") or {})
                content = message.get("content")
                if isinstance(content, list):
                    return "\n".join(str(item.get("text") or item) for item in content)
                if content not in (None, ""):
                    return str(content)
                message_text = message.get("text")
                if message_text not in (None, ""):
                    return str(message_text)
                choice_text = first_choice.get("text")
                if choice_text not in (None, ""):
                    return str(choice_text)
                delta = dict(first_choice.get("delta") or {})
                delta_content = delta.get("content")
                if isinstance(delta_content, list):
                    return "\n".join(str(item.get("text") or item) for item in delta_content)
                if delta_content not in (None, ""):
                    return str(delta_content)
                # Fallback: DeepSeek-V4-pro / R1 style returns reasoning_content when content is empty
                # (2026-05-28: official DeepSeek API returns msg.reasoning_content for v4-pro)
                reasoning_content = message.get("reasoning_content")
                if isinstance(reasoning_content, list):
                    return "\n".join(str(item.get("text") or item) for item in reasoning_content)
                if reasoning_content not in (None, ""):
                    return str(reasoning_content)
            output = list(payload.get("output") or [])
            parts = []
            for item in output:
                for content in list((item or {}).get("content") or []):
                    text = content.get("text") if isinstance(content, dict) else None
                    if text:
                        parts.append(str(text))
            if parts:
                return "\n".join(parts)
            output_text = payload.get("output_text")
            if output_text not in (None, ""):
                return str(output_text)
            return ""

        @staticmethod
        def _normalize_analysis(analysis: Any) -> dict[str, Any]:
            if not isinstance(analysis, dict):
                return {}
            normalized: dict[str, Any] = {}
            for key in ("market_regime", "style_bias", "hypothesis", "selection_notes", "risk_focus", "evidence", "universe_view", "selection_plan", "trade_plan"):
                value = analysis.get(key)
                if value is None:
                    continue
                if isinstance(value, list):
                    normalized[key] = [str(item) for item in value[:6]]
                elif isinstance(value, dict):
                    normalized[key] = value
                else:
                    normalized[key] = str(value)
            for key, value in analysis.items():
                if key not in normalized and len(normalized) < 10:
                    normalized[key] = value
            return normalized

        @staticmethod
        def _normalize_code_list(values: Any, limit: int = 12) -> list[str]:
            codes: list[str] = []
            seen: set[str] = set()

            def visit(value: Any):
                if value is None:
                    return
                if isinstance(value, dict):
                    for key in ('code', 'symbol', 'stock_code'):
                        if value.get(key) is not None:
                            visit(value.get(key))
                    for key in ('codes', 'symbols', 'stock_codes', 'target_symbols'):
                        if value.get(key) is not None:
                            visit(value.get(key))
                    return
                if isinstance(value, (list, tuple, set)):
                    for item in value:
                        visit(item)
                    return
                raw = str(value or '').strip()
                if not raw:
                    return
                if any(sep in raw for sep in [',', ';', '|', '\n', '\t', ' ']):
                    normalized = raw.replace(';', ',').replace('|', ',').replace('\n', ',').replace('\t', ',').replace(' ', ',')
                    for part in normalized.split(','):
                        visit(part)
                    return
                code = raw.split('.')[0].strip()
                if not code or code in seen:
                    return
                seen.add(code)
                codes.append(code)

            visit(values)
            return codes[: max(1, min(int(limit or 12), 40))]

        @staticmethod
        def _contract_value_missing(value: Any) -> bool:
            return value in (None, "", [], {})

        @classmethod
        def _has_required_contract_keys(
            cls,
            payload: Any,
            *,
            required_keys: tuple[str, ...],
        ) -> bool:
            if not isinstance(payload, dict) or not payload:
                return False
            return all(not cls._contract_value_missing(payload.get(key)) for key in required_keys)

        @classmethod
        def _canonicalize_validation_profile(
            cls,
            candidate: dict[str, Any],
            *,
            strategy_type: str,
            normalized_task: dict[str, Any],
            validation_profile: Any,
        ) -> dict[str, Any]:
            explicit_profile = dict(validation_profile or {}) if isinstance(validation_profile, dict) else {}
            canonical_profile = resolve_candidate_validation_profile(
                {
                    "strategy_type": strategy_type,
                    "research_task": dict(normalized_task),
                },
                research_task=normalized_task,
            )
            if not cls._has_required_contract_keys(
                explicit_profile,
                required_keys=cls._LEGACY_VALIDATION_REQUIRED_KEYS,
            ):
                return dict(canonical_profile)
            actual_profile = str(explicit_profile.get("profile") or "").strip().lower()
            actual_focus = str(explicit_profile.get("validation_focus") or "").strip().lower()
            actual_layer = str(explicit_profile.get("primary_validation_layer") or "").strip().lower()
            expected_profile = str(canonical_profile.get("profile") or "").strip().lower()
            expected_focus = str(canonical_profile.get("validation_focus") or "").strip().lower()
            expected_layer = str(canonical_profile.get("primary_validation_layer") or "").strip().lower()
            if (
                actual_profile == expected_profile
                and actual_focus == expected_focus
                and actual_layer == expected_layer
            ):
                return explicit_profile
            return {
                **explicit_profile,
                **canonical_profile,
            }

        @classmethod
        def _merge_precision_preferences(
            cls,
            validation_profile: dict[str, Any],
            *,
            candidate: dict[str, Any],
            normalized_task: dict[str, Any],
        ) -> dict[str, Any]:
            merged = dict(validation_profile or {})
            hypothesis_artifact = dict(candidate.get("hypothesis_artifact") or candidate.get("hypothesis_structured") or {})
            explicit_profile = dict(candidate.get("validation_profile") or {})
            for field_name in (
                "objective_profile",
                "trade_density_preference",
                "entry_selectivity",
                "regime_required",
                "cost_robust_required",
            ):
                for value in (
                    explicit_profile.get(field_name),
                    hypothesis_artifact.get(field_name),
                    normalized_task.get(field_name),
                ):
                    if value not in (None, "", [], {}):
                        merged[field_name] = value
                        break
            return merged

        @staticmethod
        def _normalize_targeting_policy_payload(
            payload: Any,
            *,
            fallback: dict[str, Any],
        ) -> dict[str, Any]:
            if isinstance(payload, dict) and payload:
                return dict(payload)
            if payload not in (None, "", [], {}):
                return {"mode": str(payload).strip()}
            return dict(fallback)

        @classmethod
        def _normalize_open_dsl_candidate_contract(
            cls,
            candidate: dict[str, Any],
            *,
            normalized_task: dict[str, Any],
        ) -> Optional[dict[str, Any]]:
            if not is_open_dsl_candidate(candidate):
                return None
            compilation = compile_open_dsl_candidate(candidate)
            if compilation.attempted and not compilation.accepted:
                candidate["_open_dsl_reject_reasons"] = list(compilation.reject_reasons)
                candidate["_open_dsl_audit"] = dict(compilation.audit or {})
                return None
            compiled = dict(compilation.compiled or {})
            params = dict(compiled.get("params") or {})
            compiled_dsl = dict(params.get("dsl") or {})
            if not compiled_dsl:
                candidate["_open_dsl_reject_reasons"] = ["open_dsl_missing:compiled_dsl"]
                candidate["_open_dsl_audit"] = dict(compilation.audit or {})
                return None
            compiled_meta = dict(compiled.get("metadata") or {})
            target_symbols = cls._normalize_code_list(
                [
                    candidate.get("target_symbols"),
                    candidate.get("stock_pool"),
                    compiled_meta.get("target_symbols"),
                    (compiled_dsl.get("metadata") or {}).get("target_symbols"),
                ],
                limit=8,
            )
            stock_pool_payload = candidate.get("stock_pool")
            stock_pool = {
                "selection_mode": "explicit" if target_symbols else "screened",
                "symbols": list(target_symbols),
            }
            if isinstance(stock_pool_payload, dict):
                stock_pool = {
                    "selection_mode": str(
                        stock_pool_payload.get("selection_mode")
                        or stock_pool_payload.get("mode")
                        or stock_pool.get("selection_mode")
                        or ""
                    ).strip()
                    or stock_pool.get("selection_mode")
                    or "screened",
                    "symbols": cls._normalize_code_list(
                        stock_pool_payload.get("symbols")
                        or stock_pool_payload.get("codes")
                        or target_symbols,
                        limit=8,
                    ),
                    "filters": dict(stock_pool_payload.get("filters") or {}),
                    "rationale": stock_pool_payload.get("rationale"),
                }
            strategy_type = str(candidate.get("strategy_type") or compiled.get("strategy_type") or "dsl_rule").strip() or "dsl_rule"
            holding_horizon = dict(candidate.get("holding_horizon") or compiled_meta.get("holding_horizon") or {})
            if not holding_horizon:
                holding_horizon = _default_holding_horizon(strategy_type, normalized_task, str(normalized_task.get("task_source") or ""))
            trade_plan = candidate.get("trade_plan")
            normalized_trade_plan = dict(trade_plan) if isinstance(trade_plan, dict) and trade_plan else _default_trade_plan(strategy_type, str(normalized_task.get("task_source") or ""))
            risk_rules = dict(candidate.get("risk_rules") or compiled_meta.get("risk_rules") or compiled_dsl.get("risk_rules") or {})
            if not risk_rules:
                risk_rules = _default_risk_rules(str(normalized_task.get("task_source") or ""), holding_horizon)
            position_sizing = candidate.get("position_sizing")
            normalized_position_sizing = dict(position_sizing) if isinstance(position_sizing, dict) and position_sizing else _default_position_sizing(target_symbols)
            rebalance_rule = candidate.get("rebalance_rule")
            normalized_rebalance_rule = dict(rebalance_rule) if isinstance(rebalance_rule, dict) and rebalance_rule else _default_rebalance_rule(strategy_type, str(normalized_task.get("task_source") or ""))
            portfolio_spec = dict(candidate.get("portfolio_spec") or {})
            if not cls._has_required_contract_keys(portfolio_spec, required_keys=cls._LEGACY_PORTFOLIO_REQUIRED_KEYS):
                portfolio_spec = {
                    "position_assumption": "single_name_full_notional" if len(target_symbols) <= 1 else "equal_weight_proxy",
                    "target_weight_scheme": "single_name" if len(target_symbols) <= 1 else "equal_weight",
                }
            execution_assumptions = dict(candidate.get("execution_assumptions") or {})
            if not cls._has_required_contract_keys(execution_assumptions, required_keys=cls._LEGACY_EXECUTION_REQUIRED_KEYS):
                execution_assumptions = {
                    "commission_rate": 0.00025,
                    "slippage_bps": 5,
                    "tradability_filter": True,
                    "slippage_model": "fixed",
                }
            validation_profile = cls._canonicalize_validation_profile(
                candidate,
                strategy_type=strategy_type,
                normalized_task=normalized_task,
                validation_profile=candidate.get("validation_profile"),
            )
            validation_profile = cls._merge_precision_preferences(
                validation_profile,
                candidate=candidate,
                normalized_task=normalized_task,
            )
            dsl_metadata = dict(compiled_dsl.get("metadata") or {})
            dsl_metadata.update(
                {
                    "target_symbols": list(target_symbols),
                    "stock_pool": dict(stock_pool),
                    "portfolio_spec": dict(portfolio_spec),
                    "execution_assumptions": dict(execution_assumptions),
                    "validation_profile": dict(validation_profile),
                    "open_dsl_audit": dict(compilation.audit or {}),
                    "open_dsl_source_mode": "provider_normalize_bridge",
                }
            )
            compiled_dsl["metadata"] = dsl_metadata
            return ensure_candidate_semantic_contract({
                **dict(candidate or {}),
                "strategy_type": strategy_type,
                "dsl": compiled_dsl,
                "target_symbols": list(target_symbols),
                "stock_pool": dict(stock_pool),
                "holding_horizon": dict(holding_horizon),
                "trade_plan": dict(normalized_trade_plan),
                "risk_rules": dict(risk_rules),
                "position_sizing": dict(normalized_position_sizing),
                "execution_notes": candidate.get("execution_notes") or compiled_meta.get("execution_notes") or "prefer liquid session execution with tradability filter",
                "rebalance_rule": dict(normalized_rebalance_rule),
                "portfolio_spec": dict(portfolio_spec),
                "execution_assumptions": dict(execution_assumptions),
                "validation_profile": dict(validation_profile),
                "_open_dsl_audit": dict(compilation.audit or {}),
                "_open_dsl_compiled": True,
            })

        @classmethod
        def _require_explicit_contract_dict(
            cls,
            candidate: dict[str, Any],
            field: str,
            *,
            required_keys: tuple[str, ...],
        ) -> tuple[dict[str, Any], list[str]]:
            payload = candidate.get(field)
            if not isinstance(payload, dict) or not payload:
                return {}, [f"{field}_missing"]
            normalized = dict(payload)
            missing_keys = [
                key
                for key in required_keys
                if cls._contract_value_missing(normalized.get(key))
            ]
            if missing_keys:
                return normalized, [f"{field}_missing_keys:{','.join(missing_keys)}"]
            return normalized, []
        @classmethod
        def _normalize_candidate_payload(
            cls,
            candidate: Any,
            research_task: Optional[dict[str, Any]] = None,
            *,
            allow_legacy_contract_defaults: bool = False,
        ) -> Optional[dict[str, Any]]:
            if not isinstance(candidate, dict):
                return None
            normalized_task = normalize_research_task_contract(research_task)
            open_dsl_normalized = cls._normalize_open_dsl_candidate_contract(
                candidate,
                normalized_task=normalized_task,
            )
            if open_dsl_normalized is not None:
                candidate = open_dsl_normalized
            task_source = str(normalized_task.get('task_source') or '').strip().lower()
            research_symbols = cls._normalize_code_list(normalized_task.get('target_symbols'), limit=8)
            target_alignment_contract = dict(
                build_target_alignment_contract(normalized_task, candidate=candidate) or {}
            )
            dsl_payload = candidate.get('dsl')
            if not isinstance(dsl_payload, dict):
                dsl_payload = {
                    'version': candidate.get('version'),
                    'timeframe': candidate.get('timeframe'),
                    'entry': candidate.get('entry'),
                    'exit': candidate.get('exit'),
                    'metadata': candidate.get('metadata') or {},
                    'risk_rules': candidate.get('risk_rules') or {},
                }
            if not isinstance(dsl_payload, dict) or not dsl_payload.get('entry'):
                return None
            dsl = dict(dsl_payload)
            metadata = dict(dsl.get('metadata') or {})
            raw_target_symbols = cls._normalize_code_list([
                candidate.get('target_symbols'),
                candidate.get('stock_pool'),
                metadata.get('target_symbols'),
                metadata.get('stock_pool'),
            ], limit=8)
            stock_pool_payload = candidate.get('stock_pool')
            fallback_symbols = cls._normalize_code_list([
                stock_pool_payload,
                metadata.get('stock_pool'),
                research_symbols,
            ], limit=8)
            policy_result = apply_target_symbol_policy(
                raw_target_symbols,
                normalized_task,
                fallback_symbols=fallback_symbols,
                limit=8,
            )
            target_symbols = list(policy_result.get('target_symbols') or [])
            max_candidate_target_symbols = int(target_alignment_contract.get('max_candidate_target_symbols') or 0)
            if max_candidate_target_symbols > 0 and len(target_symbols) > max_candidate_target_symbols:
                target_symbols = list(target_symbols[:max_candidate_target_symbols])
            stock_pool_payload = candidate.get('stock_pool')
            stock_pool_symbols = cls._normalize_code_list([
                stock_pool_payload,
                metadata.get('stock_pool'),
                target_symbols,
            ], limit=8)
            stock_pool: dict[str, Any] = {
                'selection_mode': 'explicit' if stock_pool_symbols else 'screened',
                'symbols': stock_pool_symbols,
            }
            if isinstance(stock_pool_payload, dict):
                selection_mode = str(stock_pool_payload.get('selection_mode') or stock_pool_payload.get('mode') or stock_pool.get('selection_mode') or '').strip()
                if selection_mode:
                    stock_pool['selection_mode'] = selection_mode
                filters = dict(stock_pool_payload.get('filters') or {})
                if filters:
                    stock_pool['filters'] = filters
                rationale = stock_pool_payload.get('rationale')
                if rationale not in (None, ''):
                    stock_pool['rationale'] = str(rationale)
            constraint_check = dict(policy_result.get('constraint_check') or {})
            if (
                constraint_check.get('expansion_applied')
                and stock_pool_symbols
                and not str(stock_pool.get('rationale') or '').strip()
            ):
                stock_pool['rationale'] = str(
                    constraint_check.get('expansion_reason')
                    or constraint_check.get('constraint_violation')
                    or 'expanded_from_candidate_universe'
                )
            overlap_count = len(set(target_symbols).intersection(research_symbols))
            coverage_ratio = round(overlap_count / max(1, len(target_symbols)), 4) if target_symbols else 0.0
            intersection_ratio = (
                round(overlap_count / max(1, len(research_symbols)), 4)
                if research_symbols
                else None
            )
            min_coverage_ratio = float(target_alignment_contract.get('min_coverage_ratio') or 0.0)
            min_intersection_ratio = float(target_alignment_contract.get('min_intersection_ratio') or 0.0)
            min_required_overlap_count = int(target_alignment_contract.get('min_required_overlap_count') or 0)
            alignment_reject_reasons: list[str] = []
            if target_alignment_contract.get('quality_gate_enabled'):
                if not target_symbols and research_symbols:
                    alignment_reject_reasons.append('empty_target_symbols_after_alignment')
                if coverage_ratio < min_coverage_ratio:
                    alignment_reject_reasons.append('coverage_ratio_below_contract')
                if intersection_ratio is not None and intersection_ratio < min_intersection_ratio:
                    alignment_reject_reasons.append('intersection_ratio_below_contract')
                if min_required_overlap_count > 0 and overlap_count < min_required_overlap_count:
                    alignment_reject_reasons.append('target_overlap_count_below_contract')
            constraint_check['target_symbols_after_normalize'] = list(target_symbols)
            constraint_check['coverage_ratio'] = coverage_ratio
            constraint_check['intersection_ratio'] = intersection_ratio
            constraint_check['target_overlap_count'] = int(overlap_count)
            strategy_type = str(candidate.get('strategy_type') or 'dsl_rule').strip() or 'dsl_rule'
            holding_horizon = dict(candidate.get('holding_horizon') or {})
            if not holding_horizon:
                holding_horizon = dict(normalized_task.get('holding_window') or {})
            if not holding_horizon:
                holding_horizon = _default_holding_horizon(strategy_type, normalized_task, task_source)
            risk_rules = dict(candidate.get('risk_rules') or dsl.get('risk_rules') or {})
            if not risk_rules:
                risk_rules = _default_risk_rules(task_source, holding_horizon)
            rebalance_rule = dict(candidate.get('rebalance_rule') or {})
            if not rebalance_rule:
                rebalance_rule = _default_rebalance_rule(strategy_type, task_source)
            trade_plan = candidate.get('trade_plan')
            if isinstance(trade_plan, dict):
                normalized_trade_plan = dict(trade_plan)
            elif trade_plan not in (None, '', [], {}):
                normalized_trade_plan = {'summary': str(trade_plan)}
            else:
                normalized_trade_plan = _default_trade_plan(strategy_type, task_source)
            position_sizing = candidate.get('position_sizing')
            if isinstance(position_sizing, dict):
                normalized_position_sizing = dict(position_sizing)
            elif position_sizing not in (None, '', [], {}):
                normalized_position_sizing = {'summary': str(position_sizing)}
            else:
                normalized_position_sizing = _default_position_sizing(target_symbols)
            if allow_legacy_contract_defaults:
                portfolio_spec = dict(candidate.get('portfolio_spec') or {})
                if not cls._has_required_contract_keys(portfolio_spec, required_keys=cls._LEGACY_PORTFOLIO_REQUIRED_KEYS):
                    portfolio_spec = {
                        'position_assumption': 'single_name_full_notional' if len(target_symbols) <= 1 else 'equal_weight_proxy',
                        'target_weight_scheme': 'single_name' if len(target_symbols) <= 1 else 'equal_weight',
                    }
                execution_assumptions = dict(candidate.get('execution_assumptions') or {})
                if not cls._has_required_contract_keys(execution_assumptions, required_keys=cls._LEGACY_EXECUTION_REQUIRED_KEYS):
                    execution_assumptions = {
                        'commission_rate': 0.00025,
                        'slippage_bps': 5,
                        'tradability_filter': True,
                        'slippage_model': 'fixed',
                    }
                validation_profile = cls._canonicalize_validation_profile(
                    candidate,
                    strategy_type=strategy_type,
                    normalized_task=normalized_task,
                    validation_profile=candidate.get('validation_profile'),
                )
                validation_profile = cls._merge_precision_preferences(
                    validation_profile,
                    candidate=candidate,
                    normalized_task=normalized_task,
                )
            else:
                portfolio_spec, portfolio_reject_reasons = cls._require_explicit_contract_dict(
                    candidate,
                    'portfolio_spec',
                    required_keys=('position_assumption', 'target_weight_scheme'),
                )
                execution_assumptions, execution_reject_reasons = cls._require_explicit_contract_dict(
                    candidate,
                    'execution_assumptions',
                    required_keys=('commission_rate', 'slippage_bps', 'tradability_filter', 'slippage_model'),
                )
                validation_profile, validation_reject_reasons = cls._require_explicit_contract_dict(
                    candidate,
                    'validation_profile',
                    required_keys=('profile', 'validation_focus', 'primary_validation_layer'),
                )
                explicit_contract_reject_reasons = [
                    *portfolio_reject_reasons,
                    *execution_reject_reasons,
                    *validation_reject_reasons,
                ]
                if explicit_contract_reject_reasons:
                    candidate["_normalize_reject_reasons"] = list(dict.fromkeys(explicit_contract_reject_reasons))
                    return None
            validation = validate_precompile_candidate_contract(
                {
                    **candidate,
                    'research_task': dict(normalized_task),
                    'strategy_type': strategy_type,
                    'target_symbols': list(target_symbols),
                    'stock_pool': dict(stock_pool),
                    'constraint_check': dict(constraint_check),
                    'holding_horizon': dict(holding_horizon),
                    'trade_plan': dict(normalized_trade_plan),
                    'risk_rules': dict(risk_rules),
                    'position_sizing': dict(normalized_position_sizing),
                    'rebalance_rule': dict(rebalance_rule),
                    'portfolio_spec': dict(portfolio_spec),
                    'execution_assumptions': dict(execution_assumptions),
                    'validation_profile': dict(validation_profile),
                },
                research_task=normalized_task,
                source='external_llm',
            )
            constraint_check = dict(validation.constraint_check)
            if not validation.accepted:
                candidate["_normalize_reject_reasons"] = list(validation.reject_reasons)
                candidate["_normalize_precompile_validation"] = validation.to_dict()
                return None
            portfolio_spec = dict(validation.portfolio_spec or portfolio_spec)
            execution_assumptions = dict(validation.execution_assumptions or execution_assumptions)
            validation_profile = dict(validation.validation_profile or validation_profile)
            metadata['target_symbols'] = list(target_symbols)
            metadata['stock_pool'] = stock_pool
            metadata['constraint_check'] = constraint_check
            targeting_policy = cls._normalize_targeting_policy_payload(
                metadata.get('targeting_policy'),
                fallback=_default_targeting_policy(normalized_task),
            )
            metadata['targeting_policy'] = targeting_policy
            metadata['target_alignment_contract'] = dict(target_alignment_contract)
            dsl['metadata'] = metadata
            dsl = cls._sanitize_dsl_for_candidate(dsl)
            tags = candidate.get('tags') or []
            if not isinstance(tags, list):
                tags = [tags]
            hypothesis = str(
                candidate.get('hypothesis')
                or candidate.get('rationale')
                or candidate.get('description')
                or ''
            ).strip()
            dsl['risk_rules'] = dict(risk_rules)
            execution_notes = candidate.get('execution_notes')
            if execution_notes in (None, '', [], {}):
                normalized_execution_notes = 'prefer liquid session execution with tradability filter'
            elif isinstance(execution_notes, list):
                normalized_execution_notes = [str(item) for item in execution_notes[:3] if str(item or '').strip()]
            else:
                normalized_execution_notes = str(execution_notes)
            metadata['portfolio_spec'] = dict(portfolio_spec)
            metadata['execution_assumptions'] = dict(execution_assumptions)
            metadata['validation_profile'] = dict(validation_profile)
            metadata['targeting_policy'] = dict(targeting_policy)
            metadata['constraint_check'] = dict(constraint_check)
            dsl['metadata'] = metadata
            normalized_params = {
                'dsl': dict(dsl),
                'target_symbols': list(target_symbols),
                'stock_pool': dict(stock_pool),
                'research_task': dict(normalized_task),
                'holding_horizon': dict(holding_horizon),
                'trade_plan': dict(normalized_trade_plan),
                'risk_rules': dict(risk_rules),
                'position_sizing': dict(normalized_position_sizing),
                'execution_notes': normalized_execution_notes,
                'rebalance_rule': dict(rebalance_rule),
                'portfolio_spec': dict(portfolio_spec),
                'execution_assumptions': dict(execution_assumptions),
                'validation_profile': dict(validation_profile),
                'targeting_policy': dict(targeting_policy),
                'constraint_check': dict(constraint_check),
            }
            for key in (
                'evidence_chain',
                'prediction_contract',
                'evidence_alignment_audit',
            ):
                value = candidate.get(key)
                if isinstance(value, dict) and value:
                    normalized_params[key] = dict(value)
            for key in ('legacy_semantic_contract', 'contradiction_count', 'proxy_dependency_score'):
                value = candidate.get(key)
                if value not in (None, '', [], {}):
                    normalized_params[key] = value
            normalized: dict[str, Any] = {
                'name': str(candidate.get('name') or '外部 AI 候选策略'),
                'strategy_type': strategy_type,
                'params': normalized_params,
                'target_symbols': list(target_symbols),
                'stock_pool': stock_pool,
                'dsl': dsl,
                'hypothesis': hypothesis,
                'holding_horizon': holding_horizon,
                'trade_plan': normalized_trade_plan,
                'risk_rules': dict(risk_rules),
                'position_sizing': normalized_position_sizing,
                'execution_notes': normalized_execution_notes,
                'rebalance_rule': rebalance_rule,
                'portfolio_spec': portfolio_spec,
                'execution_assumptions': execution_assumptions,
                'validation_profile': validation_profile,
                'targeting_policy': dict(targeting_policy),
                'target_alignment_contract': dict(target_alignment_contract),
                'constraint_check': constraint_check,
                'tags': [str(item) for item in [*tags, 'target_contract_enforced'] if str(item or '').strip()][:8],
            }
            for key in (
                'evidence_chain',
                'prediction_contract',
                'evidence_alignment_audit',
            ):
                value = candidate.get(key)
                if isinstance(value, dict) and value:
                    normalized[key] = dict(value)
            for key in ('legacy_semantic_contract', 'contradiction_count', 'proxy_dependency_score'):
                value = candidate.get(key)
                if value not in (None, '', [], {}):
                    normalized[key] = value
            if allow_legacy_contract_defaults:
                normalized["_legacy_contract_defaults_applied"] = True
            if isinstance(candidate.get('hypothesis_artifact'), dict) and candidate.get('hypothesis_artifact'):
                normalized['hypothesis_artifact'] = dict(candidate.get('hypothesis_artifact') or {})
            elif isinstance(candidate.get('hypothesis_structured'), dict) and candidate.get('hypothesis_structured'):
                normalized['hypothesis_artifact'] = dict(candidate.get('hypothesis_structured') or {})
            for key in (
                'holding_rationale',
                'alpha_half_life',
                'cost_sensitivity_grid',
                'position_model',
                'capacity_assumption',
                'market_regime_assumption',
                'economic_semantics_score',
                'economic_semantics_missing_fields',
                'validation_focus',
                'hypothesis_artifact_id',
            ):
                if candidate.get(key) not in (None, '', [], {}):
                    normalized[key] = candidate.get(key)
            description = candidate.get('description')
            if description not in (None, ''):
                normalized['description'] = str(description)
            rationale = candidate.get('rationale')
            if rationale not in (None, ''):
                normalized['rationale'] = str(rationale)
            selection_logic = candidate.get('selection_logic')
            if selection_logic not in (None, '', [], {}):
                if isinstance(selection_logic, list):
                    normalized['selection_logic'] = [str(item) for item in selection_logic[:4]]
                else:
                    normalized['selection_logic'] = [str(selection_logic)]
            synthesized_confidence_contract = synthesize_confidence_contract(normalized)
            normalized['confidence_contract'] = dict(synthesized_confidence_contract)
            normalized_params['confidence_contract'] = dict(synthesized_confidence_contract)
            return ensure_candidate_semantic_contract(normalized)

        @classmethod
        def _minimal_output_example(cls, target_symbols: list[str]) -> dict[str, Any]:
            symbols = cls._normalize_code_list(target_symbols, limit=2)
            if not symbols:
                symbols = ['000300']
            stock_pool = {
                'selection_mode': 'explicit',
                'symbols': list(symbols),
            }
            return {
                'candidates': [{
                    'name': 'single_stock_trend_follow',
                    'strategy_type': 'dsl_rule',
                    'hypothesis': '目标股票在中短期趋势延续中更容易产生顺势机会。',
                    'hypothesis_artifact': {
                        'alpha_hypothesis': '目标股票在中短期趋势延续中更容易产生顺势机会。',
                        'failure_mode': {
                            'primary_failure_mode': 'trend_break_or_time_stop',
                            'stop_loss_pct': 0.08,
                        },
                        'target_universe_hypothesis': {
                            'target_symbols': list(symbols),
                            'stock_pool': stock_pool,
                            'target_symbol_policy': 'prefer_intersection',
                        },
                        'family_hint': 'dsl_rule',
                        'holding_rationale': '趋势确认后持有 10 天以内，直到趋势衰减或时间止损。',
                        'alpha_half_life': 10,
                        'cost_sensitivity_grid': {
                            'base_case': {
                                'commission_rate': 0.00025,
                                'slippage_bps': 5,
                            },
                        },
                        'position_model': 'single_name',
                        'capacity_assumption': {
                            'max_position_pct': 1.0,
                            'symbol_count': len(symbols),
                        },
                        'objective_profile': 'high_precision',
                        'trade_density_preference': 'low',
                        'entry_selectivity': 'strict',
                        'regime_required': True,
                        'cost_robust_required': True,
                        'market_regime_assumption': {
                            'summary': '趋势延续需要市场流动性正常且目标股仍保持相对强势。',
                            'preferred_regime': 'trend_expansion',
                            'avoid_regime': 'range_bound_chop',
                        },
                        'validation_focus': 'target_plus_representative',
                    },
                    'holding_horizon': {'max_days': 10},
                    'evidence_chain': {
                        'evidences': [
                            {
                                'evidence_id': 'ev_1',
                                'source_type': 'price_action',
                                'direction': 'up',
                                'summary': '10 日均线向上，收盘价重新站回均线之上。',
                                'proxy_only': False,
                                'target_symbols': list(symbols),
                            },
                            {
                                'evidence_id': 'ev_2',
                                'source_type': 'volume',
                                'direction': 'up',
                                'summary': '突破伴随量能放大，volume_ratio 保持在 1.2 以上。',
                                'proxy_only': False,
                                'target_symbols': list(symbols),
                            },
                        ],
                    },
                    'prediction_contract': {
                        'claims': [
                            {
                                'claim_id': 'claim_1',
                                'expected_move': 'up',
                                'expected_horizon': 10,
                                'evidence_ids': ['ev_1', 'ev_2'],
                            },
                        ],
                    },
                    'trade_plan': {
                        'entry_bias': 'trend_follow',
                        'exit_bias': 'signal_or_time_stop',
                        'entry': {
                            'node_id': 'entry_1',
                            'claim_ids': ['claim_1'],
                            'evidence_ids': ['ev_1', 'ev_2'],
                        },
                        'exit': {
                            'node_id': 'exit_1',
                            'claim_ids': ['claim_1'],
                        },
                    },
                    'risk_rules': {'stop_loss_pct': 0.08, 'take_profit_pct': 0.18, 'max_holding_days': 10},
                    'position_sizing': {'mode': 'single_name', 'position_assumption': 'single_name_full_notional'},
                    'execution_notes': 'prefer liquid session execution',
                    'rebalance_rule': {'mode': 'signal_rebalance'},
                    'portfolio_spec': {'position_assumption': 'single_name_full_notional', 'target_weight_scheme': 'single_name'},
                    'execution_assumptions': {'commission_rate': 0.00025, 'slippage_bps': 5, 'tradability_filter': True, 'slippage_model': 'fixed'},
                    'validation_profile': {
                        'profile': 'trade_rule_validation',
                        'validation_focus': 'target_plus_representative',
                        'primary_validation_layer': 'target',
                        'objective_profile': 'high_precision',
                        'trade_density_preference': 'low',
                        'entry_selectivity': 'strict',
                        'regime_required': True,
                        'cost_robust_required': True,
                    },
                    'target_symbols': list(symbols),
                    'stock_pool': stock_pool,
                    'dsl': {
                        'version': '1.0',
                        'timeframe': 'daily',
                        'entry': {
                            'trade_plan_node_id': 'entry_1',
                            'any': [{
                                'op': 'cross_above',
                                'left': {'field': 'close'},
                                'right': {'indicator': 'sma', 'field': 'close', 'window': 10},
                            }],
                        },
                        'exit': {
                            'trade_plan_node_id': 'exit_1',
                            'any': [{
                                'op': 'cross_below',
                                'left': {'field': 'close'},
                                'right': {'indicator': 'sma', 'field': 'close', 'window': 10},
                            }],
                        },
                        'metadata': {
                            'target_symbols': list(symbols),
                            'stock_pool': stock_pool,
                        },
                    },
                    'tags': ['external_llm', 'daily_dsl'],
                }],
            }

        @classmethod
        def _sanitize_expr_for_candidate(cls, expr: Any) -> dict[str, Any]:
            if not isinstance(expr, dict):
                return dict(expr or {}) if isinstance(expr, dict) else {}
            payload = dict(expr)
            indicator = str(payload.get('indicator') or '').strip().lower()
            if indicator:
                field = str(payload.get('field') or 'close').strip().lower() or 'close'
                if field not in {'open', 'high', 'low', 'close', 'volume'}:
                    field = 'close'
                payload['field'] = field
            field = str(payload.get('field') or '').strip().lower()
            if field in {'open', 'high', 'low', 'close', 'volume'}:
                payload['field'] = field
            return payload
        @classmethod
        def _sanitize_condition_for_candidate(cls, node: Any) -> dict[str, Any]:
            if not isinstance(node, dict):
                return {}
            if 'all' in node:
                return {'all': [cls._sanitize_condition_for_candidate(item) for item in list(node.get('all') or []) if cls._sanitize_condition_for_candidate(item)]}
            if 'any' in node:
                return {'any': [cls._sanitize_condition_for_candidate(item) for item in list(node.get('any') or []) if cls._sanitize_condition_for_candidate(item)]}
            if 'not' in node:
                child = cls._sanitize_condition_for_candidate(node.get('not'))
                return {'not': child} if child else {}
            op = str(node.get('op') or '').strip().lower()
            left = cls._sanitize_expr_for_candidate(node.get('left') or {})
            right = cls._sanitize_expr_for_candidate(node.get('right') or {})
            left_indicator = str(left.get('indicator') or '').strip().lower()
            right_indicator = str(right.get('indicator') or '').strip().lower()
            if left_indicator == 'volume_ratio' and 'value' not in right and right_indicator != 'volume_ratio':
                right = {'value': 1.0}
            elif left_indicator == 'rsi' and 'value' not in right and right_indicator != 'rsi':
                right = {'value': 60.0 if op in {'gt', 'gte', 'cross_above'} else 40.0}
            elif left_indicator == 'roc' and 'value' not in right and right_indicator != 'roc':
                right = {'value': 0.01 if op in {'gt', 'gte', 'cross_above'} else -0.01}
            elif left_indicator == 'zscore' and 'value' not in right and right_indicator != 'zscore':
                right = {'value': 0.5 if op in {'gt', 'gte', 'cross_above'} else -0.5}
            return {'op': op, 'left': left, 'right': right}

        @classmethod
        def _sanitize_dsl_for_candidate(cls, dsl: dict[str, Any]) -> dict[str, Any]:
            payload = dict(dsl or {})
            payload['entry'] = cls._sanitize_condition_for_candidate(payload.get('entry'))
            payload['exit'] = cls._sanitize_condition_for_candidate(payload.get('exit'))
            return payload

        @staticmethod
        def _summarize_market_frame(frame: Optional[pd.DataFrame]) -> dict[str, Any]:
            if frame is None or frame.empty:
                return {"rows": 0}
            compact = frame.tail(min(len(frame), 120)).copy()
            result = {"rows": int(len(compact)), "columns": [str(col) for col in compact.columns.tolist()]}
            if "close" in compact.columns:
                close = pd.to_numeric(compact["close"], errors="coerce").dropna()
                if len(close) >= 2:
                    result["close"] = {
                        "latest": float(close.iloc[-1]),
                        "period_return_20": float(close.iloc[-1] / max(close.iloc[max(0, len(close) - 20)], 1e-9) - 1.0) if len(close) >= 20 else float(close.iloc[-1] / max(close.iloc[0], 1e-9) - 1.0),
                        "period_return_full": float(close.iloc[-1] / max(close.iloc[0], 1e-9) - 1.0),
                        "volatility_20": float(close.pct_change().dropna().tail(20).std() if len(close) > 20 else close.pct_change().dropna().std() or 0.0),
                    }
            if "volume" in compact.columns:
                volume = pd.to_numeric(compact["volume"], errors="coerce").dropna()
                if len(volume) >= 5:
                    result["volume"] = {
                        "latest": float(volume.iloc[-1]),
                        "mean_20": float(volume.tail(20).mean()),
                    }
            return result

        @staticmethod
        def _normalize_limit(limit: int) -> int:
            return max(1, min(int(limit or 3), 8))

        @staticmethod
        def _prompt_profile_name(compact_level: int) -> str:
            if compact_level <= 0:
                return "normal"
            if compact_level == 1:
                return "compact"
            return "minimal"

        @classmethod
        def _compact_snapshot(cls, snapshot: dict[str, Any], compact_level: int = 0) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            for key in (
                "date",
                "fear_greed_index",
                "fg_level",
                "listed_count",
                "incubating_count",
                "degraded",
                "margin_5d_change_pct",
                "north_fund_3d_net",
                "factor_ic",
                "factor_ic_trend",
            ):
                if key in snapshot and snapshot.get(key) is not None:
                    payload[key] = cls._round_number(snapshot.get(key), digits=4)

            hot_limit = 4 if compact_level <= 0 else 2
            cold_limit = 3 if compact_level <= 0 else 2
            if snapshot.get("hot_sectors"):
                payload["hot_sectors"] = [str(item) for item in list(snapshot.get("hot_sectors") or [])[:hot_limit]]
            if snapshot.get("cold_sectors"):
                payload["cold_sectors"] = [str(item) for item in list(snapshot.get("cold_sectors") or [])[:cold_limit]]

            category_counts = snapshot.get("category_counts") or {}
            if isinstance(category_counts, dict) and category_counts:
                sorted_items = sorted(category_counts.items(), key=lambda item: item[1], reverse=True)
                payload["category_counts"] = {str(key): int(value) for key, value in sorted_items[: max(2, 5 - compact_level)]}

            completeness = dict(snapshot.get("completeness") or {})
            if completeness:
                payload["data_quality"] = {
                    "completion_ratio": cls._round_number(completeness.get("completion_ratio") or 0.0, digits=4),
                    "missing_sources": [
                        str(item) for item in list(completeness.get("missing_sources") or [])[: max(1, 3 - min(compact_level, 2))]
                    ],
                }

            failure_reasons = list(snapshot.get("failure_reasons") or [])
            if failure_reasons:
                payload["failure_reasons"] = [
                    {
                        "source": str((item or {}).get("source") or ""),
                        "reason": str((item or {}).get("reason") or ""),
                    }
                    for item in failure_reasons[: max(1, 2 - min(compact_level, 1))]
                ]
            return payload

        @classmethod
        def _compact_parent_strategies(cls, parent_strategies: list[dict[str, Any]], compact_level: int = 0) -> list[dict[str, Any]]:
            rows = []
            for item in list(parent_strategies or [])[: max(1, 3 - min(compact_level, 1))]:
                rows.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "strategy_type": item.get("strategy_type"),
                    "status": item.get("status"),
                    "tags": list(item.get("tags") or [])[: max(1, 3 - compact_level)],
                })
            return rows

        @classmethod
        def _compact_history_summary(cls, history_summary: list[dict[str, Any]], compact_level: int = 0) -> list[dict[str, Any]]:
            rows = []
            for item in list(history_summary or [])[: max(2, 6 - compact_level * 2)]:
                payload = {
                    "parent_strategy_id": item.get("parent_strategy_id"),
                    "generator_type": item.get("generator_type"),
                    "status": item.get("status"),
                    "decision": item.get("decision"),
                    "final_score": cls._round_number(item.get("final_score"), digits=4),
                }
                if item.get("family_hint") not in (None, "", [], {}):
                    payload["family_hint"] = item.get("family_hint")
                if item.get("validation_focus") not in (None, "", [], {}):
                    payload["validation_focus"] = item.get("validation_focus")
                if item.get("replay_ready") is not None:
                    payload["replay_ready"] = bool(item.get("replay_ready"))
                target_symbols = list(item.get("target_symbols") or [])
                if target_symbols:
                    payload["target_symbols"] = target_symbols[: max(1, 4 - compact_level)]
                rows.append(payload)
            return rows

        @classmethod
        def _compact_symbol_insight(cls, item: dict[str, Any], compact_level: int = 0) -> dict[str, Any]:
            payload = {
                "code": item.get("code"),
                "name": item.get("name"),
                "industry": item.get("industry"),
                "close": cls._round_number(item.get("close"), digits=4),
                "return_20d": cls._round_number(item.get("return_20d"), digits=4),
                "trend_state": item.get("trend_state"),
            }
            if compact_level <= 1:
                payload["return_5d"] = cls._round_number(item.get("return_5d"), digits=4)
                payload["volume_ratio_20"] = cls._round_number(item.get("volume_ratio_20"), digits=4)
            if compact_level == 0:
                payload["volatility_20d"] = cls._round_number(item.get("volatility_20d"), digits=4)
                payload["price_vs_sma20"] = item.get("price_vs_sma20")
            return payload

        @classmethod
        def _compact_candidate_universe_item(cls, item: dict[str, Any], compact_level: int = 0) -> dict[str, Any]:
            payload = {
                "code": item.get("code"),
                "name": item.get("name"),
                "industry": item.get("industry"),
                "return_20d": cls._round_number(item.get("return_20d"), digits=4),
                "trend_state": item.get("trend_state"),
                "volume_ratio_20": cls._round_number(item.get("volume_ratio_20"), digits=4),
                "screen_score": cls._round_number(item.get("screen_score"), digits=4),
            }
            if compact_level == 0:
                payload["market_cap"] = cls._round_number(item.get("market_cap"), digits=4)
                payload["pe_ratio"] = cls._round_number(item.get("pe_ratio"), digits=4)
                payload["pb_ratio"] = cls._round_number(item.get("pb_ratio"), digits=4)
                payload["factor_snapshot"] = {str(key): cls._round_number(value, digits=4) for key, value in dict(item.get("factor_snapshot") or {}).items()}
                payload["financial_snapshot"] = {
                    "revenue_growth": cls._round_number((item.get("financial_snapshot") or {}).get("revenue_growth"), digits=4),
                    "profit_growth": cls._round_number((item.get("financial_snapshot") or {}).get("profit_growth"), digits=4),
                    "roe": cls._round_number((item.get("financial_snapshot") or {}).get("roe"), digits=4),
                }
            elif compact_level == 1:
                factor_snapshot = dict(item.get("factor_snapshot") or {})
                if factor_snapshot:
                    top_factor_items = list(factor_snapshot.items())[:3]
                    payload["factor_snapshot"] = {str(key): cls._round_number(value, digits=4) for key, value in top_factor_items}
            return payload

        @classmethod
        def _compact_task_target_context(
            cls,
            context: dict[str, Any],
            *,
            compact_level: int,
            symbol_limit: int,
            candidate_limit: int,
        ) -> dict[str, Any]:
            task_target_context = dict(context.get("task_target_context") or {})
            research_task = dict(context.get("research_task") or {})
            requested_target_symbols = cls._normalize_code_list([
                task_target_context.get("requested_target_symbols"),
                research_task.get("target_symbols"),
            ], limit=8)
            target_symbol_set = set(requested_target_symbols)
            raw_symbols = [dict(item or {}) for item in list(context.get("symbol_insights") or [])]
            raw_candidates = [dict(item or {}) for item in list(context.get("candidate_universe") or [])]
            compact_symbols = [dict(item or {}) for item in list(task_target_context.get("symbol_insights") or [])]
            compact_candidates = [dict(item or {}) for item in list(task_target_context.get("candidate_universe") or [])]
            if target_symbol_set:
                filtered_symbols = [item for item in raw_symbols if str(item.get("code") or "").strip() in target_symbol_set]
                filtered_candidates = [item for item in raw_candidates if str(item.get("code") or "").strip() in target_symbol_set]
            else:
                filtered_symbols = raw_symbols
                filtered_candidates = raw_candidates
            effective_symbols = filtered_symbols or compact_symbols
            effective_candidates = filtered_candidates or compact_candidates
            matched_target_symbols = cls._normalize_code_list([
                task_target_context.get("matched_target_symbols"),
                [
                    item.get("code")
                    for item in [*effective_symbols, *effective_candidates]
                    if str(item.get("code") or "").strip()
                ],
            ], limit=8)
            compact_payload = {
                "targeted_task": bool(task_target_context.get("targeted_task") or requested_target_symbols),
                "status": task_target_context.get("status") or context.get("target_context_status"),
                "blocked_by_target_universe": bool(
                    task_target_context.get("blocked_by_target_universe")
                    or context.get("blocked_by_target_universe")
                ),
                "requested_target_symbols": requested_target_symbols,
                "matched_target_symbols": matched_target_symbols,
                "symbol_count": int(task_target_context.get("symbol_count") or len(effective_symbols)),
                "candidate_universe_count": int(task_target_context.get("candidate_universe_count") or len(effective_candidates)),
                "symbol_insight_codes": cls._normalize_code_list([
                    task_target_context.get("symbol_insight_codes"),
                    [item.get("code") for item in effective_symbols],
                ], limit=max(2, symbol_limit)),
                "candidate_universe_symbols": cls._normalize_code_list([
                    task_target_context.get("candidate_universe_symbols"),
                    [item.get("code") for item in effective_candidates],
                ], limit=max(2, candidate_limit)),
            }
            if compact_level >= 2:
                return {
                    key: value
                    for key, value in compact_payload.items()
                    if value not in (None, [], {}, "")
                }
            compact_payload["symbol_insights"] = [
                cls._compact_symbol_insight(item, compact_level=compact_level)
                for item in effective_symbols[:symbol_limit]
            ]
            compact_payload["candidate_universe"] = [
                cls._compact_candidate_universe_item(item, compact_level=compact_level)
                for item in effective_candidates[:candidate_limit]
            ]
            return {
                key: value
                for key, value in compact_payload.items()
                if value not in (None, [], {}, "")
            }

        @classmethod
        def _compact_market_background_context(
            cls,
            context: dict[str, Any],
            *,
            compact_level: int,
            symbol_limit: int,
            candidate_limit: int,
            targeted_task: bool,
        ) -> dict[str, Any]:
            market_background_context = dict(context.get("market_background_context") or {})
            raw_symbols = [dict(item or {}) for item in list(market_background_context.get("symbol_insights") or [])]
            raw_candidates = [dict(item or {}) for item in list(market_background_context.get("candidate_universe") or [])]
            if not raw_symbols and not targeted_task:
                raw_symbols = [dict(item or {}) for item in list(context.get("symbol_insights") or [])]
            if not raw_candidates and not targeted_task:
                raw_candidates = [dict(item or {}) for item in list(context.get("candidate_universe") or [])]
            compact_payload = {
                "available": bool(
                    market_background_context.get("available")
                    or raw_symbols
                    or raw_candidates
                    or market_background_context
                ),
                "symbol_count": int(market_background_context.get("symbol_count") or len(raw_symbols)),
                "candidate_universe_count": int(
                    market_background_context.get("candidate_universe_count") or len(raw_candidates)
                ),
                "symbol_insight_codes": cls._normalize_code_list([
                    market_background_context.get("symbol_insight_codes"),
                    [item.get("code") for item in raw_symbols],
                ], limit=max(2, symbol_limit)),
                "candidate_universe_symbols": cls._normalize_code_list([
                    market_background_context.get("candidate_universe_symbols"),
                    [item.get("code") for item in raw_candidates],
                ], limit=max(2, candidate_limit)),
                "total_stock_count": market_background_context.get("total_stock_count"),
                "scanned_stock_count": market_background_context.get("scanned_stock_count"),
                "data_ready_count": market_background_context.get("data_ready_count"),
                "coverage_ratio": cls._round_number(market_background_context.get("coverage_ratio"), digits=4),
                "top_industries": dict(list((market_background_context.get("top_industries") or {}).items())[:4]),
                "cache_reused": bool(market_background_context.get("cache_reused")),
            }
            if compact_level >= 2:
                return {
                    key: value
                    for key, value in compact_payload.items()
                    if value not in (None, [], {}, "")
                }
            compact_payload["symbol_insights"] = [
                cls._compact_symbol_insight(item, compact_level=compact_level)
                for item in raw_symbols[:symbol_limit]
            ]
            compact_payload["candidate_universe"] = [
                cls._compact_candidate_universe_item(item, compact_level=compact_level)
                for item in raw_candidates[:candidate_limit]
            ]
            return {
                key: value
                for key, value in compact_payload.items()
                if value not in (None, [], {}, "")
            }

        @classmethod
        def _compact_structured_research_value(cls, value: Any, *, compact_level: int) -> Any:
            list_limit = 8 if compact_level <= 0 else (4 if compact_level == 1 else 2)
            dict_limit = 16 if compact_level <= 0 else (10 if compact_level == 1 else 6)
            if isinstance(value, dict):
                compacted: dict[str, Any] = {}
                for index, (key, item) in enumerate(value.items()):
                    if index >= dict_limit:
                        break
                    compact_item = cls._compact_structured_research_value(item, compact_level=compact_level)
                    if compact_item in (None, [], {}, ""):
                        continue
                    compacted[str(key)] = compact_item
                return compacted
            if isinstance(value, list):
                compacted_items = [
                    cls._compact_structured_research_value(item, compact_level=compact_level)
                    for item in value[:list_limit]
                ]
                return [item for item in compacted_items if item not in (None, [], {}, "")]
            if isinstance(value, float):
                return cls._round_number(value, digits=4)
            return value

        @classmethod
        def _compact_structured_research_context_blocks(
            cls,
            context: dict[str, Any],
            *,
            compact_level: int,
        ) -> dict[str, Any]:
            compacted: dict[str, Any] = {}
            for block_key in (
                "strategy_context",
                "backtest_summary",
                "regime_panel",
                "capacity_panel",
                "generalization_seed",
            ):
                block_value = context.get(block_key)
                if block_value in (None, [], {}, ""):
                    continue
                compacted[block_key] = cls._compact_structured_research_value(
                    block_value,
                    compact_level=compact_level,
                )
            return compacted
        @classmethod
        def _compact_research_context(cls, research_context: Optional[dict[str, Any]], compact_level: int = 0) -> dict[str, Any]:
            context = dict(research_context or {})
            structured_blocks = cls._compact_structured_research_context_blocks(
                context,
                compact_level=compact_level,
            )
            market_regime = dict(context.get("market_regime") or {})
            market_breadth = dict(context.get("market_breadth") or {})
            symbol_limit = 4 if compact_level <= 0 else (2 if compact_level == 1 else 1)
            candidate_limit = 8 if compact_level <= 0 else (4 if compact_level == 1 else 2)
            symbols = list(context.get("symbol_insights") or [])[:symbol_limit]
            candidate_universe = list(context.get("candidate_universe") or [])[:candidate_limit]
            population_state = dict(context.get("population_state") or {})
            universe_scan = dict(context.get("universe_scan") or {})
            analysis_scope = dict(context.get("analysis_scope") or {})
            selection_framework = dict(context.get("selection_framework") or {})
            task_target_context = cls._compact_task_target_context(
                context,
                compact_level=compact_level,
                symbol_limit=symbol_limit,
                candidate_limit=candidate_limit,
            )
            targeted_task = bool(task_target_context.get("targeted_task"))
            market_background_context = cls._compact_market_background_context(
                context,
                compact_level=compact_level,
                symbol_limit=symbol_limit,
                candidate_limit=candidate_limit,
                targeted_task=targeted_task,
            )
            primary_symbol_codes_source = (
                task_target_context.get("symbol_insight_codes")
                if targeted_task
                else market_background_context.get("symbol_insight_codes")
            )
            primary_candidate_codes_source = (
                task_target_context.get("candidate_universe_symbols")
                if targeted_task
                else market_background_context.get("candidate_universe_symbols")
            )
            primary_symbol_codes = list(primary_symbol_codes_source or [])
            primary_candidate_codes = list(primary_candidate_codes_source or [])
            if compact_level >= 2:
                payload = {
                    "target_context_status": context.get("target_context_status") or task_target_context.get("status"),
                    "blocked_by_target_universe": bool(context.get("blocked_by_target_universe")),
                    "market_regime": {
                        "fg_level": market_regime.get("fg_level"),
                        "fear_greed_index": cls._round_number(market_regime.get("fear_greed_index"), digits=4),
                    },
                    "task_target_context": task_target_context,
                    "market_background_context": market_background_context,
                    "candidate_universe_symbols": primary_candidate_codes[:4],
                    "symbol_insight_codes": primary_symbol_codes[:2],
                }
                return {
                    key: value
                    for key, value in {**payload, **structured_blocks}.items()
                    if value not in (None, [], {}, "")
                }
            payload = {
                "target_context_status": context.get("target_context_status") or task_target_context.get("status"),
                "blocked_by_target_universe": bool(context.get("blocked_by_target_universe")),
                "market_regime": {
                    "fg_level": market_regime.get("fg_level"),
                    "fear_greed_index": cls._round_number(market_regime.get("fear_greed_index"), digits=4),
                    "hot_sectors": list(market_regime.get("hot_sectors") or [])[: max(2, 4 - compact_level)],
                    "cold_sectors": list(market_regime.get("cold_sectors") or [])[: max(1, 3 - compact_level)],
                    "factor_ic_trend": dict(list((market_regime.get("factor_ic_trend") or {}).items())[: max(2, 4 - compact_level)]),
                },
                "market_breadth": {
                    "symbol_count": market_breadth.get("symbol_count"),
                    "trend_up_count": market_breadth.get("trend_up_count"),
                    "trend_down_count": market_breadth.get("trend_down_count"),
                    "avg_return_20d": cls._round_number(market_breadth.get("avg_return_20d"), digits=4),
                    "avg_volatility_20d": cls._round_number(market_breadth.get("avg_volatility_20d"), digits=4),
                },
                "symbol_insights": [cls._compact_symbol_insight(item, compact_level=compact_level) for item in symbols],
                "candidate_universe": [cls._compact_candidate_universe_item(item, compact_level=compact_level) for item in candidate_universe],
                "symbol_insight_codes": primary_symbol_codes,
                "candidate_universe_symbols": primary_candidate_codes,
                "task_target_context": task_target_context,
                "market_background_context": market_background_context,
                "universe_scan": {
                    "total_stock_count": universe_scan.get("total_stock_count"),
                    "scanned_stock_count": universe_scan.get("scanned_stock_count"),
                    "data_ready_count": universe_scan.get("data_ready_count"),
                    "coverage_ratio": cls._round_number(universe_scan.get("coverage_ratio"), digits=4),
                    "candidate_universe_count": universe_scan.get("candidate_universe_count"),
                    "top_industries": dict(list((universe_scan.get("top_industries") or {}).items())[: max(2, 6 - compact_level)]),
                },
                "analysis_scope": {
                    "scan_mode": analysis_scope.get("scan_mode"),
                    "scan_limit": analysis_scope.get("scan_limit"),
                    "kline_scan_limit": analysis_scope.get("kline_scan_limit"),
                    "detail_limit": analysis_scope.get("detail_limit"),
                    "candidate_pool_limit": analysis_scope.get("candidate_pool_limit"),
                },
                "selection_framework": {
                    "technical": list(selection_framework.get("technical") or [])[:4],
                    "fundamental": list(selection_framework.get("fundamental") or [])[:4],
                    "factor_names": list(selection_framework.get("factor_names") or [])[:3],
                },
                "population_state": {
                    "listed_count": population_state.get("listed_count"),
                    "incubating_count": population_state.get("incubating_count"),
                    "top_categories": dict(list((population_state.get("top_categories") or {}).items())[: max(2, 5 - compact_level)]),
                },
            }
            return {
                key: value
                for key, value in {**payload, **structured_blocks}.items()
                if value not in (None, [], {}, "")
            }

        @classmethod
        def _compact_market_summary(cls, market_summary: Optional[dict[str, Any]], compact_level: int = 0) -> dict[str, Any]:
            summary = dict(market_summary or {})
            if compact_level < 2:
                return summary
            payload = {"rows": summary.get("rows")}
            close = dict(summary.get("close") or {})
            if close:
                payload["close"] = {
                    "latest": cls._round_number(close.get("latest"), digits=4),
                    "period_return_20": cls._round_number(close.get("period_return_20"), digits=4),
                    "volatility_20": cls._round_number(close.get("volatility_20"), digits=4),
                }
            volume = dict(summary.get("volume") or {})
            if volume:
                payload["volume"] = {
                    "latest": cls._round_number(volume.get("latest"), digits=4),
                    "mean_20": cls._round_number(volume.get("mean_20"), digits=4),
                }
            return payload

        @classmethod
        def _compact_research_task(cls, research_task: Optional[dict[str, Any]], compact_level: int = 0) -> dict[str, Any]:
            task = normalize_research_task_contract(research_task)
            target_alignment_contract = dict(task.get("target_alignment_contract") or {})
            compact = {
                "task_id": task.get("task_id"),
                "task_source": task.get("task_source"),
                "opportunity_type": task.get("opportunity_type"),
                "target_symbols": list(task.get("target_symbols") or [])[:5],
                "preferred_strategy_types": list(task.get("preferred_strategy_types") or [])[:4],
                "allowed_strategy_types": list(task.get("allowed_strategy_types") or [])[:6],
                "target_symbol_policy": task.get("target_symbol_policy"),
                "universe_expansion_policy": task.get("universe_expansion_policy"),
                "preference_strength": task.get("preference_strength"),
                "validation_focus": task.get("validation_focus"),
            }
            is_event_driven_task = any(task.get(key) not in (None, "", [], {}) for key in ("event_id", "theme_code", "direction", "horizon"))
            if is_event_driven_task:
                compact.update({
                    "event_id": task.get("event_id"),
                    "event_type": task.get("event_type"),
                    "theme": task.get("theme"),
                    "theme_code": task.get("theme_code"),
                    "direction": task.get("direction"),
                    "horizon": task.get("horizon"),
                    "event_window": dict(task.get("event_window") or {}),
                    "estimation_window": dict(task.get("estimation_window") or {}),
                    "holding_window": dict(task.get("holding_window") or {}),
                })
            if compact_level <= 1:
                compact["target_alignment_contract"] = {
                    key: target_alignment_contract.get(key)
                    for key in (
                        "profile",
                        "max_candidate_target_symbols",
                        "min_coverage_ratio",
                        "min_intersection_ratio",
                        "min_required_overlap_count",
                    )
                    if target_alignment_contract.get(key) not in (None, "", [], {})
                }
                compact["theme"] = task.get("theme") or compact.get("theme")
                if task.get("stock_pool"):
                    compact["stock_pool"] = task.get("stock_pool")
                if task.get("selection_logic"):
                    compact["selection_logic"] = list(task.get("selection_logic") or [])[:3]
                if task.get("focus_industries"):
                    compact["focus_industries"] = list(task.get("focus_industries") or [])[:3]
                evidence_bundle = dict(task.get("evidence_bundle") or {})
                if evidence_bundle:
                    compact["evidence_summary"] = {
                        "event_summary": cls._compact_text(evidence_bundle.get("event_summary") or task.get("event_summary"), limit=120),
                        "theme_name": evidence_bundle.get("theme_name") or task.get("theme"),
                        "direction": evidence_bundle.get("direction") or task.get("direction"),
                        "horizon": evidence_bundle.get("horizon") or task.get("horizon"),
                        "signal_count": evidence_bundle.get("signal_count"),
                        "top_symbols": cls._normalize_code_list([
                            evidence_bundle.get("target_symbols"),
                            ((evidence_bundle.get("score_summary") or {}).get("top_symbols") if isinstance(evidence_bundle.get("score_summary"), dict) else None),
                        ], limit=4),
                        "supporting_reasons": [
                            cls._compact_text(item, limit=72)
                            for item in list(evidence_bundle.get("supporting_reasons") or [])[:3]
                        ],
                        "score_summary": dict(evidence_bundle.get("score_summary") or {}),
                    }
            if compact_level >= 2:
                compact = {
                    key: compact.get(key)
                    for key in ("task_id", "opportunity_type", "target_symbols")
                    if compact.get(key) not in (None, [], {}, "")
                }
            return {key: value for key, value in compact.items() if value not in (None, [], {}, "")}

        @staticmethod
        def _prompt_target_symbol_rule(task: Optional[dict[str, Any]]) -> str:
            policy = str((task or {}).get("target_symbol_policy") or "").strip().lower()
            if policy == "strict_intersection":
                return "strict_intersection_with_research_task"
            if policy == "prefer_intersection":
                return "prefer_intersection_with_research_task"
            return policy or "prefer_intersection_with_research_task"

