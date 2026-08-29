"""Shared helpers for skill orchestration and workflow modules."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ..utils import fail, normalize_code, ok
from .skills_registry import _parse_bool_flag


def _normalize_params(params: Any) -> Dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return dict(params)
    if isinstance(params, str):
        raw = params.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"raw_params": params}
    return {"raw_params": params}


def _skill_meta(
    *,
    backend_requested: str,
    backend_used: str,
    fallback_used: bool,
    fallback_reason: Any = None,
    started_at: datetime | None = None,
) -> Dict[str, Any]:
    latency_ms = 0
    if started_at is not None:
        latency_ms = max(0, int((datetime.now() - started_at).total_seconds() * 1000))
    return {
        "backend_requested": backend_requested,
        "backend_used": backend_used,
        "fallback_used": bool(fallback_used),
        "fallback_reason": fallback_reason,
        "latency_ms": latency_ms,
    }


def _skill_payload(
    payload: Dict[str, Any],
    *,
    backend_requested: str,
    backend_used: str,
    fallback_used: bool,
    fallback_reason: Any = None,
    started_at: datetime | None = None,
) -> Dict[str, Any]:
    return {
        **payload,
        **_skill_meta(
            backend_requested=backend_requested,
            backend_used=backend_used,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            started_at=started_at,
        ),
    }


def _skill_ok(
    payload: Dict[str, Any],
    *,
    backend_requested: str,
    backend_used: str,
    fallback_used: bool,
    fallback_reason: Any = None,
    started_at: datetime | None = None,
) -> Dict[str, Any]:
    return ok(
        _skill_payload(
            payload,
            backend_requested=backend_requested,
            backend_used=backend_used,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            started_at=started_at,
        )
    )


def _skill_fail(
    error: Any,
    *,
    backend_requested: str,
    backend_used: str,
    fallback_used: bool,
    fallback_reason: Any = None,
    started_at: datetime | None = None,
    error_code: str | None = None,
    detail: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    result = fail(error)
    result["message"] = str(error)
    if error_code is not None:
        result["error_code"] = error_code
    if detail is not None:
        result["detail"] = detail
    result.update(
        _skill_meta(
            backend_requested=backend_requested,
            backend_used=backend_used,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            started_at=started_at,
        )
    )
    return result


def _step_result(step: str, output: Any = None, error: str | None = None) -> Dict[str, Any]:
    if error is not None:
        return {"step": step, "success": False, "error": error}
    if isinstance(output, dict):
        return {"step": step, "success": bool(output.get("success", True)), "output": output}
    return {"step": step, "success": True, "output": output}


def _run_step(step: str, fn: Callable[..., Any], **kwargs: Any) -> Dict[str, Any]:
    try:
        result = fn(**kwargs)
        return _step_result(step, output=result)
    except Exception as exc:
        return _step_result(step, error=f"{type(exc).__name__}: {exc}")


async def _run_step_async(step: str, fn: Callable[..., Any], **kwargs: Any) -> Dict[str, Any]:
    import inspect

    try:
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _step_result(step, output=result)
    except Exception as exc:
        return _step_result(step, error=f"{type(exc).__name__}: {exc}")


def _finalize_skill_result(
    task: str,
    steps: List[Dict[str, Any]],
    *,
    backend_requested: str = "skill_orchestrator",
    backend_used: str = "skill_orchestrator",
    fallback_used: bool = False,
    fallback_reason: Any = None,
    started_at: datetime | None = None,
) -> Dict[str, Any]:
    failed = [step["step"] for step in steps if not step.get("success")]
    return _skill_payload(
        {
            "task": task,
            "status": "completed" if not failed else "partial_failed",
            "steps": steps,
            "summary": {
                "total_steps": len(steps),
                "failed_steps": failed,
                "success_count": len(steps) - len(failed),
                "failed_count": len(failed),
            },
        },
        backend_requested=backend_requested,
        backend_used=backend_used,
        fallback_used=fallback_used or bool(failed),
        fallback_reason=fallback_reason if fallback_reason is not None else (failed or None),
        started_at=started_at,
    )


def _unsupported_task_result(task: str, supported_tasks: List[str]) -> Dict[str, Any]:
    return {
        "task": task,
        "status": "unsupported_task",
        "steps": [],
        "summary": {
            "total_steps": 0,
            "failed_steps": [],
            "supported_tasks": supported_tasks,
        },
    }


def _safe_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_codes_input(raw_codes: Any, fallback: List[str]) -> List[str]:
    values: List[str] = []
    if isinstance(raw_codes, str):
        values = [item.strip() for item in raw_codes.split(",") if item.strip()]
    elif isinstance(raw_codes, list):
        values = [str(item or "").strip() for item in raw_codes if str(item or "").strip()]
    deduped: List[str] = []
    seen: set[str] = set()
    for raw in values or fallback:
        code = normalize_code(str(raw or ""))
        if not code or code in seen:
            continue
        seen.add(code)
        deduped.append(code)
    return deduped or [normalize_code(str(fallback[0] or "600519"))]


def _normalize_holdings_input(
    params: Dict[str, Any],
    default_codes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    raw_holdings = params.get("holdings")
    if isinstance(raw_holdings, list) and raw_holdings:
        parsed: List[Dict[str, Any]] = []
        for item in raw_holdings:
            if not isinstance(item, dict):
                continue
            code = normalize_code(str(item.get("code") or item.get("symbol") or ""))
            if not code:
                continue
            parsed.append(
                {
                    "code": code,
                    "weight": _safe_float(item.get("weight"), 0.0),
                    "value": _safe_float(item.get("value"), 0.0),
                    "return_pct": _safe_float(item.get("return_pct"), 0.0),
                }
            )
        if parsed:
            total_weight = sum(max(0.0, float(item.get("weight") or 0.0)) for item in parsed) or 1.0
            return [
                {
                    **item,
                    "weight": round(max(0.0, float(item.get("weight") or 0.0)) / total_weight, 6),
                }
                for item in parsed
            ]

    codes = _normalize_codes_input(
        params.get("codes") or params.get("code"),
        default_codes or ["600519", "000001", "510300"],
    )
    weight = round(1.0 / len(codes), 6) if codes else 1.0
    notional = _safe_float(params.get("initial_capital") or params.get("total_capital"), 1_000_000.0)
    return [
        {
            "code": code,
            "weight": weight,
            "value": round(notional * weight, 2),
            "return_pct": _safe_float(params.get("default_return_pct"), 0.0),
        }
        for code in codes
    ]


def _default_notice_window(params: Dict[str, Any]) -> tuple[str, str]:
    end_date = str(params.get("end_date") or datetime.now().strftime("%Y-%m-%d"))
    start_date = str(
        params.get("start_date")
        or (datetime.now() - timedelta(days=_safe_int(params.get("window_days"), 30))).strftime("%Y-%m-%d")
    )
    return start_date, end_date


def _normalize_rebalance_threshold(value: Any, default: float = 0.08) -> float:
    threshold = _safe_float(value, default)
    if threshold > 1:
        threshold = threshold / 100.0
    return max(0.01, min(threshold, 0.30))


def _static_step(step: str, output: Dict[str, Any]) -> Dict[str, Any]:
    return _step_result(step, output=output)


def _response_data_dict(response: Any) -> Dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return dict(data) if isinstance(data, dict) else {}
