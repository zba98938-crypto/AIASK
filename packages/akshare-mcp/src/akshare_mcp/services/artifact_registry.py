"""策略工件注册中心（P0）：DB 持久化 + 内存缓存。P2-3: 实验注册标准模板。"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .background_tasks import track_background_task

logger = logging.getLogger(__name__)

# ── 内存缓存（DB 不可用时降级） ──────────────────────────
_ARTIFACTS: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db():
    """延迟导入，避免循环依赖，并兼容上层 storage.get_db 注入。"""
    try:
        from ..storage import get_db
        return get_db()
    except Exception:
        return None


def _prepare_artifact_payload(artifact: dict) -> dict[str, Any]:
    aid = str((artifact or {}).get("artifact_id") or "").strip()
    if not aid:
        raise ValueError("artifact_id is required")

    payload = deepcopy(artifact)
    payload.setdefault("registered_at", _now_iso())
    payload["updated_at"] = _now_iso()
    return payload


def _cache_artifact(payload: dict[str, Any]) -> None:
    aid = str((payload or {}).get("artifact_id") or "").strip()
    if aid:
        _ARTIFACTS[aid] = deepcopy(payload)


# ── 公开 API ────────────────────────────────────────────

def register_artifact(artifact: dict) -> dict:
    """注册策略工件：优先写 DB，同时更新内存缓存。"""
    payload = _prepare_artifact_payload(artifact)
    aid = str(payload.get("artifact_id") or "").strip()
    _cache_artifact(payload)

    # 尝试异步写 DB
    db = _get_db()
    if db is not None and hasattr(db, "save_artifact"):
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                track_background_task(_safe_save(db, payload), name=f"artifact-save:{aid}")
        except RuntimeError:
            logger.debug("No running event loop; artifact %s cached in memory only", aid)

    return deepcopy(payload)


async def register_artifact_async(artifact: dict) -> dict:
    """异步注册策略工件：优先直写 DB，确保跨进程查询可见。"""
    payload = _prepare_artifact_payload(artifact)
    _cache_artifact(payload)

    db = _get_db()
    if db is not None and hasattr(db, "save_artifact"):
        await _safe_save(db, payload)

    return deepcopy(payload)


async def _safe_save(db, payload: dict) -> None:
    """安全写入 DB，失败不影响主流程。"""
    try:
        await db.save_artifact(payload)
    except Exception as exc:
        logger.warning("Failed to persist artifact %s to DB: %s",
                       payload.get("artifact_id"), exc)


def get_artifact(artifact_id: str) -> dict | None:
    """查询工件：先查内存缓存，缓存未命中则查 DB。"""
    aid = str(artifact_id or "").strip()
    if not aid:
        return None

    # 内存缓存命中
    item = _ARTIFACTS.get(aid)
    if item is not None:
        return deepcopy(item)

    # 尝试从 DB 加载
    db = _get_db()
    if db is not None and hasattr(db, "get_artifact_by_id"):
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                track_background_task(_safe_get(db, aid), name=f"artifact-get:{aid}")
        except RuntimeError:
            pass

    return None


async def _safe_get(db, artifact_id: str) -> dict | None:
    """从 DB 加载并回填缓存。"""
    try:
        row = await db.get_artifact_by_id(artifact_id)
        if row is not None:
            _ARTIFACTS[artifact_id] = deepcopy(row)
        return row
    except Exception as exc:
        logger.warning("Failed to load artifact %s from DB: %s", artifact_id, exc)
        return None


async def get_artifact_async(artifact_id: str) -> dict | None:
    """异步版本：先查缓存，再查 DB。"""
    aid = str(artifact_id or "").strip()
    if not aid:
        return None

    item = _ARTIFACTS.get(aid)
    if item is not None:
        return deepcopy(item)

    db = _get_db()
    if db is not None and hasattr(db, "get_artifact_by_id"):
        row = await _safe_get(db, aid)
        if row is not None:
            return deepcopy(row)

    return None


def _matches_strategy(row: dict[str, Any], strategy: str | None) -> bool:
    if strategy is None:
        return True
    target = str(strategy or "").strip().lower()
    if not target:
        return True
    return str((row or {}).get("strategy") or "").strip().lower() == target


def _summarize_artifact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": row.get("artifact_id"),
        "strategy_version": row.get("strategy_version"),
        "code": row.get("code"),
        "strategy": row.get("strategy"),
        "updated_at": row.get("updated_at"),
    }


def _filter_artifact_summaries(rows: list[dict] | None, strategy: str | None) -> list[dict]:
    return [
        dict(row)
        for row in list(rows or [])
        if isinstance(row, dict) and _matches_strategy(row, strategy)
    ]


def list_artifacts(limit: int = 20, strategy: str | None = None) -> list[dict]:
    """按更新时间倒序返回工件摘要（内存缓存）。"""
    rows = [row for row in _ARTIFACTS.values() if _matches_strategy(row, strategy)]
    rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    out = []
    for row in rows[: max(1, int(limit))]:
        out.append(_summarize_artifact_row(row))
    return out


async def list_artifacts_async(limit: int = 20, strategy: str | None = None) -> list[dict]:
    """异步版本：优先从 DB 获取。"""
    db = _get_db()
    if db is not None and hasattr(db, "list_artifacts_db"):
        try:
            if strategy is not None:
                try:
                    return await db.list_artifacts_db(limit, strategy=strategy)
                except TypeError:
                    rows = await db.list_artifacts_db(limit)
                    return _filter_artifact_summaries(rows, strategy)
            return await db.list_artifacts_db(limit)
        except Exception as exc:
            logger.warning("Failed to list artifacts from DB: %s", exc)

    return list_artifacts(limit, strategy=strategy)


# ── P2-3: 实验注册标准模板 ─────────────────────────────────

EXPERIMENT_SCHEMA = {
    "required": [
        "experiment_id",   # 唯一标识
        "hypothesis",      # 实验假设
        "method",          # 方法描述（如 IC分析/回测/OOS验证）
        "parameters",      # 参数字典
        "status",          # draft / running / completed / failed
    ],
    "optional": [
        "result",              # 实验结果
        "conclusion",          # 结论
        "author",              # 作者
        "tags",                # 标签列表
        "factor_name",         # 关联因子名
        "universe",            # 股票池描述
        "data_range",          # 数据区间
        "reproducibility_info",  # 可复现信息（数据版本、随机种子等）
    ],
    "valid_statuses": {"draft", "running", "completed", "failed"},
}


def validate_experiment(exp: dict) -> list[str]:
    """校验实验是否符合标准模板，返回错误列表（空=通过）。"""
    if not isinstance(exp, dict):
        return ["experiment must be a dict"]
    errors: list[str] = []
    for field in EXPERIMENT_SCHEMA["required"]:
        val = exp.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing required field: {field}")
    # parameters 必须是 dict
    params = exp.get("parameters")
    if params is not None and not isinstance(params, dict):
        errors.append("parameters must be a dict")
    # status 枚举校验
    status = exp.get("status")
    if status and status not in EXPERIMENT_SCHEMA["valid_statuses"]:
        errors.append(f"invalid status '{status}', must be one of {EXPERIMENT_SCHEMA['valid_statuses']}")
    # tags 如果存在必须是 list
    tags = exp.get("tags")
    if tags is not None and not isinstance(tags, list):
        errors.append("tags must be a list")
    return errors


def register_experiment(experiment: dict) -> dict:
    """校验并注册实验工件，返回完整注册记录。"""
    errors = validate_experiment(experiment)
    if errors:
        raise ValueError(f"实验模板校验失败: {'; '.join(errors)}")

    payload = deepcopy(experiment)
    # 统一元数据字段
    payload["artifact_type"] = "experiment"
    payload["artifact_id"] = payload.pop("experiment_id")
    payload.setdefault("created_at", _now_iso())
    payload.setdefault("tags", [])
    payload.setdefault("reproducibility_info", {})

    return register_artifact(payload)
