"""Layer A: LLM-based structural review (replaces Gate-0 fixed rules).

Uses DeepSeek V4 Pro (primary) or Claude Opus (fallback) to:
- Understand strategy logic and detect contradictions
- Auto-classify strategy type
- Assess economic rationale
- Detect look-ahead bias risk
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .config import AI_VALIDATION_CONFIG
from ...infrastructure.env_loader import load_strategy_llm_env

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一位资深 A 股量化策略审查员，拥有 10 年以上的量化投资经验。

你的任务是审查策略候选的结构完整性和逻辑合理性。请严格按照以下维度评估：

1. **策略逻辑自洽性**：入场/出场条件是否矛盾？信号逻辑是否合理？
2. **参数合理性**：参数值是否在经济学合理范围内？（如 lookback=1 天太短，lookback=500 天太长）
3. **风控完整性**：是否有止损/止盈/最大持仓期限？
4. **前视偏差风险**：是否存在使用未来数据的风险？
5. **策略类型识别**：自动判断属于哪种类型（momentum_trend / mean_reversion / event_driven / factor_rank / macro_timing / multi_factor）
6. **经济学合理性**：这个策略背后的经济学逻辑是什么？是否有合理的 alpha 来源？

请严格输出以下 JSON 格式（不要输出其他内容）：
{
    "passed": true或false,
    "strategy_type_detected": "类型名称",
    "confidence": 0.0到1.0的置信度,
    "economic_rationale_score": 0.0到1.0,
    "look_ahead_bias_risk": "none/low/medium/high",
    "issues": ["问题1", "问题2"],
    "strengths": ["优势1", "优势2"],
    "suggestions": ["建议1"],
    "reasoning": "简要推理过程（50字以内）"
}"""


class LLMStructuralReviewer:
    """LLM-based structural review for strategy candidates."""

    def __init__(self, llm_gateway=None):
        self._llm_gateway = llm_gateway
        self._config = AI_VALIDATION_CONFIG["layer_a"]

    async def review(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Review a strategy candidate using LLM.

        Returns structured assessment or fallback result on failure.
        """
        prompt = self._build_prompt(candidate)

        try:
            response = await asyncio.wait_for(
                self._call_llm(prompt),
                timeout=self._config["timeout_sec"],
            )
            parsed = self._parse_response(response)
            if parsed is not None:
                parsed["model_used"] = self._config["primary_model"]
                parsed["layer"] = "A"
                return parsed
        except asyncio.TimeoutError:
            logger.warning("Layer A: LLM timeout after %ss", self._config["timeout_sec"])
        except Exception as exc:
            logger.warning("Layer A: LLM call failed: %s", exc)

        # Retry with fallback model
        if self._config.get("retry_count", 0) > 0:
            try:
                response = await asyncio.wait_for(
                    self._call_llm(prompt, model=self._config["fallback_model"]),
                    timeout=self._config["timeout_sec"],
                )
                parsed = self._parse_response(response)
                if parsed is not None:
                    parsed["model_used"] = self._config["fallback_model"]
                    parsed["layer"] = "A"
                    return parsed
            except Exception as exc:
                logger.warning("Layer A: fallback LLM also failed: %s", exc)

        # Final fallback: rule-based (legacy Gate-0 behavior)
        return self._rule_based_fallback(candidate)

    def _build_prompt(self, candidate: dict[str, Any]) -> str:
        """Build the review prompt from candidate data."""
        strategy_type = candidate.get("strategy_type", "unknown")
        params = candidate.get("params") or {}
        trade_plan = candidate.get("trade_plan") or params.get("trade_plan") or {}
        risk_rules = candidate.get("risk_rules") or params.get("risk_rules") or {}
        holding_horizon = candidate.get("holding_horizon") or params.get("holding_horizon") or {}
        target_symbols = (candidate.get("target_symbols") or [])[:5]
        validation_profile = candidate.get("validation_profile") or {}

        # Extract key numeric params for review
        key_params = {}
        for k, v in list(params.items())[:15]:
            if isinstance(v, (int, float, str, bool)):
                key_params[k] = v

        return f"""请审查以下 A 股策略候选：

策略类型: {strategy_type}
核心参数: {json.dumps(key_params, ensure_ascii=False, indent=2)}
入场逻辑: {trade_plan.get('entry_bias', '未指定')}
出场逻辑: {trade_plan.get('exit_bias', '未指定')}
风控规则: {json.dumps(risk_rules, ensure_ascii=False) if risk_rules else '未指定'}
持仓周期: {holding_horizon}
目标股票: {target_symbols if target_symbols else '未指定'}
验证模式: {validation_profile.get('profile', '未指定')}

请按照系统提示的格式输出 JSON 审查结果。"""

    async def _call_llm(self, prompt: str, model: Optional[str] = None) -> str:
        """Call the LLM gateway. Uses project's existing StrategyLLMProvider infrastructure."""
        resolved_model = model or self._config["primary_model"]

        if self._llm_gateway is not None:
            # Use injected gateway (e.g., AutonomyGateway or StrategyLLMProvider)
            # Try generate_text first (AI validation interface)
            generate_text = getattr(self._llm_gateway, "generate_text", None)
            if callable(generate_text):
                result = await generate_text(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    model=resolved_model,
                    temperature=self._config["temperature"],
                    max_tokens=self._config["max_tokens"],
                    response_format={"type": "json_object"},
                )
                return str(result or "")

            # Fallback: use raw StrategyLLMProvider._request style
            raw = getattr(self._llm_gateway, "raw", self._llm_gateway)
            request_method = getattr(raw, "_request", None) or getattr(raw, "request", None)
            if callable(request_method):
                import inspect
                result = request_method(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self._config["temperature"],
                    max_tokens=self._config["max_tokens"],
                )
                if inspect.isawaitable(result):
                    result = await result
                return str(result or "")

        # Direct API call using project's STRATEGY_LLM_* env vars
        try:
            import httpx
            import os
            load_strategy_llm_env()
            # Reuse the same env vars as StrategyLLMProvider
            api_base = os.getenv("STRATEGY_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
            api_key = os.getenv("STRATEGY_LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
            llm_model = os.getenv("STRATEGY_LLM_MODEL", resolved_model)
            if not api_key:
                raise RuntimeError("STRATEGY_LLM_API_KEY or DEEPSEEK_API_KEY not set")

            endpoint = f"{api_base}/chat/completions" if not api_base.endswith("/chat/completions") else api_base

            async with httpx.AsyncClient(timeout=self._config["timeout_sec"]) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": self._config["temperature"],
                        "max_tokens": self._config["max_tokens"],
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except ImportError:
            raise RuntimeError("httpx not installed, cannot call LLM directly")

    def _parse_response(self, response: str) -> Optional[dict[str, Any]]:
        """Parse and validate LLM JSON response."""
        if not response:
            return None
        try:
            # Strip markdown code blocks if present
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            data = json.loads(text)

            # Validate required fields
            if not isinstance(data, dict):
                return None
            if "passed" not in data:
                return None

            # Normalize and clamp values
            data["passed"] = bool(data.get("passed"))
            data["confidence"] = max(0.0, min(1.0, float(data.get("confidence") or 0.5)))
            data["economic_rationale_score"] = max(0.0, min(1.0, float(data.get("economic_rationale_score") or 0.5)))

            valid_types = {"momentum_trend", "mean_reversion", "event_driven", "factor_rank", "macro_timing", "multi_factor", "unknown"}
            if data.get("strategy_type_detected") not in valid_types:
                data["strategy_type_detected"] = "unknown"

            valid_risks = {"none", "low", "medium", "high"}
            if data.get("look_ahead_bias_risk") not in valid_risks:
                data["look_ahead_bias_risk"] = "medium"

            data["issues"] = list(data.get("issues") or [])[:10]
            data["strengths"] = list(data.get("strengths") or [])[:5]
            data["suggestions"] = list(data.get("suggestions") or [])[:5]

            return data
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.debug("Layer A: failed to parse LLM response: %s", exc)
            return None

    def _rule_based_fallback(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Fallback to simple rule-based check when LLM is unavailable."""
        issues = []
        strategy_type = str(candidate.get("strategy_type") or "").strip()
        params = candidate.get("params")

        if not strategy_type:
            issues.append("missing_strategy_type")
        if not isinstance(params, dict):
            issues.append("params_not_dict")
        if not candidate.get("risk_rules") and not (params or {}).get("risk_rules"):
            issues.append("missing_risk_rules")

        return {
            "passed": len(issues) == 0,
            "strategy_type_detected": "unknown",
            "confidence": 0.3,
            "economic_rationale_score": 0.5,
            "look_ahead_bias_risk": "medium",
            "issues": issues,
            "strengths": [],
            "suggestions": ["LLM unavailable, using rule-based fallback"],
            "reasoning": "rule_based_fallback",
            "model_used": "rule_fallback",
            "layer": "A",
        }
