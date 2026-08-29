"""Quant manager AutoML: feature engineering, model training, anchor factor selection."""

import logging

import numpy as np

from .quant_mgr_helpers import (
    _clip,
    _compute_alternative_factors_for_code,
    _rank_transform,
    _safe_float,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AutoML feature engineering
# ---------------------------------------------------------------------------


def _build_automl_features(closes: list[float], volumes: list[float], idx: int) -> dict[str, float] | None:
    if idx < 30 or idx >= len(closes):
        return None
    px = closes[idx]
    if px <= 0:
        return None

    p5 = closes[idx - 5]
    p10 = closes[idx - 10]
    p20 = closes[idx - 20]
    if p5 <= 0 or p10 <= 0 or p20 <= 0:
        return None

    ret_20 = []
    for j in range(idx - 19, idx + 1):
        prev = closes[j - 1]
        if prev <= 0:
            continue
        ret_20.append((closes[j] - prev) / prev)
    vol_20 = float(np.std(ret_20)) if len(ret_20) > 1 else 0.0

    ma5 = float(np.mean(closes[idx - 4: idx + 1]))
    ma20 = float(np.mean(closes[idx - 19: idx + 1]))
    ma_ratio = ((ma5 / ma20) - 1.0) if ma20 > 0 else 0.0

    gains = 0.0
    losses = 0.0
    for j in range(idx - 14 + 1, idx + 1):
        chg = closes[j] - closes[j - 1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    avg_gain = gains / 14.0
    avg_loss = losses / 14.0
    if avg_loss <= 1e-12:
        rsi_14 = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_14 = 100.0 - (100.0 / (1.0 + rs))

    v5 = float(np.mean(volumes[idx - 4: idx + 1])) if idx >= 4 else 0.0
    v20 = float(np.mean(volumes[idx - 19: idx + 1])) if idx >= 19 else 0.0
    volume_ratio = (v5 / v20) if v20 > 0 else 1.0

    return {
        "mom_5d": float((px - p5) / p5),
        "mom_10d": float((px - p10) / p10),
        "mom_20d": float((px - p20) / p20),
        "volatility_20d": vol_20,
        "ma5_ma20_ratio": float(ma_ratio),
        "rsi_14": float((rsi_14 - 50.0) / 50.0),
        "volume_ratio_5_20": float(volume_ratio - 1.0),
    }


# ---------------------------------------------------------------------------
# AutoML dataset builder
# ---------------------------------------------------------------------------


async def _build_automl_dataset(
    db,
    codes: list[str],
    horizon_days: int,
    lookback_bars: int,
    include_alternative: bool,
    alt_lookback_days: int,
) -> tuple[list[dict], dict]:
    records: list[dict] = []
    stats = {
        "input_codes": len(codes),
        "processed_codes": 0,
        "skipped_no_kline": 0,
        "sample_count": 0,
    }
    fetch_bars = max(lookback_bars + horizon_days + 40, 120)

    for code in codes:
        klines = await db.get_klines(code, limit=fetch_bars)
        if not klines or len(klines) < max(60, horizon_days + 40):
            stats["skipped_no_kline"] += 1
            continue

        ordered = list(reversed([k for k in klines if isinstance(k, dict)]))  # oldest -> newest
        closes = [_safe_float(k.get("close"), 0.0) for k in ordered]
        volumes = [_safe_float(k.get("volume"), 0.0) for k in ordered]
        dates = [str(k.get("date") or k.get("trade_date") or "") for k in ordered]
        if len(closes) < max(60, horizon_days + 40):
            stats["skipped_no_kline"] += 1
            continue

        latest_financial = None
        try:
            financials = await db.get_financials(code, limit=1)
            if isinstance(financials, list) and financials:
                latest_financial = financials[0] if isinstance(financials[0], dict) else None
            elif isinstance(financials, dict):
                latest_financial = financials
        except Exception:
            latest_financial = None
        latest_financial = latest_financial or {}

        pe = _safe_float(latest_financial.get("pe_ratio"), 0.0)
        pb = _safe_float(latest_financial.get("pb_ratio"), 0.0)
        roe = _safe_float(latest_financial.get("roe"), 0.0)
        debt_ratio = _safe_float(latest_financial.get("debt_ratio"), 0.0)
        static_features = {
            "value_pe_inv": float(1.0 / pe) if pe > 0 else 0.0,
            "value_pb_inv": float(1.0 / pb) if pb > 0 else 0.0,
            "quality_roe": float(roe),
            "quality_debt_neg": float(-debt_ratio),
        }

        alt_features = {}
        if include_alternative:
            try:
                alt, _ = await _compute_alternative_factors_for_code(
                    db=db,
                    code=code,
                    lookback_days=alt_lookback_days,
                    limit=10,
                )
                alt_features = {
                    "alt_sentiment": _safe_float(alt.get("sentiment", {}).get("score_raw"), 0.0),
                    "alt_event": _safe_float(alt.get("event", {}).get("score_raw"), 0.0),
                    "alt_capital": _safe_float(alt.get("capital_flow", {}).get("score_raw"), 0.0),
                    "alt_composite": _safe_float(alt.get("alternative_composite", {}).get("score_raw"), 0.0),
                }
            except Exception:
                alt_features = {}

        for idx in range(30, len(closes) - horizon_days):
            px = closes[idx]
            future_px = closes[idx + horizon_days]
            if px <= 0 or future_px <= 0:
                continue
            base_features = _build_automl_features(closes, volumes, idx)
            if not base_features:
                continue
            target = (future_px - px) / px
            features = {**base_features, **static_features, **alt_features}
            records.append(
                {
                    "code": code,
                    "date": dates[idx] or f"t_{idx}",
                    "features": features,
                    "target": float(target),
                }
            )

        stats["processed_codes"] += 1

    records.sort(key=lambda x: str(x.get("date") or ""))
    stats["sample_count"] = len(records)
    return records, stats


# ---------------------------------------------------------------------------
# AutoML model fitting
# ---------------------------------------------------------------------------


def _fit_automl_model(records: list[dict], top_k_features: int, train_ratio: float, max_feature_corr: float) -> dict:
    if len(records) < 80:
        return {"success": False, "error": f"insufficient sample size: {len(records)} < 80"}

    feature_names = sorted(
        {
            key
            for row in records
            for key in (row.get("features") or {}).keys()
            if isinstance(key, str)
        }
    )
    if not feature_names:
        return {"success": False, "error": "no feature columns"}

    x_all = np.array(
        [[_safe_float((row.get("features") or {}).get(name), 0.0) for name in feature_names] for row in records],
        dtype=np.float64,
    )
    y_all = np.array([_safe_float(row.get("target"), 0.0) for row in records], dtype=np.float64)

    split = int(len(records) * _clip(train_ratio, 0.55, 0.9))
    split = max(50, min(split, len(records) - 20))
    x_train, x_test = x_all[:split], x_all[split:]
    y_train, y_test = y_all[:split], y_all[split:]
    if len(y_test) < 20:
        return {"success": False, "error": "test set too small"}

    mu = np.mean(x_train, axis=0)
    sigma = np.std(x_train, axis=0)
    sigma[sigma < 1e-12] = 1.0
    x_train_z = (x_train - mu) / sigma
    x_test_z = (x_test - mu) / sigma

    corr_scores = []
    for i, name in enumerate(feature_names):
        x_col = x_train_z[:, i]
        std_col = float(np.std(x_col))
        if std_col < 1e-8:
            continue
        corr = float(np.corrcoef(x_col, y_train)[0, 1])
        if np.isnan(corr):
            continue
        corr_scores.append((name, i, corr, abs(corr)))

    if not corr_scores:
        return {"success": False, "error": "all features degenerated"}

    corr_scores.sort(key=lambda x: x[3], reverse=True)
    selected: list[tuple[str, int, float]] = []
    for name, idx, corr, _ in corr_scores:
        allow = True
        for _, prev_idx, _ in selected:
            c = float(np.corrcoef(x_train_z[:, idx], x_train_z[:, prev_idx])[0, 1])
            if np.isnan(c):
                continue
            if abs(c) >= _clip(max_feature_corr, 0.5, 0.99):
                allow = False
                break
        if allow:
            selected.append((name, idx, corr))
        if len(selected) >= max(2, int(top_k_features)):
            break

    if len(selected) < 2:
        return {"success": False, "error": "not enough orthogonal features"}

    selected_idx = [x[1] for x in selected]
    selected_names = [x[0] for x in selected]
    raw_weights = np.array([x[2] for x in selected], dtype=np.float64)
    norm = np.sum(np.abs(raw_weights))
    if norm < 1e-8:
        raw_weights = np.ones_like(raw_weights) / len(raw_weights)
    else:
        raw_weights = raw_weights / norm

    x_test_sel = x_test_z[:, selected_idx]
    pred_linear = x_test_sel @ raw_weights
    rank_stack = np.vstack([_rank_transform(x_test_sel[:, j]) * np.sign(raw_weights[j]) for j in range(x_test_sel.shape[1])])
    pred_rank = np.mean(rank_stack, axis=0)
    pred = 0.6 * pred_linear + 0.4 * pred_rank

    ic = float(np.corrcoef(pred, y_test)[0, 1]) if len(y_test) > 2 else 0.0
    if np.isnan(ic):
        ic = 0.0
    hit_rate = float(np.mean((pred > 0) == (y_test > 0)))
    q = max(1, int(0.2 * len(pred)))
    order = np.argsort(pred)
    top_idx = order[-q:]
    bot_idx = order[:q]
    top_ret = float(np.mean(y_test[top_idx])) if len(top_idx) else 0.0
    bot_ret = float(np.mean(y_test[bot_idx])) if len(bot_idx) else 0.0
    long_short = float(top_ret - bot_ret)

    threshold_records = []
    for threshold in [0.0, float(np.percentile(pred, 60)), float(np.percentile(pred, 75))]:
        mask = pred >= threshold
        n = int(np.sum(mask))
        if n == 0:
            threshold_records.append(
                {"threshold": float(threshold), "sample_count": 0, "hit_rate": None, "avg_forward_return": None}
            )
            continue
        y_sel = y_test[mask]
        threshold_records.append(
            {
                "threshold": float(threshold),
                "sample_count": n,
                "hit_rate": float(np.mean(y_sel > 0)),
                "avg_forward_return": float(np.mean(y_sel)),
            }
        )

    return {
        "success": True,
        "selected_features": selected_names,
        "feature_weights": {name: float(w) for name, w in zip(selected_names, raw_weights)},
        "feature_importance_abs_corr": [
            {"feature": name, "train_corr": float(corr), "abs_corr": float(abs(corr))}
            for name, _, corr in selected
        ],
        "metrics": {
            "test_ic": float(ic),
            "hit_rate": float(hit_rate),
            "top_quantile_return": float(top_ret),
            "bottom_quantile_return": float(bot_ret),
            "long_short_return": float(long_short),
            "test_sample_count": int(len(y_test)),
        },
        "threshold_backtest": threshold_records,
        "train_test_split": {
            "train_samples": int(len(y_train)),
            "test_samples": int(len(y_test)),
            "train_ratio": float(split / len(records)),
        },
    }


# ---------------------------------------------------------------------------
# Anchor factor selection
# ---------------------------------------------------------------------------


def _select_anchor_factor(selected_features: list[str]) -> str:
    labels = [str(x).lower() for x in selected_features]
    if any("mom" in x for x in labels):
        return "momentum"
    if any("vol" in x for x in labels):
        return "volatility"
    if any("value" in x or "pe" in x or "pb" in x for x in labels):
        return "value"
    if any("quality" in x or "roe" in x or "debt" in x for x in labels):
        return "quality"
    return "momentum"
