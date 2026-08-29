"""Layer C: Multi-agent strategy review committee (replaces Gate-3 fixed thresholds).

Simulates a professional investment committee debate:
- Bull Analyst: finds strengths and potential
- Bear Analyst: finds risks and weaknesses
- Risk Manager: evaluates tail risks
- Judge: synthesizes and makes final decision
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .config import AI_VALIDATION_CONFIG
from ...infrastructure.env_loader import load_strategy_llm_env

logger = logging.getLogger(__name__)

_AGENT_PROMPTS = {
    "bull_analyst": """你是一位乐观的量化策略分析师。你的任务是找出这个策略的优势和潜力。
关注：alpha 来源的独特性、市场时机的合理性、参数的稳健性、策略的可扩展性。
请输出 JSON：{"strengths": [...], "alpha_source": "...", "potential_score": 0.0-1.0, "reasoning": "..."}""",

    "bear_analyst": """你是一位严格的量化策略风险审查员。你的任务是找出这个策略的所有风险和缺陷。
关注：过拟合风险、容量限制、alpha 衰减风险、尾部风险、数据窥探、参数敏感性。
请输出 JSON：{"risks": [...], "overfitting_probability": 0.0-1.0, "decay_risk": "low/medium/high", "reasoning": "..."}""",

    "risk_manager": """你是一位保守的风险管理者。你的任务是评估极端情况下的风险敞口。
关注：最大可能亏损、流动性风险、相关性风险、黑天鹅事件影响、容量约束。
请输出 JSON：{"max_loss_scenario": "...", "liquidity_risk": "low/medium/high", "capacity_yuan": 数字, "tail_risk_score": 0.0-1.0, "reasoning": "..."}""",

    "judge": """你是投资委员会主席。你需要综合 Bull Analyst、Bear Analyst 和 Risk Manager 的意见，做出最终决策。

决策标准：
- approve：策略有明确的 alpha 来源，风险可控，过拟合概率低
- observe：策略有潜力但存在不确定性，建议进入观察期
- reject：策略风险过高或缺乏经济学合理性

请输出 JSON：{"decision": "approve/observe/reject", "confidence": 0.0-1.0, "key_reasons": [...], "conditions": [...], "reasoning": "..."}""",
}


class StrategyReviewCommittee:
    """Multi-agent strategy review committee."""

    def __init__(self, llm_gateway=None):
        self._llm_gateway = llm_gateway
        self._config = AI_VALIDATION_CONFIG["layer_c"]

    async def review(
        self,
        candidate: dict[str, Any],
        backtest_result: dict[str, Any],
        quality_score: float,
    ) -> dict[str, Any]:
        """Run the full committee review process.

        Only triggered when Layer B quality_score >= min threshold.
        """
        # Skip if quality score too low (save cost)
        min_score = self._config.get("min_quality_score_to_trigger", 0.5)
        if quality_score < min_score:
            return {
                "decision": "reject",
                "confidence": 0.8,
                "reasoning": f"Quality score {quality_score:.2f} below committee threshold {min_score}",
                "skipped": True,
                "layer": "C",
            }

        context = self._build_context(candidate, backtest_result, quality_score)

        try:
            result = await asyncio.wait_for(
                self._run_committee(context),
                timeout=self._config["total_timeout_sec"],
            )
            result["layer"] = "C"
            return result
        except asyncio.TimeoutError:
            logger.warning("Layer C: committee review timed out after %ss", self._config["total_timeout_sec"])
            return {
                "decision": "observe",
                "confidence": 0.3,
                "reasoning": "Committee review timed out, defaulting to observe",
                "timeout": True,
                "layer": "C",
            }
        except Exception as exc:
            logger.warning("Layer C: committee review failed: %s", exc)
            return {
                "decision": "observe",
                "confidence": 0.3,
                "reasoning": f"Committee review error: {type(exc).__name__}",
                "error": str(exc),
                "layer": "C",
            }

    async def _run_committee(self, context: str) -> dict[str, Any]:
        """Execute the multi-agent debate process."""
        # Phase 1: Parallel analysis by Bull, Bear, Risk Manager
        analyst_tasks = [
            self._agent_analyze("bull_analyst", context),
            self._agent_analyze("bear_analyst", context),
            self._agent_analyze("risk_manager", context),
        ]
        analyses = await asyncio.gather(*analyst_tasks, return_exceptions=True)

        bull_result = analyses[0] if not isinstance(analyses[0], BaseException) else {"error": str(analyses[0])}
        bear_result = analyses[1] if not isinstance(analyses[1], BaseException) else {"error": str(analyses[1])}
        risk_result = analyses[2] if not isinstance(analyses[2], BaseException) else {"error": str(analyses[2])}

        # Phase 2: Judge synthesizes
        judge_context = f"""{context}

--- Bull Analyst 意见 ---
{json.dumps(bull_result, ensure_ascii=False, indent=2)}

--- Bear Analyst 意见 ---
{json.dumps(bear_result, ensure_ascii=False, indent=2)}

--- Risk Manager 意见 ---
{json.dumps(risk_result, ensure_ascii=False, indent=2)}

请综合以上三方意见，做出最终决策。"""

        judge_result = await self._agent_analyze("judge", judge_context)

        # Normalize decision
        decision = str(judge_result.get("decision") or "observe").strip().lower()
        if decision not in {"approve", "observe", "reject"}:
            decision = "observe"

        return {
            "decision": decision,
            "confidence": float(judge_result.get("confidence") or 0.5),
            "reasoning": judge_result.get("reasoning") or "",
            "key_reasons": list(judge_result.get("key_reasons") or []),
            "conditions": list(judge_result.get("conditions") or []),
            "debate_summary": {
                "bull": bull_result,
                "bear": bear_result,
                "risk": risk_result,
                "judge": judge_result,
            },
            "model_used": self._config["judge_model"],
        }

    async def _agent_analyze(self, agent_name: str, context: str) -> dict[str, Any]:
        """Single agent analysis."""
        system_prompt = _AGENT_PROMPTS.get(agent_name, "")
        model = (
            self._config["judge_model"] if agent_name == "judge"
            else self._config["analyst_model"]
        )

        try:
            response = await asyncio.wait_for(
                self._call_llm(system_prompt, context, model=model),
                timeout=self._config["timeout_per_agent_sec"],
            )
            return self._parse_json(response) or {"raw": response[:500]}
        except asyncio.TimeoutError:
            return {"error": "timeout", "agent": agent_name}
        except Exception as exc:
            return {"error": str(exc), "agent": agent_name}

    async def _call_llm(self, system_prompt: str, user_prompt: str, model: str) -> str:
        """Call LLM for a single agent. Uses project's existing STRATEGY_LLM_* infrastructure."""
        if self._llm_gateway is not None:
            generate_text = getattr(self._llm_gateway, "generate_text", None)
            if callable(generate_text):
                result = await generate_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=model,
                    temperature=0.2,
                    max_tokens=1500,
                    response_format={"type": "json_object"},
                )
                return str(result or "")

            raw = getattr(self._llm_gateway, "raw", self._llm_gateway)
            request_method = getattr(raw, "_request", None) or getattr(raw, "request", None)
            if callable(request_method):
                import inspect
                result = request_method(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.2,
                    max_tokens=1500,
                )
                if inspect.isawaitable(result):
                    result = await result
                return str(result or "")

        # Direct API call using project's STRATEGY_LLM_* env vars
        try:
            import httpx
            import os
            load_strategy_llm_env()
            api_base = os.getenv("STRATEGY_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
            api_key = os.getenv("STRATEGY_LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
            llm_model = os.getenv("STRATEGY_LLM_MODEL", model)
            if not api_key:
                raise RuntimeError("STRATEGY_LLM_API_KEY or DEEPSEEK_API_KEY not set")

            endpoint = f"{api_base}/chat/completions" if not api_base.endswith("/chat/completions") else api_base

            async with httpx.AsyncClient(timeout=self._config["timeout_per_agent_sec"]) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": 1500,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except ImportError:
            raise RuntimeError("httpx not installed")

    def _build_context(self, candidate: dict[str, Any], backtest_result: dict[str, Any], quality_score: float) -> str:
        """Build the review context for all agents."""
        metrics = dict(backtest_result.get("metrics") or {})
        params = dict(candidate.get("params") or {})

        return f"""策略审查请求：

策略类型: {candidate.get('strategy_type')}
ML 质量分数: {quality_score:.2f}

回测指标:
- Sharpe Ratio: {metrics.get('sharpe_ratio', 'N/A')}
- 总收益: {metrics.get('total_return', 'N/A')}
- 最大回撤: {metrics.get('max_drawdown', 'N/A')}
- 胜率: {metrics.get('win_rate', 'N/A')}
- 交易次数: {metrics.get('trades_count', 'N/A')}
- 平均持仓天数: {metrics.get('avg_holding_days', 'N/A')}
- 参数扰动稳定性: {metrics.get('parameter_perturbation_trade_stability', 'N/A')}

核心参数: {json.dumps({k: v for k, v in list(params.items())[:10] if isinstance(v, (int, float, str, bool))}, ensure_ascii=False)}
目标股票: {(candidate.get('target_symbols') or [])[:5]}
风控规则: {candidate.get('risk_rules') or params.get('risk_rules') or '未指定'}
"""

    @staticmethod
    def _parse_json(text: str) -> Optional[dict[str, Any]]:
        """Parse JSON from LLM response."""
        if not text:
            return None
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
            return json.loads(cleaned.strip())
        except (json.JSONDecodeError, TypeError):
            return None
