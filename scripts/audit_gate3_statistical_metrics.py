#!/usr/bin/env python3
"""Gate-3 statistical metric availability audit (P1 prep).

This script inspects ``strategy_quality_reports`` from the most recent
``strategy_factory_runs`` to answer: for each Gate-3 statistical metric
(``wf_ic_ir`` / ``pkf_ic`` / ``bootstrap_ci_lower`` / ``param_sensitivity``
/ ``period_robustness``), is the value:

    - **missing**: field absent in ``validation_report``;
    - **placeholder**: field present but all candidates carry the same
      degenerate value (typically ``0.0`` from an empty walk-forward run);
    - **computed**: field present, varies across candidates, and the audit
      considers it a real number.

The output is a markdown + JSON report per metric with:

    - source_path: where Gate-3 currently reads it from;
    - real_compute_path: where the value comes from upstream;
    - current_status: missing / placeholder / computed;
    - missing_reason: short text;
    - short_term_derivation: how to back-fill from existing fields
      (a future P1 task may implement these);
    - long_term_capability: what real fix is needed (links to P4).

Usage::

    python scripts/audit_gate3_statistical_metrics.py
    python scripts/audit_gate3_statistical_metrics.py --run-id <factory_run_id>
    python scripts/audit_gate3_statistical_metrics.py --output reports/foo.json --format json
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from _integration_paths import default_sqlite_path

logger = logging.getLogger("audit_gate3_statistical_metrics")


def _configure_stdio_utf8() -> None:
    if sys.platform != "win32":
        return
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass


REPO = Path(__file__).resolve().parents[1]


# Static metadata about each metric Gate-3 expects. The ``source_path`` and
# ``real_compute_path`` fields are derived from reading the actual code
# in ``submission_gate/runner_parts/{trade_profile,multiple_testing}.py``.
# They are not produced by introspection because the audit is intentionally
# a static documentation step the operator inspects.
METRIC_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "wf_ic_ir",
        "display": "Walk-forward IC IR",
        "source_path": "validation_report.walk_forward.oos_rank_ic_ir | oos_ic_ir",
        "real_compute_path": (
            "factor_validation_bootstrap.run_walk_forward_validation -> "
            "validation_report.walk_forward"
        ),
        "short_term_derivation": (
            "If walk_forward.oos_rank_ic_mean / oos_rank_ic_std are populated "
            "but oos_rank_ic_ir is not, derive IR = mean / std (clipped). "
            "Mark derived=true."
        ),
        "long_term_capability": (
            "Ensure walk-forward backtest produces non-degenerate folds for "
            "the candidate's lookback window. Today many candidates show "
            "n_folds=0 because the universe lookback (~120 periods) is too "
            "short for the chosen fold count."
        ),
    },
    {
        "name": "pkf_ic",
        "display": "Purged k-fold IC",
        "source_path": "validation_report.purged_kfold.oos_rank_ic_mean | oos_ic_mean",
        "real_compute_path": (
            "factor_validation_bootstrap.run_purged_kfold_validation -> "
            "validation_report.purged_kfold"
        ),
        "short_term_derivation": (
            "Use validation_report.bootstrap_ci.ic_mean as a coarse proxy "
            "if purged_kfold is empty (mark derived=true). Acceptable only "
            "as P1 stop-gap; long-term we want real purged k-fold."
        ),
        "long_term_capability": (
            "Purged k-fold needs at least ~10 folds with embargo. Most "
            "current candidates have n_folds=0 — same root cause as wf_ic_ir."
        ),
    },
    {
        "name": "bootstrap_ci_lower",
        "display": "Bootstrap CI lower",
        "source_path": "validation_report.bootstrap_ci.ci_lower",
        "real_compute_path": (
            "factor_validation_bootstrap.run_skill_bootstrap -> "
            "validation_report.bootstrap_ci"
        ),
        "short_term_derivation": (
            "If ci_lower is missing but ic_mean and se are present, derive "
            "ci_lower = ic_mean - 1.96 * se. Mark derived=true."
        ),
        "long_term_capability": (
            "Bootstrap should resample the IC distribution >= 1000 times. "
            "Verify factor_validation_bootstrap actually runs the resampler "
            "for short-history candidates instead of returning zeros."
        ),
    },
    {
        "name": "param_sensitivity",
        "display": "Parameter sensitivity",
        "source_path": (
            "gate_payload.param_sensitivity (originally derived from "
            "backtest_metrics.parameter_perturbation_trade_stability)"
        ),
        "real_compute_path": (
            "_build_statistical_gate_payload computes "
            "1 - parameter_perturbation_trade_stability when the inverse "
            "is missing. P4 will replace this with a real perturbation "
            "backtest."
        ),
        "short_term_derivation": (
            "Already derived: 1 - parameter_perturbation_trade_stability. "
            "If trade_stability itself is missing, mark derived=true with "
            "value=None."
        ),
        "long_term_capability": (
            "P4 R8.1: actually run perturbation backtests (±10% / ±20% on "
            "the most sensitive parameter) and emit a real "
            "param_sensitivity = std(sharpe) / mean(sharpe)."
        ),
    },
    {
        "name": "period_robustness",
        "display": "Period robustness",
        "source_path": (
            "validation_report.period_robustness "
            "(first_half_ic / second_half_ic)"
        ),
        "real_compute_path": (
            "factor_validation_bootstrap.run_period_split_validation -> "
            "validation_report.period_robustness"
        ),
        "short_term_derivation": (
            "If only walk_forward exists, sample the first/second half of "
            "its is_ic / oos_ic series and surface as period_robustness. "
            "Mark derived=true."
        ),
        "long_term_capability": (
            "P4 R8.2: explicit train+oos / oos1+oos2 segmented backtest "
            "with cross-period correlation."
        ),
    },
]


def _load_db_path() -> Path:
    env_path = os.getenv("AKSHARE_MCP_SQLITE_PATH") or os.getenv("AIASK_SQLITE_PATH")
    if env_path:
        return Path(os.path.expanduser(env_path)).resolve()
    return default_sqlite_path()


def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"sqlite db not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _resolve_run_id(con: sqlite3.Connection, explicit: str | None) -> str:
    cur = con.cursor()
    if explicit:
        row = cur.execute(
            "SELECT run_id FROM strategy_factory_runs WHERE run_id = ?",
            (explicit,),
        ).fetchone()
        if not row:
            raise SystemExit(f"run_id not found: {explicit}")
        return row["run_id"]
    row = cur.execute(
        "SELECT run_id FROM strategy_factory_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise SystemExit("no rows in strategy_factory_runs")
    return row["run_id"]


def _load_strategy_quality_reports(
    con: sqlite3.Connection, run_id: str, *, lookback_window: int = 64,
) -> list[dict[str, Any]]:
    """Pull recent strategy_quality_reports.

    The schema doesn't link reports to a run_id directly, but Gate-3 runs
    submit them roughly at run time. We pull the ``lookback_window`` newest
    reports as a best-effort sample for the run; this matches what an
    operator would do manually.
    """
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT strategy_id, report_type, passed, summary, quality_gate,
               validation_report, backtest_metrics, created_at
        FROM strategy_quality_reports
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (lookback_window,),
    ).fetchall()
    return [dict(r) for r in rows]


def _safe_json_loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _safe_get(d: dict, *path: str) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _classify_value(value: Any) -> str:
    """Bucket a metric value into present_real / present_zero / present_nan / missing."""
    if value is None:
        return "missing"
    try:
        f = float(value)
    except Exception:
        return "missing"
    if f != f:  # NaN
        return "present_nan"
    if not (-1e30 < f < 1e30):
        return "present_inf"
    if abs(f) < 1e-12:
        return "present_zero"
    return "present_real"


def _extract_metric_value(metric: str, vr: dict[str, Any], gate: dict[str, Any]) -> Any:
    """Read a metric from validation_report / quality_gate the same way
    Gate-3 does at runtime."""
    if metric == "wf_ic_ir":
        wf = _safe_get(vr, "walk_forward") or {}
        for key in ("oos_rank_ic_ir", "oos_ic_ir"):
            if isinstance(wf, dict) and wf.get(key) is not None:
                return wf.get(key)
        return None
    if metric == "pkf_ic":
        pkf = _safe_get(vr, "purged_kfold") or {}
        for key in ("oos_rank_ic_mean", "oos_ic_mean"):
            if isinstance(pkf, dict) and pkf.get(key) is not None:
                return pkf.get(key)
        return None
    if metric == "bootstrap_ci_lower":
        return _safe_get(vr, "bootstrap_ci", "ci_lower")
    if metric == "param_sensitivity":
        # Gate-3 reads it from the gate result first, then derives.
        return gate.get("param_sensitivity")
    if metric == "period_robustness":
        pr = _safe_get(vr, "period_robustness") or {}
        first_ic = _safe_get(pr, "first_half_ic")
        second_ic = _safe_get(pr, "second_half_ic")
        if first_ic is None and second_ic is None:
            return None
        return {"first_half_ic": first_ic, "second_half_ic": second_ic}
    return None


def _summarize_metric(
    metric: str, observations: list[Any]
) -> dict[str, Any]:
    """Aggregate per-candidate observations into one verdict for the metric."""
    classifications = Counter()
    real_values: list[float] = []
    for obs in observations:
        if isinstance(obs, dict):
            # period_robustness style — both subfields must be inspected
            inner_classes = []
            for sub in ("first_half_ic", "second_half_ic"):
                inner_classes.append(_classify_value(obs.get(sub)))
            if all(c == "missing" for c in inner_classes):
                classifications["missing"] += 1
            elif all(c == "present_zero" for c in inner_classes):
                classifications["present_zero"] += 1
            else:
                classifications["present_real"] += 1
                # Take first_half_ic as a sample value to detect variance
                fv = obs.get("first_half_ic")
                try:
                    real_values.append(float(fv))
                except Exception:
                    pass
        else:
            cls = _classify_value(obs)
            classifications[cls] += 1
            if cls == "present_real":
                try:
                    real_values.append(float(obs))
                except Exception:
                    pass

    n = sum(classifications.values())
    if n == 0:
        return {
            "current_status": "missing",
            "missing_reason": "no candidates in the lookback window",
            "candidate_count": 0,
            "classifications": dict(classifications),
            "value_variance_observed": False,
        }

    # All missing? -> missing
    if classifications.get("missing", 0) == n:
        return {
            "current_status": "missing",
            "missing_reason": "field absent in all candidates' validation_report",
            "candidate_count": n,
            "classifications": dict(classifications),
            "value_variance_observed": False,
        }

    # All zeros (or zero+missing)? -> placeholder
    non_zero_real = classifications.get("present_real", 0)
    if non_zero_real == 0:
        zero = classifications.get("present_zero", 0)
        miss = classifications.get("missing", 0)
        if zero > 0 and zero + miss == n:
            return {
                "current_status": "placeholder",
                "missing_reason": (
                    "field is structurally present but every candidate "
                    "reports 0.0 (likely from empty walk-forward / "
                    "bootstrap runs)"
                ),
                "candidate_count": n,
                "classifications": dict(classifications),
                "value_variance_observed": False,
            }

    # Some real values — check whether they vary
    if real_values:
        unique_real = len({round(v, 6) for v in real_values})
        variance_observed = unique_real > 1
    else:
        variance_observed = False

    return {
        "current_status": "computed" if variance_observed else "placeholder",
        "missing_reason": (
            None
            if variance_observed
            else "field is present and non-zero but does not vary across "
                 "candidates — likely a constant fallback rather than a "
                 "real per-candidate computation"
        ),
        "candidate_count": n,
        "classifications": dict(classifications),
        "value_variance_observed": variance_observed,
        "real_value_sample": real_values[:5],
    }


def _audit_metric(metric_def: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    name = metric_def["name"]
    observations = []
    for row in reports:
        vr = _safe_json_loads(row.get("validation_report"))
        gate = _safe_json_loads(row.get("quality_gate"))
        observations.append(_extract_metric_value(name, vr, gate))
    summary = _summarize_metric(name, observations)
    return {
        **metric_def,
        **summary,
    }


def _render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Gate-3 Statistical Metric Audit")
    lines.append("")
    lines.append(f"- **run_id**: `{report['run_id']}`")
    lines.append(f"- **audited_at**: `{report['audited_at']}`")
    lines.append(f"- **candidate_lookback_count**: {report['candidate_lookback_count']}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Status | Candidates | Note |")
    lines.append("|---|---|---|---|")
    for m in report["metrics"]:
        note = m.get("missing_reason") or "ok"
        lines.append(
            f"| `{m['name']}` | **{m['current_status']}** | "
            f"{m.get('candidate_count', 0)} | {note} |"
        )
    lines.append("")

    for m in report["metrics"]:
        lines.append(f"## `{m['name']}` — {m['display']}")
        lines.append("")
        lines.append(f"- **status**: `{m['current_status']}`")
        lines.append(f"- **source_path**: `{m['source_path']}`")
        lines.append(f"- **real_compute_path**: `{m['real_compute_path']}`")
        lines.append("- **classifications**:")
        for k, v in (m.get("classifications") or {}).items():
            lines.append(f"  - `{k}`: {v}")
        if m.get("real_value_sample"):
            sample = ", ".join(f"{v:.4f}" for v in m["real_value_sample"])
            lines.append(f"- **real_value_sample (top 5)**: {sample}")
        if m.get("missing_reason"):
            lines.append(f"- **missing_reason**: {m['missing_reason']}")
        lines.append(f"- **short_term_derivation**: {m['short_term_derivation']}")
        lines.append(f"- **long_term_capability**: {m['long_term_capability']}")
        lines.append("")

    lines.append("## Recommended P1 接线 strategy")
    lines.append("")
    lines.append("Each metric's P1 wire-up depends on `current_status`:")
    lines.append("")
    lines.append("- `missing` -> implement `short_term_derivation` and mark `derived=true`.")
    lines.append("- `placeholder` -> wire up the field, but mark `derived=true` and treat 0.0 as `missing` for Gate-3 purposes (since it indicates an upstream broken pipeline, not a real-but-weak signal). P4 is the real fix.")
    lines.append("- `computed` -> wire up directly; `derived=false`. No further action needed.")
    return "\n".join(lines)


def _audit(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if getattr(args, "db", None) else _load_db_path()
    logger.info("audit using db=%s", db_path)
    con = _open_db(db_path)
    try:
        run_id = _resolve_run_id(con, getattr(args, "run_id", None))
        logger.info("audit using run_id=%s", run_id)
        reports = _load_strategy_quality_reports(
            con, run_id, lookback_window=int(getattr(args, "lookback", 64) or 64),
        )
    finally:
        con.close()

    metric_audits = [_audit_metric(m, reports) for m in METRIC_DEFINITIONS]
    report = {
        "run_id": run_id,
        "audited_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_lookback_count": len(reports),
        "db_path": str(db_path),
        "metrics": metric_audits,
    }

    fmt = (getattr(args, "format", "markdown") or "markdown").lower()
    output_path = getattr(args, "output", None)
    rendered_md = _render_markdown(report)
    rendered_json = _render_json(report)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # If user asked for a specific format, write that. If they didn't,
        # write both .md and .json so the operator can pick.
        if fmt == "json":
            out.write_text(rendered_json, encoding="utf-8")
            print(f"wrote json -> {out}")
        elif fmt == "markdown":
            out.write_text(rendered_md, encoding="utf-8")
            print(f"wrote markdown -> {out}")
        else:
            md_out = out.with_suffix(".md")
            json_out = out.with_suffix(".json")
            md_out.write_text(rendered_md, encoding="utf-8")
            json_out.write_text(rendered_json, encoding="utf-8")
            print(f"wrote markdown -> {md_out}")
            print(f"wrote json -> {json_out}")
    else:
        if fmt == "json":
            print(rendered_json)
        else:
            print(rendered_md)
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Audit Gate-3 statistical metric availability."
    )
    parser.add_argument("--run-id", "--run_id", dest="run_id",
                        type=str, default=None,
                        help="Specific factory run_id; defaults to the latest one.")
    parser.add_argument("--db", type=str, default=None,
                        help="Path to akshare_mcp.sqlite3; defaults to the env var "
                             "AKSHARE_MCP_SQLITE_PATH, AIASK_SQLITE_PATH, or AIASK_RUNTIME_HOME.")
    parser.add_argument("--lookback", type=int, default=64,
                        help="Number of recent strategy_quality_reports to inspect.")
    parser.add_argument("--output", type=str, default=None,
                        help="Optional output file path. If omitted, prints to stdout.")
    parser.add_argument("--format", type=str, choices=["markdown", "json", "both"],
                        default="markdown")
    args = parser.parse_args(argv)
    return _audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
