"""MCP_FIX_PLAN 第五批回归测试（FIX-39~43，findings_v3 剩余三类）。

- FIX-39 (F-N43-3): calibrate_probability_series 用 sklearn LR/Isotonic 直接拟合，不再恒失败降级
- FIX-40 (F-N22-3): should_i_buy score→recommendation 单调（context 覆盖最多下调一档）
- FIX-41 (F-N01-2): enrich_response_meta 上浮 data 内层 fallback_used/degraded
- FIX-42 (F-N45): run_skill 不再 execution==result 双份完整副本
- FIX-43 (F-N43-5): data_quality_workflow 空 records 返回 PARAM_ERROR（与 data_validation 一致）
"""

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "akshare_mcp"


# ── FIX-39: sklearn 校准后端不再恒失败 ───────────────────────────────

def test_fix39_sklearn_calibration_backend_works():
    pytest.importorskip("sklearn")
    import numpy as np
    from akshare_mcp.services.probability_calibration import calibrate_probability_series

    rng = np.random.default_rng(0)
    scores = list(np.linspace(0.1, 0.9, 40))
    ys = [float(rng.random() < s) for s in scores]

    r_sig = calibrate_probability_series(scores, ys, method="sigmoid")
    assert r_sig.fallback_used is False
    assert r_sig.backend_used == "sklearn_logistic_platt"

    r_iso = calibrate_probability_series(scores, ys, method="isotonic")
    assert r_iso.fallback_used is False
    assert r_iso.backend_used == "sklearn_isotonic_regression"


def test_fix39_no_legacy_calibratedclassifiercv():
    src = (_SRC / "services" / "probability_calibration.py").read_text(encoding="utf-8")
    # 不应再实例化易碎的 CalibratedClassifierCV 或定义假估计器（注释/文档提及不算）
    assert "CalibratedClassifierCV(" not in src
    assert "class _RawScoreEstimator" not in src
    # 应使用直接拟合路径
    assert "sklearn_logistic_platt" in src
    assert "sklearn_isotonic_regression" in src


# ── FIX-40: score→recommendation 单调 ────────────────────────────────

def test_fix40_recommendation_monotonic_static():
    src = (_SRC / "tools" / "_decision_buy.py").read_text(encoding="utf-8")
    assert "_REC_ORDER" in src
    assert "bounded_level" in src
    # 不再直接整段替换为 context_decision 的 recommendation
    assert 'recommendation = context_decision["recommendation"]\n            action_text = context_decision["action_text"]' not in src


# ── FIX-41: envelope 顶层/内层标志一致 ───────────────────────────────

def test_fix41_envelope_surfaces_inner_fallback():
    from akshare_mcp.utils import ok

    # data 内层标 fallback_used=true，顶层必须随之为 true
    result = ok({"value": 1, "fallback_used": True, "fallback_reason": "x_unavailable"})
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "x_unavailable"
    assert "fallback" in result["quality_flags"]


def test_fix41_envelope_surfaces_inner_degraded():
    from akshare_mcp.utils import ok

    result = ok({"value": 1, "degraded": True})
    assert result["degraded"] is True


def test_fix41_envelope_clean_when_no_inner_flags():
    from akshare_mcp.utils import ok

    result = ok({"value": 1})
    assert result["fallback_used"] is False
    assert result["degraded"] is False


# ── FIX-42: run_skill payload 去重 ───────────────────────────────────

def test_fix42_run_skill_no_duplicate_execution():
    src = (_SRC / "tools" / "skills.py").read_text(encoding="utf-8")
    # 不应再同时塞 "execution": execution 和 "result": execution 两份完整副本
    assert '"execution": execution' not in src
    assert "execution_ref" in src


# ── FIX-43: data_quality_workflow 空 records 一致拒绝 ────────────────

def test_fix43_data_quality_empty_records_rejected_static():
    src = (_SRC / "tools" / "ai_workflows_parts" / "formatters.py").read_text(encoding="utf-8")
    assert "records is required and must be a non-empty array" in src
    assert "empty_records" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
