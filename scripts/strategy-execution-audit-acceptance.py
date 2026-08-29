#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def _as_unique_strings(values: Iterable[Any]) -> list[str]:
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _normalize_detail_dimension(
    *,
    top_level_values: Iterable[Any],
    blocker_details: Iterable[dict[str, Any]],
    detail_key: str,
    field_name: str,
    strategy_id: str,
) -> list[str]:
    top_level = _as_unique_strings(top_level_values)
    detail_values = _as_unique_strings(
        detail.get(detail_key)
        for detail in blocker_details
        if isinstance(detail, dict)
    )
    if top_level != detail_values:
        raise ValueError(
            f"{field_name} contract mismatch for {strategy_id}: "
            f"top_level={top_level} detail_values={detail_values}"
        )
    return top_level


def _validate_strategy_result(result: dict[str, Any]) -> dict[str, Any]:
    strategy_id = str(result.get("strategy_id") or "unknown").strip() or "unknown"
    blocker_details = list(result.get("blocker_details") or [])
    blockers = _normalize_detail_dimension(
        top_level_values=list(result.get("blockers") or []),
        blocker_details=blocker_details,
        detail_key="blocker",
        field_name="blockers",
        strategy_id=strategy_id,
    )
    gap_categories = _normalize_detail_dimension(
        top_level_values=list(result.get("gap_categories") or []),
        blocker_details=blocker_details,
        detail_key="category",
        field_name="gap_categories",
        strategy_id=strategy_id,
    )
    return {
        "strategy_id": strategy_id,
        "blockers": blockers,
        "gap_categories": gap_categories,
        "has_sample_gap": "sample_gap" in gap_categories,
    }


def validate_acceptance_report(report: dict[str, Any]) -> dict[str, Any]:
    strategy_results = list(report.get("strategy_results") or [])
    validated = [
        _validate_strategy_result(item)
        for item in strategy_results
        if isinstance(item, dict)
    ]
    return {
        "report_type": "execution_audit_acceptance_validation",
        "strategy_count": len(validated),
        "sample_gap_count": sum(1 for item in validated if item["has_sample_gap"]),
        "strategy_results": validated,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate strategy execution audit acceptance report contracts.")
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    print(json.dumps(validate_acceptance_report(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
