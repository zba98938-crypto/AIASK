"""High-value MCP prompts built from server-side context."""

from __future__ import annotations

import inspect
import json

from mcp.server.fastmcp.prompts.base import AssistantMessage, UserMessage

from ..services.stock_deep_analysis import get_analysis_run_summary, get_latest_analysis_summary_for_code
from ..services.artifact_registry import get_artifact_async
from ..services.probability_calibration import build_calibration_quality_report
from ..services.factor_prompt_builder import build_factor_mining_prompt
from ..storage import get_db
from ..resources.strategy import build_strategy_review_payload
from ..resources.stock_and_watchlist import build_stock_profile_resource_payload
from ..tools._decision_unified import get_unified_decision_summary
from ..tools.finance import get_financials


def _parse_codes(raw_codes: str) -> list[str]:
    text = str(raw_codes or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
    return [item.strip() for item in text.split(",") if item.strip()]


def _parse_float_list(raw_values: str) -> list[float]:
    text = str(raw_values or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [float(item) for item in parsed]
        except Exception:
            return []
    return [float(item.strip()) for item in text.split(",") if item.strip()]


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def register(mcp) -> None:
    """Register prompt templates."""

    @mcp.prompt(
        name="factor-mining",
        title="Factor Mining",
        description="Build a structured factor-mining prompt from recent market, fund-flow and financial context",
    )
    async def factor_mining(codes: str, candidate_count: int = 8, focus: str | None = None):
        normalized_codes = _parse_codes(codes)
        if not normalized_codes:
            raise ValueError("codes is required")

        db = get_db()
        prompt = await build_factor_mining_prompt(
            db,
            normalized_codes,
            candidate_count=max(1, min(int(candidate_count or 8), 24)),
        )

        user_text = str(prompt.user_prompt or "")
        if focus:
            user_text = f"{user_text}\n\n额外关注点:\n{focus}"

        return [
            AssistantMessage(content=str(prompt.system_prompt or "")),
            UserMessage(content=user_text),
        ]

    @mcp.prompt(
        name="stock-analysis",
        title="Stock Analysis",
        description="Assemble a stock-analysis prompt from profile, financial and decision context",
    )
    async def stock_analysis(code: str, focus: str | None = None, include_financials: bool = True, include_decision: bool = True):
        resolved_code = str(code or "").strip()
        if not resolved_code:
            raise ValueError("code is required")

        profile_payload = await build_stock_profile_resource_payload(resolved_code)
        context = {
            "code": resolved_code,
            "profile": profile_payload,
            "resource_uri": f"resource://stock/{resolved_code}/profile",
        }
        if include_financials:
            context["financials"] = await _maybe_await(get_financials(resolved_code))
        if include_decision:
            context["decision_summary"] = await _maybe_await(get_unified_decision_summary(code=resolved_code))
        if focus:
            context["focus"] = focus

        rubric = "\n".join(
            [
                "请基于结构化上下文输出股票分析结论：",
                "1. 先区分事实、推断和不确定性。",
                "2. 单独说明估值、趋势、财务和决策证据。",
                "3. 若 evidence 不足或存在 fallback / degraded 信号，明确标注。",
                "4. 输出后续建议动作，而不是只给结论。",
            ]
        )
        return [
            AssistantMessage(content=rubric),
            UserMessage(content=json.dumps(context, ensure_ascii=False, indent=2, default=str)),
        ]

    @mcp.prompt(
        name="stock-analysis-deep",
        title="Stock Analysis Deep",
        description="Assemble a deep-analysis prompt from persisted run artifacts or fall back to stock profile, financial and decision context",
    )
    async def stock_analysis_deep(code: str, focus: str | None = None, run_id: str | None = None):
        resolved_code = str(code or "").strip()
        if not resolved_code:
            raise ValueError("code is required")

        deep_context = await (
            get_analysis_run_summary(run_id)
            if str(run_id or "").strip()
            else get_latest_analysis_summary_for_code(resolved_code)
        )
        if deep_context.get("found") is not False:
            context = {
                "code": resolved_code,
                "resource_uri": (
                    f"resource://analysis-run/{deep_context.get('run_id')}/summary"
                    if deep_context.get("run_id")
                    else f"resource://stock/{resolved_code}/deep-analysis"
                ),
                "deep_analysis": deep_context,
                "focus": focus or "",
            }
            rubric = "\n".join(
                [
                    "请基于统一 run 工件输出个股深度分析结论：",
                    "1. 先区分事实、推断、缺口与 fallback 标记。",
                    "2. 每个核心结论都要引用 evidence id 或结构化来源字段。",
                    "3. 单独说明风险与反证，不允许只给单向乐观叙事。",
                    "4. 若 run 尚未通过 final check，明确指出不能直接发布报告。",
                ]
            )
            return [
                AssistantMessage(content=rubric),
                UserMessage(content=json.dumps(context, ensure_ascii=False, indent=2, default=str)),
            ]

        profile_payload = await build_stock_profile_resource_payload(resolved_code)
        financials = await _maybe_await(get_financials(resolved_code))
        decision_summary = await _maybe_await(get_unified_decision_summary(code=resolved_code))
        fallback_context = {
            "code": resolved_code,
            "resource_uri": f"resource://stock/{resolved_code}/profile",
            "profile": profile_payload,
            "financials": financials,
            "decision_summary": decision_summary,
            "focus": focus or "",
        }
        rubric = "\n".join(
            [
                "尚未发现持久化 deep-analysis run，请基于基础上下文生成候选深度分析：",
                "1. 清楚标注当前属于 fallback 视图，而不是协议化 run 结果。",
                "2. 输出缺口与后续动作，不要把候选结论伪装成已完成的正式报告。",
                "3. 若需要正式产品化报告，应先运行 analyze_stock_product_workflow 或对应 skill。",
            ]
        )
        return [
            AssistantMessage(content=rubric),
            UserMessage(content=json.dumps(fallback_context, ensure_ascii=False, indent=2, default=str)),
        ]

    @mcp.prompt(
        name="strategy-review",
        title="Strategy Review",
        description="Assemble a strategy review prompt from lifecycle projection, runtime control and latest review state",
    )
    async def strategy_review(strategy_id: str, focus: str | None = None, include_projection: bool = True):
        payload = await build_strategy_review_payload(strategy_id)
        if not payload.get("found"):
            raise ValueError(str(payload.get("error") or f"strategy not found: {strategy_id}"))

        strategy = dict(payload.get("strategy") or {})
        projection = dict(payload.get("projection") or {}) if include_projection else {}
        review = dict(payload.get("latest_promotion_review") or {})
        runtime_control = dict(payload.get("runtime_control") or {})
        open_risks = list(payload.get("open_risks") or [])

        review_context = {
            "strategy": strategy,
            "projection": projection,
            "latest_promotion_review": review,
            "runtime_control": runtime_control,
            "open_risks": open_risks,
            "summary": payload.get("summary") or {},
            "focus": focus or "",
            "resource_uri": f"resource://strategy/{strategy_id}/review",
        }

        rubric = "\n".join(
            [
                "请从以下维度评审该策略：",
                "1. 生命周期阶段与当前状态是否一致。",
                "2. 最新推广评审、运行时控制和风险事件是否存在冲突。",
                "3. 是否需要补充投影重建、风控动作或指标回补。",
                "4. 给出优先级明确的整改建议。",
            ]
        )

        return [
            AssistantMessage(content=rubric),
            UserMessage(content=json.dumps(review_context, ensure_ascii=False, indent=2, default=str)),
        ]

    @mcp.prompt(
        name="prediction-diagnosis",
        title="Prediction Diagnosis",
        description="Build a prompt for reviewing calibration quality, uncertainty and prediction reliability",
    )
    async def prediction_diagnosis(probabilities: str, labels: str, method: str = "raw", focus: str | None = None):
        probs = _parse_float_list(probabilities)
        ys = _parse_float_list(labels)
        if not probs or len(probs) != len(ys):
            raise ValueError("probabilities and labels are required and must share the same length")

        report = build_calibration_quality_report(
            probs,
            ys,
            calibration_method=str(method or "raw").strip().lower(),
            calibration_version="prompt_v1",
        )
        context = {
            "method": str(method or "raw").strip().lower(),
            "sample_size": len(probs),
            "probabilities": probs,
            "labels": ys,
            "calibration_report": report.to_dict(),
            "focus": focus or "",
        }
        rubric = "\n".join(
            [
                "请评估该预测输出是否可靠：",
                "1. 先解释校准质量、ECE、Brier 和样本量限制。",
                "2. 区分模型本身问题与数据样本问题。",
                "3. 给出最优先的修正动作，例如重新校准、补样本或降低使用等级。",
            ]
        )
        return [
            AssistantMessage(content=rubric),
            UserMessage(content=json.dumps(context, ensure_ascii=False, indent=2, default=str)),
        ]

    @mcp.prompt(
        name="factor-registry-review",
        title="Factor Registry Review",
        description="Review a factor candidate artifact or code set before promotion into registry workflows",
    )
    async def factor_registry_review(artifact_id: str | None = None, codes: str | None = None, focus: str | None = None):
        artifact = None
        resolved_artifact_id = str(artifact_id or "").strip()
        if resolved_artifact_id:
            artifact = await get_artifact_async(resolved_artifact_id)
            if artifact is None:
                raise ValueError(f"artifact not found: {resolved_artifact_id}")

        normalized_codes = _parse_codes(codes or "")
        context = {
            "artifact_id": resolved_artifact_id or None,
            "artifact": artifact,
            "codes": normalized_codes,
            "focus": focus or "",
        }
        rubric = "\n".join(
            [
                "请从候选治理角度评审该因子上下文：",
                "1. 判断是否具备继续验证或入池的证据。",
                "2. 识别 fallback、重复、复杂度过高和研究留痕不足等问题。",
                "3. 明确说明下一步应该是验证、回放、入池还是拒绝。",
            ]
        )
        return [
            AssistantMessage(content=rubric),
            UserMessage(content=json.dumps(context, ensure_ascii=False, indent=2, default=str)),
        ]

    @mcp.prompt(
        name="strategy-promotion-review",
        title="Strategy Promotion Review",
        description="Assemble a strategy-promotion review prompt from lifecycle and runtime context",
    )
    async def strategy_promotion_review(strategy_id: str, focus: str | None = None):
        payload = await build_strategy_review_payload(strategy_id)
        if not payload.get("found"):
            raise ValueError(str(payload.get("error") or f"strategy not found: {strategy_id}"))

        context = {
            "resource_uri": f"resource://strategy/{strategy_id}/review",
            "strategy_review": payload,
            "focus": focus or "",
        }
        rubric = "\n".join(
            [
                "请从晋级评审视角审阅该策略：",
                "1. 是否具备进入下一生命周期阶段的证据。",
                "2. 当前 runtime 风险是否会阻断推广。",
                "3. 需要补做哪些回放、风控动作或投影重建。",
                "4. 输出 promote / hold / reject 及其理由。",
            ]
        )
        return [
            AssistantMessage(content=rubric),
            UserMessage(content=json.dumps(context, ensure_ascii=False, indent=2, default=str)),
        ]
