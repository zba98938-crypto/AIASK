from __future__ import annotations


def _source(status: str, fields: list[str]) -> dict:
    from strategy_factory.application.collect import DataCollector

    return DataCollector._build_source_status(
        status,
        fields,
        source_name="test",
        asof_time="2026-05-21T09:00:00+08:00",
    )


def test_optional_north_fund_unavailable_does_not_degrade_snapshot():
    from strategy_factory.application.collect import DataCollector

    snapshot = {
        "date": "2026-05-21",
        "north_fund_3d_net": 0.0,
        "event_driven": {"event_count": 0, "tasks_ready_count": 0},
    }
    sources = {
        "fear_greed": _source("success", ["fear_greed_index"]),
        "north_fund": DataCollector._build_source_status(
            "optional_unavailable",
            ["north_fund_3d_net"],
            reason="north_fund db summary unavailable",
            source_name="north_fund",
            asof_time="2026-05-21T09:00:00+08:00",
        ),
    }

    DataCollector._finalize_snapshot_contract(
        snapshot,
        sources,
        failure_reasons=[],
        missing_fields=[],
        asof_time="2026-05-21T09:00:00+08:00",
    )

    assert snapshot["degraded"] is False
    assert snapshot["completeness"]["completion_ratio"] == 1.0
    assert snapshot["completeness"]["missing_sources"] == []
    assert snapshot["completeness"]["optional_unavailable_sources"] == ["north_fund"]
    assert snapshot["failure_reasons"] == []
    assert snapshot["missing_fields"] == []
    assert snapshot["sources"]["north_fund"]["degraded"] is False
    assert snapshot["sources"]["north_fund"]["quality_flags"] == ["optional_unavailable"]


def test_required_source_fallback_still_degrades_snapshot_completeness():
    from strategy_factory.application.collect import DataCollector

    snapshot = {
        "date": "2026-05-21",
        "event_driven": {"event_count": 0, "tasks_ready_count": 0},
    }
    sources = {
        "fear_greed": _source("success", ["fear_greed_index"]),
        "margin_data": _source("fallback", ["margin_5d_change_pct"]),
        "north_fund": _source("optional_unavailable", ["north_fund_3d_net"]),
    }

    DataCollector._finalize_snapshot_contract(
        snapshot,
        sources,
        failure_reasons=[
            {
                "source": "margin_data",
                "status": "fallback",
                "reason": "margin proxy unavailable",
                "fallback_used": True,
                "fields": ["margin_5d_change_pct"],
            }
        ],
        missing_fields=["margin_5d_change_pct"],
        asof_time="2026-05-21T09:00:00+08:00",
    )

    assert snapshot["degraded"] is True
    assert snapshot["completeness"]["completion_ratio"] == 0.5
    assert snapshot["completeness"]["missing_sources"] == ["margin_data"]
    assert snapshot["completeness"]["optional_unavailable_sources"] == ["north_fund"]
    assert snapshot["failure_reasons"][0]["source"] == "margin_data"
    assert snapshot["missing_fields"] == ["margin_5d_change_pct"]
