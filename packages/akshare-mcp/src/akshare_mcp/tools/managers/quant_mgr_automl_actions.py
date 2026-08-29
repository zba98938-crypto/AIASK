"""AutoML discovery handler for quant_manager."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4


async def handle_automl_discovery(
    *,
    kw: dict[str, Any],
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    build_automl_dataset_fn: Callable[..., Any],
    fit_automl_model_fn: Callable[..., Any],
    select_anchor_factor_fn: Callable[[list[str]], str],
    run_factor_oos_validation_fn: Callable[..., Any],
    register_artifact_fn: Callable[[dict], Any],
    clip_fn: Callable[[float, float, float], float],
    db: Any,
) -> dict:
    codes = kw.get("codes", [])
    if not isinstance(codes, list) or not codes:
        return fail("需要提供股票列表（codes）")

    horizon_days = int(kw.get("horizon_days", 10) or 10)
    lookback_bars = int(kw.get("lookback_bars", 160) or 160)
    top_k_features = int(kw.get("top_k_features", 6) or 6)
    train_ratio = float(kw.get("train_ratio", 0.7) or 0.7)
    max_feature_corr = float(kw.get("max_feature_corr", 0.85) or 0.85)
    include_alternative = bool(kw.get("include_alternative", True))
    alt_lookback_days = int(kw.get("alt_lookback_days", 30) or 30)
    persist_artifact = bool(kw.get("persist_artifact", True))
    run_anchor_oos = bool(kw.get("run_anchor_oos", True))

    records, dataset_stats = await build_automl_dataset_fn(
        db=db,
        codes=codes,
        horizon_days=max(3, min(30, horizon_days)),
        lookback_bars=max(120, min(500, lookback_bars)),
        include_alternative=include_alternative,
        alt_lookback_days=max(7, min(120, alt_lookback_days)),
    )
    model_res = fit_automl_model_fn(
        records=records,
        top_k_features=max(2, min(15, top_k_features)),
        train_ratio=clip_fn(train_ratio, 0.55, 0.9),
        max_feature_corr=clip_fn(max_feature_corr, 0.5, 0.99),
    )
    if not model_res.get("success"):
        return fail(
            model_res.get("error", "automl failed"),
            source_chain=["db.get_klines", "numpy.feature_selection"],
        )

    selected_features = model_res.get("selected_features", [])
    anchor_factor = select_anchor_factor_fn(selected_features)
    anchor_oos = None
    if run_anchor_oos:
        try:
            anchor_oos = await run_factor_oos_validation_fn(
                codes=codes,
                factor=anchor_factor,
                factor_lookback=20,
                forward_period=max(5, min(20, horizon_days)),
                panel_periods=180,
                wf_train_window=60,
                wf_test_window=20,
                wf_step=20,
                kfold_n_folds=5,
                kfold_purge_gap=5,
                bootstrap_n=600,
                bootstrap_confidence=0.95,
            )
        except Exception as exc:
            anchor_oos = fail(f"anchor_oos_failed: {exc}")

    artifact_id = f"quant_automl_{int(time.time())}_{uuid4().hex[:8]}"
    output = {
        "artifact_id": artifact_id,
        "codes": codes,
        "dataset_stats": dataset_stats,
        "selected_features": selected_features,
        "feature_weights": model_res.get("feature_weights", {}),
        "feature_importance_abs_corr": model_res.get("feature_importance_abs_corr", []),
        "metrics": model_res.get("metrics", {}),
        "threshold_backtest": model_res.get("threshold_backtest", []),
        "train_test_split": model_res.get("train_test_split", {}),
        "robust_constraints": {
            "min_sample_required": 80,
            "max_feature_corr": clip_fn(max_feature_corr, 0.5, 0.99),
            "passed": bool(dataset_stats.get("sample_count", 0) >= 80),
        },
        "oos_anchor": {
            "factor": anchor_factor,
            "result": anchor_oos,
        },
        "params": {
            "horizon_days": horizon_days,
            "lookback_bars": lookback_bars,
            "top_k_features": top_k_features,
            "train_ratio": train_ratio,
            "include_alternative": include_alternative,
            "alt_lookback_days": alt_lookback_days,
        },
    }

    if persist_artifact:
        register_artifact_fn(
            {
                "artifact_id": artifact_id,
                "strategy": "quant_automl_discovery",
                "strategy_version": "p2.v1",
                "code": ",".join(codes[:5]),
                "payload": output,
                "created_at": datetime.now().isoformat(),
            }
        )

    return ok(
        output,
        source_chain=[
            "db.get_klines",
            "db.get_financials",
            "tools.news.*",
            "tools.fund_flow.*",
            "numpy.feature_selection_ensemble",
            "quant.run_factor_oos_validation(anchor)",
            "services.artifact_registry",
        ],
    )
