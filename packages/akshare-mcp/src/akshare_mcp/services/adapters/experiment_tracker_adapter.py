"""Experiment tracking adapter.

Provides ``ExperimentTrackerAdapter`` interface and two implementations:

1. ``BuiltinTrackerAdapter`` — uses the existing ``artifact_registry`` as
   storage backend for experiment runs, metrics, and artifacts.
2. ``MlflowTrackerAdapter`` — wraps MLflow tracking API when installed.

Usage::

    tracker = get_experiment_tracker()
    run_id = tracker.log_run("my_experiment", params={"lr": 0.01})
    tracker.log_metric(run_id, "accuracy", 0.95)
    run = tracker.get_run(run_id)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


# ── Abstract interface ────────────────────────────────────────────────────────

class ExperimentTrackerAdapter(ABC):
    """Interface for experiment tracking adapters."""

    @abstractmethod
    def log_run(
        self,
        experiment_name: str,
        *,
        params: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> str:
        """Start a new experiment run. Returns run_id."""
        ...

    @abstractmethod
    def log_metric(
        self,
        run_id: str,
        key: str,
        value: float,
        *,
        step: int | None = None,
    ) -> None:
        """Log a metric for a run."""
        ...

    @abstractmethod
    def log_artifact(
        self,
        run_id: str,
        artifact_key: str,
        artifact_data: dict[str, Any],
    ) -> None:
        """Log an artifact (dict) for a run."""
        ...

    @abstractmethod
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Retrieve a run by ID."""
        ...

    @abstractmethod
    def list_runs(
        self,
        experiment_name: str | None = None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List runs, optionally filtered by experiment name."""
        ...

    @abstractmethod
    def backend_name(self) -> str:
        ...


# ── Builtin implementation ────────────────────────────────────────────────────

class BuiltinTrackerAdapter(ExperimentTrackerAdapter):
    """In-memory experiment tracker using artifact_registry as persistence.

    Maintains runs in memory for fast access and can optionally persist
    to the existing artifact_registry.
    """

    def __init__(self, max_runs: int = 500) -> None:
        self._max = max(10, int(max_runs))
        self._runs: dict[str, dict[str, Any]] = {}
        self._run_order: list[str] = []

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def log_run(
        self,
        experiment_name: str,
        *,
        params: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> str:
        run_id = f"run-{uuid4().hex[:12]}"
        run = {
            "run_id": run_id,
            "experiment_name": str(experiment_name or "default"),
            "params": dict(params or {}),
            "tags": dict(tags or {}),
            "metrics": {},
            "metric_history": {},
            "artifacts": {},
            "status": "running",
            "started_at": self._now_iso(),
            "completed_at": None,
        }
        self._runs[run_id] = run
        self._run_order.append(run_id)

        # Evict oldest if over limit
        while len(self._run_order) > self._max:
            old_id = self._run_order.pop(0)
            self._runs.pop(old_id, None)

        return run_id

    def log_metric(
        self,
        run_id: str,
        key: str,
        value: float,
        *,
        step: int | None = None,
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run["metrics"][key] = float(value)
        history = run["metric_history"].setdefault(key, [])
        entry = {"value": float(value), "timestamp": self._now_iso()}
        if step is not None:
            entry["step"] = int(step)
        history.append(entry)

    def log_artifact(
        self,
        run_id: str,
        artifact_key: str,
        artifact_data: dict[str, Any],
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run["artifacts"][artifact_key] = artifact_data

    def complete_run(self, run_id: str, *, status: str = "completed") -> None:
        """Mark a run as completed."""
        run = self._runs.get(run_id)
        if run is not None:
            run["status"] = status
            run["completed_at"] = self._now_iso()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def list_runs(
        self,
        experiment_name: str | None = None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        runs = list(self._runs.values())
        if experiment_name:
            runs = [r for r in runs if r.get("experiment_name") == experiment_name]
        # Most recent first
        runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return runs[:limit]

    def backend_name(self) -> str:
        return "builtin"


# ── MLflow adapter (optional) ─────────────────────────────────────────────────

# P3-5.1 fix: 模块级 fallback 提示开关(诊断报告 §5.1)
# 历史问题:每次调用 backend_name 时若 mlflow 未装,只静默 fallback 到 builtin,运维不知该装 mlflow
# 修复:第一次实例化 MlflowTrackerAdapter 但 mlflow 不可用时,在 stderr 输出一次 WARNING
import logging
import sys as _sys

_mlflow_fallback_warned = False
_experiment_tracker_logger = logging.getLogger("akshare_mcp.experiment_tracker")


class MlflowTrackerAdapter(ExperimentTrackerAdapter):
    """Wraps MLflow tracking API when installed.

    Falls back to BuiltinTrackerAdapter if MLflow is not available.
    """

    def __init__(self) -> None:
        global _mlflow_fallback_warned
        self._available = False
        self._fallback = BuiltinTrackerAdapter()
        try:
            import mlflow  # noqa: F401
            self._available = True
        except ImportError:
            # P3-5.1 fix: 一次性 warning,避免重复刷屏,但运维能看到提示
            if not _mlflow_fallback_warned:
                _mlflow_fallback_warned = True
                msg = (
                    "[experiment_tracker] mlflow 未安装,实验追踪降级到 builtin in-memory tracker; "
                    "实验记录在进程重启后丢失。运维建议:pip install mlflow 后实现真持久化。"
                )
                try:
                    _experiment_tracker_logger.warning(msg)
                except Exception:
                    print(msg, file=_sys.stderr)

    def log_run(
        self,
        experiment_name: str,
        *,
        params: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> str:
        if not self._available:
            return self._fallback.log_run(experiment_name, params=params, tags=tags)

        import mlflow
        mlflow.set_experiment(experiment_name)
        run = mlflow.start_run()
        if params:
            mlflow.log_params(params)
        if tags:
            mlflow.set_tags(tags)
        return run.info.run_id

    def log_metric(
        self,
        run_id: str,
        key: str,
        value: float,
        *,
        step: int | None = None,
    ) -> None:
        if not self._available:
            self._fallback.log_metric(run_id, key, value, step=step)
            return
        import mlflow
        mlflow.log_metric(key, value, step=step)

    def log_artifact(
        self,
        run_id: str,
        artifact_key: str,
        artifact_data: dict[str, Any],
    ) -> None:
        if not self._available:
            self._fallback.log_artifact(run_id, artifact_key, artifact_data)
            return
        # MLflow artifacts are file-based; for dict data, use log_dict
        import mlflow
        mlflow.log_dict(artifact_data, f"{artifact_key}.json")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if not self._available:
            return self._fallback.get_run(run_id)
        import mlflow
        try:
            run = mlflow.get_run(run_id)
            return {
                "run_id": run.info.run_id,
                "experiment_name": run.info.experiment_id,
                "params": dict(run.data.params),
                "metrics": dict(run.data.metrics),
                "tags": dict(run.data.tags),
                "status": run.info.status,
                "started_at": str(run.info.start_time),
                "completed_at": str(run.info.end_time),
            }
        except Exception:
            return None

    def list_runs(
        self,
        experiment_name: str | None = None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self._available:
            return self._fallback.list_runs(experiment_name, limit=limit)
        import mlflow
        try:
            if experiment_name:
                exp = mlflow.get_experiment_by_name(experiment_name)
                if exp:
                    runs = mlflow.search_runs(
                        experiment_ids=[exp.experiment_id],
                        max_results=limit,
                        output_format="list",
                    )
                    return [{"run_id": r.info.run_id, "metrics": dict(r.data.metrics)} for r in runs]
            return []
        except Exception:
            return self._fallback.list_runs(experiment_name, limit=limit)

    def backend_name(self) -> str:
        return "mlflow" if self._available else "builtin_fallback"


# ── Factory ───────────────────────────────────────────────────────────────────

_default_tracker: ExperimentTrackerAdapter | None = None


def get_experiment_tracker(prefer_mlflow: bool = True) -> ExperimentTrackerAdapter:
    """Get the best available experiment tracker.

    Parameters
    ----------
    prefer_mlflow:
        If True, try MLflow first, fallback to builtin.
    """
    global _default_tracker
    if _default_tracker is not None:
        return _default_tracker

    if prefer_mlflow:
        _default_tracker = MlflowTrackerAdapter()
        return _default_tracker

    _default_tracker = BuiltinTrackerAdapter()
    return _default_tracker
