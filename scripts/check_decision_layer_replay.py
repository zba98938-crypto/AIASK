#!/usr/bin/env python3
"""Decide whether the decision layer can be replayed — before producing a hit rate.

A replay asks what `should_i_buy` would have said on day D, using only what was
knowable then, and scores it against what happened. The number it produces is only
worth reading if the answer to "knowable then" is genuinely yes, because a
contaminated replay does not return a noisy hit rate — it returns a **flattering**
one. The backtest already demonstrated the shape: a simple MA cross reported a 195×
return over a period in which the price moved +1.5%.

So this checks the inputs first and reports the contamination points. **It does not
emit a hit rate when the chain is contaminated**, because the number would be
indistinguishable from a real result to anyone reading it later.

What it checks:

* **Structural** — for each input the decision path reads, does the read accept a
  point-in-time bound, and is the bound threaded through?
* **Behavioural** — called with two different ``as_of`` values, does the answer
  actually change? A chain that ignores its own argument returns the same thing for
  every date, which no amount of reading the code shows as plainly.

Usage::

    python scripts/check_decision_layer_replay.py
    python scripts/check_decision_layer_replay.py --codes 600519,000858
    python scripts/check_decision_layer_replay.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "akshare-mcp" / "src"))

#: Inputs the decision path reads, and whether each one can be bounded to a date.
#: A read that cannot be bounded contributes today's data to a decision dated
#: earlier, which is look-ahead by construction.
INPUTS: tuple[dict[str, str], ...] = (
    {
        "input": "kline_1d (technical signals)",
        "bound": "as_of",
        "where": "tools/_decision_buy.py:_filter_klines_by_as_of",
        "verdict": "bounded",
    },
    {
        "input": "valuation / fundamentals / technical / momentum context",
        "bound": "none",
        "where": "tools/investment_analysis.py:get_investment_analysis(code)",
        "verdict": "UNBOUNDED",
    },
)


def _load_buy():
    """Import through the package: `_decision_buy` uses relative imports."""

    from akshare_mcp.tools._decision_buy import should_i_buy

    return should_i_buy


async def _behavioural_probe(codes: list[str], dates: list[str]) -> list[dict[str, Any]]:
    """Call the tool at several as_of points and see whether the answers move."""

    should_i_buy = _load_buy()
    observations: list[dict[str, Any]] = []
    for code in codes:
        answers: dict[str, dict[str, Any]] = {}
        for when in dates:
            kwargs: dict[str, Any] = {"code": code}
            if when:
                kwargs["as_of"] = when
            try:
                result = await should_i_buy(**kwargs)
            except Exception as exc:
                answers[when or "today"] = {"error": f"{type(exc).__name__}: {str(exc)[:70]}"}
                continue
            data = dict(result.get("data") or {})
            answers[when or "today"] = {
                "success": result.get("success"),
                "analysis_date": data.get("analysis_date"),
                "current_price": data.get("current_price"),
                "recommendation": data.get("recommendation"),
                "score": data.get("score"),
            }
        priced = {k: v for k, v in answers.items() if v.get("current_price") is not None}
        distinct_prices = {round(float(v["current_price"]), 2) for v in priced.values()}
        distinct_dates = {str(v.get("analysis_date")) for v in priced.values()}
        observations.append({
            "code": code,
            "answers": answers,
            "distinct_prices": sorted(distinct_prices),
            "distinct_analysis_dates": sorted(distinct_dates),
            # If two different as_of points yield the same analysis date, the bound
            # is not reaching the arithmetic that matters.
            "bound_is_honoured": len(distinct_dates) > 1 if len(priced) > 1 else None,
        })
    return observations


def run(codes: list[str], dates: list[str]) -> dict[str, Any]:
    unbounded = [entry for entry in INPUTS if entry["verdict"] != "bounded"]
    observations = asyncio.run(_behavioural_probe(codes, dates))
    return {
        "inputs": [dict(entry) for entry in INPUTS],
        "unbounded_inputs": [entry["input"] for entry in unbounded],
        "observations": observations,
        "replayable": not unbounded,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codes", default="600519,000858")
    parser.add_argument("--dates", default="2026-08-01,2026-09-01,")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    dates = [d.strip() for d in args.dates.split(",")]
    report = run(codes, dates)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=== 时点约束审计 ===")
    for entry in report["inputs"]:
        mark = "  " if entry["verdict"] == "bounded" else "!!"
        print(f"  {mark} {entry['input']}")
        print(f"       bound={entry['bound']:8s} at {entry['where']}")

    print("\n=== 行为探针：不同 as_of 是否给出不同答案 ===")
    for obs in report["observations"]:
        print(f"\n  {obs['code']}")
        for when, answer in obs["answers"].items():
            print(f"    as_of={when:12s} analysis_date={str(answer.get('analysis_date')):12s} "
                  f"price={str(answer.get('current_price')):10s} rec={answer.get('recommendation')}")
        honoured = obs["bound_is_honoured"]
        print(f"    -> 时点{'被贯彻' if honoured else ('未贯彻' if honoured is False else '样本不足')}"
              f"（不同 analysis_date: {obs['distinct_analysis_dates']}）")

    print()
    if report["replayable"]:
        print("链路全部输入都能被时点约束；可以出命中率")
        return 0

    print("!! 链路存在无法约束的输入：")
    for name in report["unbounded_inputs"]:
        print(f"     {name}")
    print()
    print("   这些输入会把手头最新的数据喂给一个发生在过去的判断。")
    print("   **因此本脚本不出命中率**——污染过的回放不会给出一个嘈杂的数字，")
    print("   它会给出一个好看的数字：回测已经演示过这个形态（声称 195 倍，实际 +1.5%）。")
    print()
    print("   要让它可回放，需要给上面每个 unbounded 输入加上时点参数并一路传到底；")
    print("   在那之前，这一层的判断无法被验证。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
