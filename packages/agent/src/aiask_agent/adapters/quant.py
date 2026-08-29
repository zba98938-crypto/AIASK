from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from ..numeric import bounded_float, bounded_int
from ..quant_research import QuantResearchStore, research_id as new_research_id
from .akshare import call_function, load_registered_tool
from . import strategy_factory as strategy_factory_adapter
from ._runtime_paths import configured_sqlite_path, default_aiask_sqlite_path


DATABASE_ENV_KEYS = ("AKSHARE_MCP_SQLITE_PATH", "AIASK_SQLITE_PATH", "SQLITE_BUSY_TIMEOUT_MS", "SQLITE_JOURNAL_MODE")
DEFAULT_FACTORS = ("momentum", "volatility", "value")
DEFAULT_UNIVERSE = ("600519", "000001", "000858")
DISCLAIMER = "NOT_INVESTMENT_ADVICE: This research artifact is decision support only and is not a trading instruction."
DEFAULT_SQLITE_PATH = default_aiask_sqlite_path()


def _is_invalid_sqlite_path(raw_value: str, path: Path) -> bool:
    raw = raw_value.strip()
    if not raw:
        return False
    normalized = raw.replace("\\", "/").lower()
    if normalized == "/dev/null" or normalized.startswith("/dev/null/"):
        return True
    if normalized in {"nul", "null", "con", "prn", "aux"}:
        return True
    try:
        return path.exists() and not path.is_file()
    except OSError:
        return True


def _envelope(
    success: bool,
    *,
    data: Any,
    error: str | None = None,
    tool_name: str = "agent_quant",
    error_code: str | None = None,
    source_chain: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": bool(success),
        "data": data,
        "error": error,
        "meta": {
            "source_chain": source_chain or ["aiask_agent.adapters.quant"],
            "side_effect": {
                "level": "read_only",
                "target": tool_name,
                "confirmation_required": False,
                "idempotent": True,
            },
        },
    }
    if error_code:
        payload["error_code"] = error_code
    return payload


def _ensure_monorepo_paths() -> None:
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent / "aiask-quant-core" / "src",
            parent / "akshare-mcp" / "src",
            parent / "strategy-factory" / "src",
        ]
        added = False
        for candidate in candidates:
            if candidate.exists() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
                added = True
        if added:
            return


def database_status() -> dict[str, Any]:
    configured = [key for key in DATABASE_ENV_KEYS if str(os.getenv(key) or "").strip()]
    path, path_sources = configured_sqlite_path()
    raw_path = str(path)
    if path_sources == ["AIASK_RUNTIME_HOME"]:
        configured = path_sources
    writable = False
    if not _is_invalid_sqlite_path(raw_path, path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".aiask_sqlite_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except Exception:
            writable = False
    return {
        "backend": "sqlite",
        "path": str(path),
        "configured": True,
        "writable": writable,
        "sources": configured or ["default"],
        "required_for_full_quant": True,
        "setup_hint": "SQLite database is ready for local quant research." if writable else f"SQLite path is not writable: {path}",
    }


def quant_presets() -> dict[str, Any]:
    db = database_status()
    return {
        "object": "aiask.quant_presets",
        "data_status": {
            "status": "ready" if db["configured"] and db.get("writable") else "unconfigured",
            "database": db,
        },
        "templates": [
            {
                "id": "balanced_factor_research",
                "label": "Balanced factor research",
                "universe": list(DEFAULT_UNIVERSE),
                "benchmark": "000300",
                "factors": list(DEFAULT_FACTORS),
                "rebalance_frequency": "monthly",
                "cost_bps": 3,
                "slippage_bps": 1,
                "risk_limits": {"max_weight": 0.35, "var_limit": 0.08},
            }
        ],
        "factor_library": [
            "momentum",
            "volatility",
            "value",
            "quality",
            "growth",
            "size",
            "trend",
            "reversal",
        ],
        "risk_defaults": {
            "lookback_days": 252,
            "stress_scenarios": ["market_crash", "sector_rotation", "interest_rate_hike", "black_swan"],
            "max_weight": 0.35,
        },
        "disclaimer": DISCLAIMER,
    }


def _as_list(value: Any, default: tuple[str, ...] = ()) -> list[str]:
    if value is None or value == "":
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    return [str(item).strip() for item in list(value or []) if str(item).strip()]


def _codes(arguments: dict[str, Any]) -> list[str]:
    return _as_list(
        arguments.get("codes") or arguments.get("universe") or arguments.get("stocks") or arguments.get("code"),
        DEFAULT_UNIVERSE,
    )


def _factors(arguments: dict[str, Any]) -> list[str]:
    return _as_list(arguments.get("factors") or arguments.get("factor"), DEFAULT_FACTORS)


def _rate_from_bps(value: Any, default_bps: float) -> float:
    bps = bounded_float(value, default=default_bps, minimum=0.0, maximum=10000.0)
    return bps / 10000.0


def _dependency_unconfigured(tool_name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    db = database_status()
    return _envelope(
        True,
        data={
            "status": "unconfigured",
            "ready": False,
            "database": db,
            "blocking_reason": "LOCAL_DATABASE_REQUIRED",
            **dict(data or {}),
        },
        tool_name=tool_name,
    )


async def _call_registered(module_name: str, function_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        fn = load_registered_tool(module_name, function_name)
        timeout = bounded_float(
            arguments.pop("_timeout_seconds", os.getenv("AIASK_QUANT_TOOL_TIMEOUT", "30")),
            default=30.0,
            minimum=1.0,
            maximum=3600.0,
        )
        result = await asyncio.wait_for(call_function(fn, dict(arguments)), timeout=timeout)
    except TimeoutError as exc:
        return {"success": False, "data": None, "error": str(exc), "error_code": "QUANT_TOOL_TIMEOUT"}
    except asyncio.TimeoutError as exc:
        return {"success": False, "data": None, "error": str(exc), "error_code": "QUANT_TOOL_TIMEOUT"}
    except ModuleNotFoundError as exc:
        return {"success": False, "data": None, "error": str(exc), "error_code": "MISSING_AKSHARE_MCP"}
    except Exception as exc:
        return {"success": False, "data": None, "error": str(exc), "error_code": "QUANT_TOOL_FAILED"}
    if isinstance(result, dict) and {"success", "data", "error"}.issubset(result):
        return result
    return {"success": True, "data": result, "error": None}


async def quant_data_gate(arguments: dict[str, Any]) -> dict[str, Any]:
    tool_name = "agent_quant_data_gate"
    db = database_status()
    codes = _codes(arguments)
    max_stale_days = bounded_int(arguments.get("max_stale_days"), default=5, minimum=0, maximum=3650)
    if not db["configured"] or not db.get("writable", False):
        return _dependency_unconfigured(tool_name, {"codes": codes, "max_stale_days": max_stale_days})

    freshness = await _call_registered(
        "akshare_mcp.tools.db_freshness",
        "check_db_freshness",
        {"codes": codes, "max_stale_days": max_stale_days},
    )
    success = freshness.get("success") is not False
    data = freshness.get("data") if isinstance(freshness.get("data"), dict) else {}
    missing = data.get("missing") or data.get("missing_codes") or []
    stale = data.get("stale") or data.get("stale_codes") or []
    ready = bool(success and not missing and not stale)
    return _envelope(
        True,
        data={
            "status": "ready" if ready else "blocked",
            "ready": ready,
            "codes": codes,
            "max_stale_days": max_stale_days,
            "database": db,
            "freshness": freshness,
            "coverage": {
                "requested": len(codes),
                "missing_count": len(missing) if isinstance(missing, list) else 0,
                "stale_count": len(stale) if isinstance(stale, list) else 0,
            },
            "blocking_reason": None if ready else freshness.get("error") or "DATA_GATE_NOT_PASSED",
        },
        tool_name=tool_name,
    )


async def factor_validation(arguments: dict[str, Any]) -> dict[str, Any]:
    tool_name = "agent_factor_validation"
    db_status = database_status()
    if not db_status["configured"] or not db_status.get("writable", False):
        return _dependency_unconfigured(tool_name, {"codes": _codes(arguments), "factors": _factors(arguments)})

    codes = _codes(arguments)
    factors = _factors(arguments)
    period = bounded_int(arguments.get("period"), default=20, minimum=1, maximum=2520)
    include_oos = bool(arguments.get("include_oos", True))
    include_robustness = bool(arguments.get("include_robustness", True))
    validations: list[dict[str, Any]] = []
    for factor in factors:
        base = {
            "codes": codes,
            "factor": factor,
            "period": period,
            "start_date": arguments.get("start_date"),
            "end_date": arguments.get("end_date"),
        }
        ic = await _call_registered("akshare_mcp.tools.quant", "calculate_factor_ic", dict(base))
        factor_backtest = await _call_registered(
            "akshare_mcp.tools.quant",
            "backtest_factor",
            {
                "codes": codes,
                "factor": factor,
                "groups": bounded_int(arguments.get("groups"), default=5, minimum=1, maximum=100),
                "holding_days": bounded_int(arguments.get("holding_days"), default=period, minimum=1, maximum=2520),
                "commission": _rate_from_bps(arguments.get("cost_bps"), 3),
                "slippage": _rate_from_bps(arguments.get("slippage_bps"), 0),
                "start_date": arguments.get("start_date"),
                "end_date": arguments.get("end_date"),
            },
        )
        oos = None
        if include_oos:
            oos = await _call_registered(
                "akshare_mcp.tools.quant",
                "validate_factor_oos",
                {
                    "codes": codes,
                    "factor": factor,
                    "forward_period": period,
                    "start_date": arguments.get("start_date"),
                    "end_date": arguments.get("end_date"),
                },
            )
        robustness = None
        if include_robustness:
            robustness = await _call_registered(
                "akshare_mcp.tools.quant",
                "factor_robustness_check",
                {
                    "codes": codes,
                    "factor": factor,
                    "start_date": arguments.get("start_date"),
                    "end_date": arguments.get("end_date"),
                },
            )
        checks = [item for item in (ic, factor_backtest, oos, robustness) if item is not None]
        validations.append(
            {
                "factor": factor,
                "status": "passed" if checks and all(item.get("success") is not False for item in checks) else "partial",
                "ic": ic,
                "factor_backtest": factor_backtest,
                "oos": oos,
                "robustness": robustness,
            }
        )
    status = "passed" if validations and all(item["status"] == "passed" for item in validations) else "partial"
    return _envelope(
        True,
        data={"status": status, "codes": codes, "factors": factors, "validations": validations},
        tool_name=tool_name,
    )


async def backtest_suite(arguments: dict[str, Any]) -> dict[str, Any]:
    tool_name = "agent_backtest_suite"
    db_status = database_status()
    if not db_status["configured"] or not db_status.get("writable", False):
        return _dependency_unconfigured(tool_name, {"codes": _codes(arguments)})

    codes = _codes(arguments)
    strategy = str(arguments.get("strategy") or "ma_cross")
    assumptions = {
        "benchmark": str(arguments.get("benchmark") or "000300"),
        "rebalance_frequency": str(arguments.get("rebalance_frequency") or "monthly"),
        "cost_bps": bounded_float(arguments.get("cost_bps"), default=3.0, minimum=0.0, maximum=10000.0),
        "slippage_bps": bounded_float(arguments.get("slippage_bps"), default=1.0, minimum=0.0, maximum=10000.0),
        "start_date": arguments.get("start_date"),
        "end_date": arguments.get("end_date"),
    }
    common = {
        "strategy": strategy,
        "start_date": assumptions["start_date"],
        "end_date": assumptions["end_date"],
        "initial_capital": bounded_float(arguments.get("initial_capital"), default=100000.0, minimum=0.0),
        "commission": _rate_from_bps(assumptions["cost_bps"], 3),
        "benchmark": assumptions["benchmark"],
    }
    if len(codes) == 1:
        result = await _call_registered(
            "akshare_mcp.tools.backtest",
            "run_simple_backtest",
            {**common, "code": codes[0], "slippage": _rate_from_bps(assumptions["slippage_bps"], 1)},
        )
    else:
        result = await _call_registered(
            "akshare_mcp.tools.backtest",
            "run_batch_backtest",
            {**common, "codes": codes, "use_parallel": bool(arguments.get("use_parallel", True))},
        )
        if result.get("success") is False and bool(arguments.get("fallback_to_single", True)):
            singles = [
                await _call_registered(
                    "akshare_mcp.tools.backtest",
                    "run_simple_backtest",
                    {**common, "code": code, "slippage": _rate_from_bps(assumptions["slippage_bps"], 1)},
                )
                for code in codes
            ]
            result = {"success": any(item.get("success") is not False for item in singles), "data": {"single_results": singles}, "error": None}
    return _envelope(
        True,
        data={
            "status": "completed" if result.get("success") is not False else "failed",
            "codes": codes,
            "strategy": strategy,
            "assumptions": assumptions,
            "backtest": result,
        },
        tool_name=tool_name,
    )


def _weights_from_optimization(result: dict[str, Any], codes: list[str]) -> list[float]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    weights = data.get("weights")
    if isinstance(weights, dict):
        return [bounded_float(weights.get(code), default=0.0, minimum=0.0, maximum=1.0) for code in codes]
    if isinstance(weights, list) and len(weights) == len(codes):
        return [bounded_float(item, default=0.0, minimum=0.0, maximum=1.0) for item in weights]
    return [1.0 / len(codes)] * len(codes) if codes else []


async def portfolio_risk(arguments: dict[str, Any]) -> dict[str, Any]:
    tool_name = "agent_portfolio_risk"
    db_status = database_status()
    if not db_status["configured"] or not db_status.get("writable", False):
        return _dependency_unconfigured(tool_name, {"codes": _codes(arguments)})

    codes = _codes(arguments)
    weights = arguments.get("weights")
    if not isinstance(weights, list) or len(weights) != len(codes):
        weights = [1.0 / len(codes)] * len(codes) if codes else []
    optimization = await _call_registered(
        "akshare_mcp.tools.portfolio",
        "optimize_portfolio",
        {
            "stocks": codes,
            "method": str(arguments.get("method") or "equal_weight"),
            "lookback_days": bounded_int(arguments.get("lookback_days"), default=252, minimum=1, maximum=10000),
            "max_weight": bounded_float(
                dict(arguments.get("risk_limits") or {}).get("max_weight") or arguments.get("max_weight"),
                default=0.35,
                minimum=0.0,
                maximum=1.0,
            ),
        },
    )
    resolved_weights = _weights_from_optimization(optimization, codes) or [
        bounded_float(item, default=0.0, minimum=0.0, maximum=1.0) for item in weights
    ]
    holdings = [{"code": code, "weight": resolved_weights[index]} for index, code in enumerate(codes)]
    risk = await _call_registered(
        "akshare_mcp.tools.portfolio",
        "analyze_portfolio_risk",
        {"holdings": holdings, "lookback_days": bounded_int(arguments.get("lookback_days"), default=252, minimum=1, maximum=10000)},
    )
    stress = await _call_registered(
        "akshare_mcp.tools.portfolio",
        "stress_test_portfolio",
        {"holdings": holdings, "scenarios": arguments.get("stress_scenarios")},
    )
    barra = None
    if bool(arguments.get("include_barra", True)):
        barra = await _call_registered(
            "akshare_mcp.tools.portfolio",
            "analyze_portfolio_risk_barra",
            {"holdings": holdings, "lookback_days": bounded_int(arguments.get("lookback_days"), default=252, minimum=1, maximum=10000)},
        )
    status = "completed" if risk.get("success") is not False and stress.get("success") is not False else "partial"
    return _envelope(
        True,
        data={
            "status": status,
            "codes": codes,
            "holdings": holdings,
            "optimization": optimization,
            "risk": risk,
            "stress": stress,
            "barra": barra,
        },
        tool_name=tool_name,
    )


def _stage(name: str, status: str, output: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "output": output, "error": error}


def _status_from_output(output: Any, default: str = "partial") -> str:
    if isinstance(output, dict):
        return str(output.get("status") or default)
    return default


def _report(research_id: str, arguments: dict[str, Any], stages: list[dict[str, Any]], status: str) -> dict[str, Any]:
    codes = _codes(arguments)
    factors = _factors(arguments)
    return {
        "object": "aiask.quant_research_report",
        "research_id": research_id,
        "status": status,
        "summary": {
            "universe_size": len(codes),
            "factor_count": len(factors),
            "benchmark": arguments.get("benchmark") or "000300",
            "failed_stage": next((item["name"] for item in stages if item["status"] in {"failed", "blocked"}), None),
        },
        "data_window": {
            "start_date": arguments.get("start_date"),
            "end_date": arguments.get("end_date"),
            "benchmark": arguments.get("benchmark") or "000300",
        },
        "universe": codes,
        "factor_evidence": next((item.get("output") for item in stages if item["name"] == "factor_validation"), None),
        "backtest_assumptions": {
            "cost_bps": bounded_float(arguments.get("cost_bps"), default=3.0, minimum=0.0, maximum=10000.0),
            "slippage_bps": bounded_float(arguments.get("slippage_bps"), default=1.0, minimum=0.0, maximum=10000.0),
            "rebalance_frequency": arguments.get("rebalance_frequency") or "monthly",
        },
        "backtest": next((item.get("output") for item in stages if item["name"] == "backtest_suite"), None),
        "portfolio_risk": next((item.get("output") for item in stages if item["name"] == "portfolio_risk"), None),
        "strategy_factory": next((item.get("output") for item in stages if item["name"] == "strategy_factory_review"), None),
        "limitations": [
            "Results depend on local database freshness and configured market data providers.",
            "Backtest results are historical simulations and may not represent future performance.",
            "Strategy factory review is read-only unless an explicit durable intent is confirmed.",
        ],
        "disclaimer": DISCLAIMER,
        "stages": stages,
    }


async def quant_research_run(arguments: dict[str, Any], *, store: QuantResearchStore | None = None) -> dict[str, Any]:
    store = store or QuantResearchStore()
    rid = str(arguments.get("research_id") or "").strip() or new_research_id()
    stages: list[dict[str, Any]] = []
    stages.append(_stage("definition", "completed", {"universe": _codes(arguments), "factors": _factors(arguments)}))

    gate = await quant_data_gate(arguments)
    gate_data = gate.get("data") if isinstance(gate.get("data"), dict) else {}
    gate_status = "completed" if gate_data.get("ready") else "blocked"
    stages.append(_stage("data_gate", gate_status, gate_data, None if gate_data.get("ready") else str(gate_data.get("blocking_reason") or "")))
    if not gate_data.get("ready"):
        status = "blocked"
        report = _report(rid, arguments, stages, status)
        item = store.upsert(research_id=rid, status=status, payload={"arguments": arguments, "stages": stages}, report=report)
        return _envelope(True, data={"research": item}, tool_name="agent_quant_research_run")

    factors = await factor_validation(arguments)
    factor_data = factors.get("data")
    stages.append(_stage("factor_validation", _status_from_output(factor_data), factor_data))

    backtest = await backtest_suite(arguments)
    backtest_data = backtest.get("data")
    stages.append(_stage("backtest_suite", _status_from_output(backtest_data), backtest_data))

    risk = await portfolio_risk(arguments)
    risk_data = risk.get("data")
    stages.append(_stage("portfolio_risk", _status_from_output(risk_data), risk_data))

    review_payload = None
    strategy_id = str(arguments.get("strategy_id") or "").strip()
    if strategy_id or bool(arguments.get("include_strategy_review", True)):
        if strategy_id:
            review_payload = await strategy_factory_adapter.strategy_review_snapshot({"strategy_id": strategy_id, "limit": 20})
        else:
            review_payload = await strategy_factory_adapter.factory_status({"recent_run_limit": 5, "_timeout_seconds": 5})
        stages.append(_stage("strategy_factory_review", "completed" if review_payload.get("success") is not False else "partial", review_payload))

    status = "completed" if all(item["status"] in {"completed", "passed"} for item in stages if item["name"] != "strategy_factory_review") else "partial"
    report = _report(rid, arguments, stages, status)
    item = store.upsert(research_id=rid, status=status, payload={"arguments": arguments, "stages": stages}, report=report)
    return _envelope(True, data={"research": item}, tool_name="agent_quant_research_run")
