"""Unit tests for ``_build_warmup_error_topn`` and the console error summary
emitted when warmup degrades.

These tests pin Requirements 3.1, 3.2, 3.3 and Property 8 (additive summary
field). The console-side `logger.warning` call site is exercised in the
end-to-end factory run; here we focus on the topN builder semantics.
"""

from __future__ import annotations

from strategy_factory.application.cycle_runner import _build_warmup_error_topn


def test_build_warmup_error_topn_extracts_failed_entries() -> None:
    warmup_result = {
        "ok": False,
        "status": "failed",
        "failed": 2,
        "schedules": [
            {
                "schedule_id": "schedule_runtime_core_market",
                "task_type": "core_market",
                "task": {
                    "task_id": "sync_core_market_001",
                    "status": "failed",
                    "error_message": "脚本不存在: /tmp/audit_sync_core_market_data.py",
                },
            },
            {
                "schedule_id": "schedule_runtime_factor_context",
                "task_type": "factor_context",
                "task": {
                    "task_id": "sync_factor_context_001",
                    "status": "failed",
                    "error_message": (
                        "[audit_sync_factor_context] reason=market_text_budget_exceeded "
                        "task_type=factor_context budget_sec=30 elapsed_sec=31.0 mode=warmup"
                    ),
                },
            },
        ],
    }

    out = _build_warmup_error_topn(warmup_result)

    assert len(out) == 2
    first, second = out
    assert first["task_type"] == "core_market"
    assert first["schedule_id"] == "schedule_runtime_core_market"
    assert first["task_id"] == "sync_core_market_001"
    assert first["error_kind"] == "script_missing"
    assert "脚本不存在" in (first["error_message"] or "")

    assert second["task_type"] == "factor_context"
    assert second["error_kind"] == "exception"
    assert "market_text_budget_exceeded" in (second["error_message"] or "")


def test_build_warmup_error_topn_skips_completed_entries() -> None:
    warmup_result = {
        "schedules": [
            {
                "schedule_id": "ok_schedule",
                "task_type": "core_market",
                "task": {"task_id": "ok_task", "status": "completed",
                         "error_message": None},
            },
            {
                "schedule_id": "bad_schedule",
                "task_type": "factor_context",
                "task": {"task_id": "bad_task", "status": "failed",
                         "error_message": "boom"},
            },
        ],
    }

    out = _build_warmup_error_topn(warmup_result)
    assert len(out) == 1
    assert out[0]["task_id"] == "bad_task"


def test_build_warmup_error_topn_truncates_long_message() -> None:
    long_msg = "x" * 5000
    warmup_result = {
        "schedules": [
            {
                "schedule_id": "s",
                "task_type": "core_market",
                "task": {"task_id": "t", "status": "failed",
                         "error_message": long_msg},
            },
        ],
    }

    out = _build_warmup_error_topn(warmup_result)
    assert len(out) == 1
    # Builder caps stored message at 500 chars; console logger does its own
    # 200-char cap separately. We verify the storage cap here.
    assert len(out[0]["error_message"]) == 500


def test_build_warmup_error_topn_respects_limit() -> None:
    schedules = [
        {
            "schedule_id": f"s{i}",
            "task_type": "core_market",
            "task": {"task_id": f"t{i}", "status": "failed",
                     "error_message": f"err {i}"},
        }
        for i in range(20)
    ]
    out = _build_warmup_error_topn({"schedules": schedules}, limit=3)
    assert len(out) == 3
    assert [e["task_id"] for e in out] == ["t0", "t1", "t2"]


def test_build_warmup_error_topn_classifies_error_kinds() -> None:
    schedules = [
        {
            "schedule_id": "s_missing",
            "task_type": "core_market",
            "task": {"task_id": "missing",
                     "status": "failed",
                     "error_message": "脚本不存在: scripts/foo.py"},
        },
        {
            "schedule_id": "s_timeout",
            "task_type": "factor_context",
            "task": {"task_id": "timeout",
                     "status": "failed",
                     "error_message": "factor_context_timeout_after_45s"},
        },
        {
            "schedule_id": "s_other",
            "task_type": "factor_context",
            "task": {"task_id": "other",
                     "status": "failed",
                     "error_message": "ConnectionResetError: peer closed"},
        },
        {
            "schedule_id": "s_blank",
            "task_type": "factor_context",
            "task": {"task_id": "blank",
                     "status": "failed",
                     "error_message": ""},
        },
    ]
    out = _build_warmup_error_topn({"schedules": schedules})
    by_id = {e["task_id"]: e for e in out}
    assert by_id["missing"]["error_kind"] == "script_missing"
    assert by_id["timeout"]["error_kind"] == "timeout"
    assert by_id["other"]["error_kind"] == "exception"
    # Empty message gets None classification — a nice signal vs a noisy default.
    assert by_id["blank"]["error_kind"] is None


def test_build_warmup_error_topn_handles_missing_keys_safely() -> None:
    # No schedules at all
    assert _build_warmup_error_topn({}) == []
    # None input
    assert _build_warmup_error_topn(None) == []
    # Schedules is wrong type
    assert _build_warmup_error_topn({"schedules": "garbage"}) == []
    # Entry without task dict
    assert _build_warmup_error_topn({
        "schedules": [{"schedule_id": "s", "task_type": "x"}]
    }) == [{
        "task_type": "x",
        "schedule_id": "s",
        "task_id": None,
        "error_message": None,
        "error_kind": None,
    }]


def test_build_warmup_error_topn_falls_back_to_results_errors() -> None:
    """When task.error_message is missing but results.errors has entries,
    the builder should join the first few errors so the operator still sees
    a useful message."""
    warmup_result = {
        "schedules": [
            {
                "schedule_id": "s",
                "task_type": "core_market",
                "task": {
                    "task_id": "t",
                    "status": "failed",
                    "error_message": None,
                    "results": {"errors": ["err A", "err B", "err C", "err D"]},
                },
            },
        ],
    }
    out = _build_warmup_error_topn(warmup_result)
    assert len(out) == 1
    msg = out[0]["error_message"]
    assert msg is not None
    assert "err A" in msg
    assert "err B" in msg
    assert "err C" in msg
    # Builder uses first 3 errors only
    assert "err D" not in msg
