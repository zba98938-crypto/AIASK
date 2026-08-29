"""Execution manager: TWAP/VWAP planning with lifecycle tracking and cost transparency."""

from __future__ import annotations

import asyncio
import logging
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from ...services.artifact_registry import (
    get_artifact_async,
    list_artifacts_async,
    register_artifact_async,
)
from ...utils import fail, ok, normalize_code
from .compliance_manager import evaluate_order_compliance
from ..manager_protocol import normalize_manager_payload
from ..risk_guard import audit_event
from . import _execution_manager_support as _execution_manager_support_mod

from ._execution_manager_support import (
    _append_event,
    _build_cost_model,
    _build_soft_gate_warnings,
    _create_task,
    _enrich_kwargs_with_realtime,
    _ensure_runtime_config_loaded,
    _load_all_tasks,
    _load_task,
    _normalize_kwargs,
    _now_iso,
    _persist_runtime_config,
    _persist_task,
    _profile_distribution,
    _reset_runtime_config_state,
    _refresh_and_persist_tasks,
    _run_pretrade_gate,
    _set_config_impl,
    _set_config_result,
    _soft_gate_config_view,
    _summary_aggregates,
    _task_brief,
)

_EXECUTION_TASKS: dict[str, dict[str, Any]] = {}
_EXECUTION_TASK_ARTIFACT_STRATEGY = "execution_task"
_EXECUTION_TASK_ARTIFACT_VERSION = "v1"
_EXECUTION_CONFIG_ARTIFACT_ID = "execution_manager:soft_gate_config"
_EXECUTION_CONFIG_ARTIFACT_STRATEGY = "execution_manager_config"
_EXECUTION_ARTIFACT_SCAN_LIMIT = 400
_RUNTIME_CONFIG_LOADED = False

logger = logging.getLogger(__name__)


_SOFT_GATE_PROFILES: dict[str, dict[str, float | int]] = {
    "conservative": {
        "max_order_shares": 500_000,
        "max_slice_shares": 100_000,
        "min_duration_minutes": 10,
        "max_cost_ratio": 0.003,
    },
    "balanced": {
        "max_order_shares": 1_000_000,
        "max_slice_shares": 200_000,
        "min_duration_minutes": 5,
        "max_cost_ratio": 0.005,
    },
    "aggressive": {
        "max_order_shares": 2_000_000,
        "max_slice_shares": 400_000,
        "min_duration_minutes": 3,
        "max_cost_ratio": 0.008,
    },
}

_SOFT_GATE_THRESHOLD_KEYS = (
    "max_order_shares",
    "max_slice_shares",
    "min_duration_minutes",
    "max_cost_ratio",
)


_SOFT_GATE_RUNTIME_CONFIG: dict[str, Any] = {
    "default_profile": "balanced",
    "default_threshold_overrides": {},
    "code_profiles": {},
}

from ._execution_manager_support import *


def _sync_execution_support_overrides() -> None:
    """Keep execution support helpers aligned with execution_manager monkeypatches."""
    _execution_manager_support_mod.register_artifact_async = register_artifact_async
    _execution_manager_support_mod.get_artifact_async = get_artifact_async
    _execution_manager_support_mod.list_artifacts_async = list_artifacts_async
    _execution_manager_support_mod.evaluate_order_compliance = evaluate_order_compliance
    _execution_manager_support_mod.audit_event = audit_event
    _execution_manager_support_mod._build_cost_model = _build_cost_model
    _execution_manager_support_mod._enrich_kwargs_with_realtime = _enrich_kwargs_with_realtime

def register_execution_manager(mcp):
    """Register execution manager tool."""
    _reset_runtime_config_state()

    @mcp.tool()
    async def execution_manager(action: str, params: dict | None = None, kwargs: Any = None, dry_run: bool = False) -> dict:
        """
        Execution manager with unified action + kwargs protocol.
        Supports structured ``params`` in addition to legacy ``kwargs`` payloads.

        Actions:
        - help
        - get_config
        - set_config
        - twap
        - vwap
        - list
        - summary
        - update (optional manual status update)

        Args by action:
        - set_config:
          - default_profile(optional)
          - default_threshold_overrides(optional, dict)
          - merge_default_threshold_overrides(optional, bool, 默认 false)
          - remove_default_threshold_keys(optional, list[str])
          - code_profiles(optional, dict[code->profile|null])
          - merge_code_profiles(optional, bool, 默认 false)
          - remove_code_profiles(optional, list[str])
        - twap/vwap: code, total_shares|total_quantity, duration|duration_minutes,
          slices(optional), reference_price(optional), artifact_id(optional),
          soft_gate_profile(optional: conservative/balanced/aggressive, 默认 balanced),
          max_order_shares/max_slice_shares/min_duration_minutes/max_cost_ratio(optional, 软告警阈值，显式参数优先于 profile 默认值),
          market_session(optional, continuous/normal/intraday 为低风险；其他值触发时段风险告警),
          avg_minute_volume(optional, 配合 max_participation_rate 默认0.2 做参与率软告警),
          top_of_book_volume(optional, 配合 max_top_book_ratio 默认0.3 做盘口冲击软告警)
        - summary: task_id(optional)
        - update: task_id, status, note(optional)

        Return highlights:
        - get_config/set_config: 软闸门运行时配置快照（支持增量更新与删除）
        - twap/vwap: 额外返回 warnings(list) 与 soft_gate(dict, non-blocking, 含 profile/thresholds)
        - list: 每条任务摘要包含 warning_count/has_high_severity
        - summary: 支持查看任务级 warnings/soft_gate 或全局 warning 汇总（含 profile/severity 分布）

        Examples:
        - execution_manager(action="set_config", kwargs='{"default_profile":"balanced","code_profiles":{"600519":"conservative"}}')
        - execution_manager(action="set_config", kwargs='{"merge_code_profiles":true,"code_profiles":{"000001":"aggressive"}}')
        - execution_manager(action="set_config", kwargs='{"merge_code_profiles":true,"code_profiles":{"000001":null}}')
        - execution_manager(action="get_config", kwargs="{}")
        - execution_manager(action="twap", kwargs='{"code":"600519","total_quantity":1000,"duration_minutes":60,"artifact_id":"art_demo_001"}')
        - execution_manager(action="vwap", kwargs='{"code":"000001","total_quantity":1500000,"duration_minutes":3}')
        - execution_manager(action="list", kwargs="{}")
        - execution_manager(action="summary", kwargs='{"task_id":"exec_xxx"}')
        """
        try:
            _sync_execution_support_overrides()
            kwargs = normalize_manager_payload(params=params, kwargs=kwargs)
            kwargs = _normalize_kwargs(kwargs)
            dry_run = dry_run or bool(kwargs.get("dry_run", False))
            if kwargs.get("code"):
                try:
                    kwargs["code"] = normalize_code(kwargs.get("code"))
                except Exception:
                    kwargs["code"] = kwargs.get("code")
            await _ensure_runtime_config_loaded()
            supported_actions = {
                "twap": "TWAP execution planning (time-weighted average price)",
                "vwap": "VWAP execution planning (volume-weighted average price)",
                "list": "list execution tasks with lifecycle status",
                "summary": "task or global summary, including cost assumptions",
                "update": "manual status update for simulation/reconciliation",
                "get_config": "get runtime soft-gate configuration",
                "set_config": "set runtime soft-gate configuration",
                "help": "show help information",
            }

            if action == "help":
                return ok({"supported_actions": supported_actions})

            if action == "get_config":
                return ok({"soft_gate_config": _soft_gate_config_view()})

            if action == "set_config":
                ok_flag, err = _set_config_impl(kwargs)
                if not ok_flag:
                    return fail(str(err))
                await _persist_runtime_config()
                return ok(_set_config_result())

            if action in {"twap", "vwap"}:
                code = kwargs.get("code")
                total_shares = kwargs.get("total_shares")
                duration = kwargs.get("duration", 60) or 60
                direction = str(kwargs.get("direction") or kwargs.get("side") or "buy").strip().lower()

                if not code:
                    return fail("code is required")
                if total_shares is None:
                    return fail("total_shares or total_quantity is required")

                try:
                    total_shares = int(total_shares)
                    duration = int(duration)
                except Exception:
                    return fail("total_shares and duration must be integers")

                if total_shares <= 0:
                    return fail("total_shares must be > 0")
                if duration <= 0:
                    return fail("duration must be > 0")

                if action == "twap":
                    slices = kwargs.get("slices")
                    slices = int(slices) if slices is not None else max(1, duration // 5)
                else:
                    slices = kwargs.get("slices")
                    slices = int(slices) if slices is not None else max(1, duration // 10)

                if slices <= 0:
                    slices = 1

                # --- P1-c: 实时行情自动填充 reference_price / avg_minute_volume ---
                _enrich_kwargs_with_realtime(str(code), kwargs)

                # Build cost and gates before creating any execution task artifact.
                cost_model = _build_cost_model(kwargs, total_shares)

                warnings, thresholds = _build_soft_gate_warnings(
                    kwargs=kwargs,
                    total_shares=total_shares,
                    duration=duration,
                    slices=slices,
                    cost_model=cost_model,
                )

                # --- 硬闸门（hard gate）：合规检查，违规阻断 ---
                gate = _run_pretrade_gate(
                    code=str(code),
                    direction=direction,
                    total_shares=total_shares,
                    kwargs=kwargs,
                    soft_warnings=warnings,
                )

                if gate["compliance_blocked"]:
                    return fail(
                        f"合规闸门阻断: {'; '.join(gate['compliance_violations'])}",
                        data={
                            "code": str(code),
                            "direction": direction,
                            "total_shares": total_shares,
                            "compliance_gate": gate,
                            "soft_warnings": warnings,
                        },
                    )

                task = _create_task(action.upper(), str(code), total_shares, duration, slices, kwargs)
                task["cost_model"] = cost_model
                soft_gate = {
                    "enabled": True,
                    "blocking": False,
                    "profile": thresholds["profile"],
                    "thresholds": {
                        "max_order_shares": thresholds["max_order_shares"],
                        "max_slice_shares": thresholds["max_slice_shares"],
                        "min_duration_minutes": thresholds["min_duration_minutes"],
                        "max_cost_ratio": thresholds["max_cost_ratio"],
                    },
                    "warning_count": len(warnings),
                    "has_high_severity": any(w.get("severity") == "high" for w in warnings),
                }
                task["pretrade_warnings"] = warnings
                task["soft_gate"] = soft_gate
                task["compliance_gate"] = gate
                task["dry_run"] = dry_run
                if not dry_run:
                    await _persist_task(task)
                else:
                    _EXECUTION_TASKS.pop(task.get("task_id", ""), None)

                return ok(
                    {
                        "algorithm": task["algorithm"],
                        "task_id": task["task_id"] if not dry_run else None,
                        "artifact_id": task.get("artifact_id") if not dry_run else None,
                        "code": task["code"],
                        "status": "dry_run_preview" if dry_run else task["status"],
                        "dry_run": dry_run,
                        "total_shares": task["total_shares"],
                        "total_quantity": task["total_quantity"],
                        "duration": task["plan"]["duration"],
                        "duration_minutes": task["plan"]["duration_minutes"],
                        "slices": task["plan"]["slices"],
                        "shares_per_slice": task["plan"]["shares_per_slice"],
                        "interval": task["plan"]["interval_minutes"],
                        "remainder_shares": task["plan"]["remainder_shares"],
                        "cost_model": task["cost_model"],
                        "warnings": warnings,
                        "soft_gate": soft_gate,
                        "compliance_gate": gate,
                        "lifecycle": task["lifecycle"] if not dry_run else [],
                    }
                )

            if action == "list":
                status_filter = str(kwargs.get("status", "")).strip().lower()
                tasks = await _load_all_tasks()
                await _refresh_and_persist_tasks(tasks)

                if status_filter:
                    tasks = [t for t in tasks if str(t.get("status", "")).lower() == status_filter]

                pending = [_task_brief(t) for t in tasks if t.get("status") in {"queued", "executing"}]
                completed = [_task_brief(t) for t in tasks if t.get("status") in {"completed", "failed"}]

                if not tasks:
                    return ok(
                        {
                            "message": "no execution tasks",
                            "tasks": [],
                            "pending_orders": [],
                            "completed_orders": [],
                            "count": 0,
                        }
                    )

                return ok(
                    {
                        "tasks": [_task_brief(t) for t in tasks],
                        "count": len(tasks),
                        "pending_orders": pending,
                        "completed_orders": completed,
                    }
                )

            if action == "summary":
                task_id = kwargs.get("task_id")
                tasks = await _load_all_tasks()
                await _refresh_and_persist_tasks(tasks)

                if task_id:
                    task = await _load_task(str(task_id))
                    if not task:
                        return fail(f"task not found: {task_id}")

                    warnings = task.get("pretrade_warnings") if isinstance(task.get("pretrade_warnings"), list) else []
                    soft_gate = task.get("soft_gate") if isinstance(task.get("soft_gate"), dict) else {
                        "enabled": False,
                        "blocking": False,
                        "warning_count": len(warnings),
                        "has_high_severity": any(w.get("severity") == "high" for w in warnings),
                    }

                    return ok(
                        {
                            "task": task,
                            "lifecycle_count": len(task.get("lifecycle", [])),
                            "estimated_cost_total": float(task.get("cost_model", {}).get("estimated", {}).get("total", 0.0)),
                            "warnings": warnings,
                            "soft_gate": soft_gate,
                        }
                    )

                if not tasks:
                    return ok(
                        {
                            "total_tasks": 0,
                            "by_status": {},
                            "estimated_total_cost": 0.0,
                            "warning_count": 0,
                            "high_severity_task_count": 0,
                            "soft_gate_profile_distribution": _profile_distribution([]),
                            "warnings_by_profile": {},
                            "warnings_by_severity": {"low": 0, "medium": 0, "high": 0},
                        }
                    )

                return ok(_summary_aggregates(tasks))

            if action == "update":
                task_id = str(kwargs.get("task_id") or "").strip()
                new_status = str(kwargs.get("status") or "").strip().lower()
                note = str(kwargs.get("note") or "manual update")

                if not task_id:
                    return fail("task_id is required")
                if new_status not in {"queued", "executing", "completed", "failed", "cancelled"}:
                    return fail("status must be one of queued/executing/completed/failed/cancelled")

                task = await _load_task(task_id)
                if not task:
                    return fail(f"task not found: {task_id}")

                task["status"] = new_status
                if new_status in {"completed", "failed", "cancelled"}:
                    task["completed_at"] = _now_iso()
                _append_event(task, new_status, note)
                await _persist_task(task)

                return ok({"task": task})

            return fail(f"Unknown action: {action}. Supported: {', '.join(supported_actions.keys())}")
        except Exception as e:
            return fail(str(e))
