"""决策跨工具 consensus meta-tool。

P1-3.3 fix(诊断报告 §3.3):
4-5 个 decision 工具同股拿到 3 种方向(hold/hold/sell/watch/sell)。
单工具不可信,AI 必须 cross-validate ≥3 工具同向才执行。

本工具一次性查询:
  - should_i_buy
  - should_i_sell (need buy_price/holding_days,缺失则跳过)
  - smart_stock_diagnosis
  - get_unified_decision
  - build_stock_context.recommendation

输出:
  - decisions: list[dict] 每条含 tool/recommendation/score/reason
  - directions_distribution: {buy/hold/sell/watch}: count
  - dominant_direction: str
  - agreement_ratio: float (dominant 工具占比)
  - cross_tool_consensus: "agree"|"split"|"divergent"
  - tools_agree / tools_split: list[str]
  - actionable_recommendation: str (最终建议给 AI)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..utils import ok, fail, resolve_existing_security_code_async


_DIRECTION_NORMALIZE = {
    "buy": "buy",
    "strong_buy": "buy",
    "promote": "buy",
    "long": "buy",
    "accumulate": "buy",
    "hold": "hold",
    "neutral": "hold",
    "wait": "hold",
    "sell": "sell",
    "strong_sell": "sell",
    "short": "sell",
    "exit": "sell",
    "reduce": "sell",
    "watch": "watch",
    "monitor": "watch",
    "wait_and_see": "watch",
    # P3-B5 fix: 补充 avoid/reject 等高频上游 raw_action(对话式复测发现)
    # 历史问题:should_i_buy 返回 raw_action="avoid",此处不识别 → recommendation=None
    #          → available=false → tools_available 计数偏低、跨工具一致性失真
    "avoid": "watch",      # avoid 语义介于 sell 与 hold,标 watch 更稳健(不强制平仓)
    "do_not_buy": "watch",
    "reject": "sell",
    "caution": "watch",
    "abstain": "watch",
}


def _normalize_direction(value: Any) -> str | None:
    if not value:
        return None
    txt = str(value).strip().lower()
    return _DIRECTION_NORMALIZE.get(txt)


def _extract_decision_from_payload(label: str, payload: Any) -> dict:
    """从 5 个 decision 工具响应中标准化提取 recommendation。"""
    out = {"tool": label, "recommendation": None, "score": None, "reason": None, "raw_action": None, "available": False}
    if not isinstance(payload, dict) or not payload.get("success"):
        out["error"] = "no_success" if not (payload and payload.get("error")) else str(payload.get("error"))
        return out

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        out["error"] = "non_dict_data"
        return out

    # 标准 recommendation 字段
    raw_rec = (
        data.get("recommendation")
        or data.get("action")
        or data.get("decision_summary", {}).get("action")
        if isinstance(data.get("decision_summary"), dict) else data.get("recommendation") or data.get("action")
    )
    out["raw_action"] = str(raw_rec) if raw_rec else None
    out["recommendation"] = _normalize_direction(raw_rec)

    # score
    for key in ("score", "total_score", "decision_score", "confidence"):
        val = data.get(key)
        if val is not None:
            try:
                out["score"] = round(float(val), 2)
                break
            except (TypeError, ValueError):
                continue

    # reason
    for key in ("reason", "text", "summary", "rationale"):
        val = data.get(key)
        if val:
            out["reason"] = str(val)[:200]
            break

    out["available"] = out["recommendation"] is not None
    return out


def register(mcp):
    """注册 decision_consensus 工具(P1-3.3)。"""

    @mcp.tool()
    async def decision_consensus(
        code: Optional[str] = None,
        stock_code: Optional[str] = None,
        symbol: Optional[str] = None,
        ticker: Optional[str] = None,
        investment_style: str = "balanced",
        # should_i_sell 需要的可选参数
        buy_price: float = 0,
        holding_days: int = 0,
        # consensus 阈值
        min_agreement_ratio: float = 0.6,
    ) -> dict:
        """跨决策工具 consensus(诊断报告 §3.3 P1 修复)。

        一次性查询 5 个 decision 工具,计算方向一致性。AI 调用此工具替代
        单工具决策,避免 build_stock_context.sell vs should_i_buy.hold 这类
        矛盾结论。

        Args:
            code: 股票代码
            investment_style: aggressive/balanced/conservative
            buy_price/holding_days: 若提供则同时调 should_i_sell
            min_agreement_ratio: 至少多少比例工具同向才算 agree(默认 0.6)

        Returns:
            dict: {
                "code": str,
                "decisions": list[dict],
                "directions_distribution": dict,
                "dominant_direction": str,
                "agreement_ratio": float,
                "cross_tool_consensus": "agree" | "split" | "divergent",
                "tools_agree": list[str],
                "tools_split": list[str],
                "actionable_recommendation": str,
                "rationale": str
            }
        """
        start_time = time.perf_counter()

        try:
            input_code = code or stock_code or symbol or ticker
            if not input_code:
                return fail("缺少股票代码")
            resolved = await resolve_existing_security_code_async(input_code)
            resolved_code = resolved[0] if isinstance(resolved, tuple) else resolved
            resolved_code = str(resolved_code or input_code).strip()

            # 通过函数 import 5 个 decision 工具实现 — 避免 mcp.tool 注册系统的循环
            from . import _decision_buy as decision_buy_mod
            from . import _decision_sell as decision_sell_mod
            try:
                from . import _decision_context as decision_context_mod
            except ImportError:
                decision_context_mod = None

            decisions: list[dict] = []

            # 1. should_i_buy
            try:
                buy_result = await decision_buy_mod.should_i_buy(
                    code=resolved_code,
                    investment_style=investment_style,
                )
                decisions.append(_extract_decision_from_payload("should_i_buy", buy_result))
            except Exception as exc:
                decisions.append({
                    "tool": "should_i_buy",
                    "available": False,
                    "error": f"{type(exc).__name__}:{exc}",
                })

            # 2. should_i_sell (仅在 buy_price>0 时)
            if buy_price and buy_price > 0:
                try:
                    sell_result = await decision_sell_mod.should_i_sell(
                        code=resolved_code,
                        buy_price=buy_price,
                        holding_days=int(holding_days),
                    )
                    decisions.append(_extract_decision_from_payload("should_i_sell", sell_result))
                except Exception as exc:
                    decisions.append({
                        "tool": "should_i_sell",
                        "available": False,
                        "error": f"{type(exc).__name__}:{exc}",
                    })

            # 3. build_stock_context (其内部含 recommendation)
            if decision_context_mod is not None:
                try:
                    ctx_result = await decision_context_mod.build_stock_context(code=resolved_code)
                    decisions.append(_extract_decision_from_payload("build_stock_context", ctx_result))
                except Exception as exc:
                    decisions.append({
                        "tool": "build_stock_context",
                        "available": False,
                        "error": f"{type(exc).__name__}:{exc}",
                    })

            # 4. smart_stock_diagnosis
            try:
                from . import smart_diagnosis as smart_diag_mod  # type: ignore
                if hasattr(smart_diag_mod, "smart_stock_diagnosis"):
                    diag_result = await smart_diag_mod.smart_stock_diagnosis(code=resolved_code)
                    decisions.append(_extract_decision_from_payload("smart_stock_diagnosis", diag_result))
            except ImportError:
                pass
            except Exception as exc:
                decisions.append({
                    "tool": "smart_stock_diagnosis",
                    "available": False,
                    "error": f"{type(exc).__name__}:{exc}",
                })

            # ----- 共识统计 -----
            available = [d for d in decisions if d.get("available")]
            if not available:
                return ok({
                    "code": resolved_code,
                    "decisions": decisions,
                    "directions_distribution": {},
                    "dominant_direction": None,
                    "agreement_ratio": 0.0,
                    "cross_tool_consensus": "no_data",
                    "tools_agree": [],
                    "tools_split": [],
                    "actionable_recommendation": "no_recommendation",
                    "rationale": "no decision tool returned a recommendation",
                    "elapsed_ms": int((time.perf_counter() - start_time) * 1000),
                })

            distribution: dict[str, int] = {}
            for d in available:
                direction = d.get("recommendation") or "unknown"
                distribution[direction] = distribution.get(direction, 0) + 1
            dominant_direction = max(distribution, key=distribution.get)
            dominant_count = distribution[dominant_direction]
            agreement_ratio = dominant_count / max(len(available), 1)

            tools_agree = [d["tool"] for d in available if d.get("recommendation") == dominant_direction]
            tools_split = [d["tool"] for d in available if d.get("recommendation") != dominant_direction]

            if agreement_ratio >= min_agreement_ratio:
                consensus = "agree"
                actionable = dominant_direction
                rationale = (
                    f"{dominant_count}/{len(available)} tools agree on '{dominant_direction}' "
                    f"(ratio {agreement_ratio:.2f} >= threshold {min_agreement_ratio})"
                )
            elif agreement_ratio >= 0.4:
                consensus = "split"
                actionable = "hold"  # 拿不准就 hold
                rationale = (
                    f"split decision: dominant={dominant_direction} but only {dominant_count}/{len(available)} "
                    f"agree (ratio {agreement_ratio:.2f}). Recommending 'hold' until more signals align"
                )
            else:
                consensus = "divergent"
                actionable = "no_action"
                rationale = (
                    f"divergent decisions: {distribution}. "
                    f"No clear majority direction — defer action and gather more evidence"
                )

            return ok({
                "code": resolved_code,
                "decisions": decisions,
                "directions_distribution": distribution,
                "dominant_direction": dominant_direction,
                "agreement_ratio": round(agreement_ratio, 4),
                "cross_tool_consensus": consensus,
                "tools_agree": tools_agree,
                "tools_split": tools_split,
                "actionable_recommendation": actionable,
                "rationale": rationale,
                "min_agreement_ratio_threshold": min_agreement_ratio,
                "tools_queried": len(decisions),
                "tools_available": len(available),
                "elapsed_ms": int((time.perf_counter() - start_time) * 1000),
            })

        except Exception as exc:
            return fail(f"decision_consensus_failed: {type(exc).__name__}: {exc}")
