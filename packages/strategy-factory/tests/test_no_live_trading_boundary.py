"""P0-4 regression guard: the strategy factory must never place live orders.

Readiness review (docs/architecture/AIASK_OVERALL_READINESS_REVIEW_2026-05-29.md,
risk P0-4) states the Strategy Factory must NOT be able to auto-promote a
strategy into real-money trading. Today this holds structurally:

  * ``StrategySubmitter`` only creates/persists strategy records and runs
    quality checks -- it has no order-execution path.
  * ``application/research/paper_trading_bridge.py`` routes incubation
    signals to *paper* orders (paper_trading_manager / paper_orders) only,
    and ``evaluate_promotion`` returns an eligibility verdict rather than
    executing anything.
  * Live order placement lives in akshare-mcp ``live_trading_manager`` behind
    a dry_run default + explicit confirm_token + audit trail.

This test locks that constraint in: no source file under
``packages/strategy-factory/src`` may import or call a live-trading /
real-order surface. Paper trading is explicitly allowed. If a future change
wires the factory directly to a broker, this test fails before review.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "strategy_factory"

# Tokens that indicate a *live* (real-money) trading surface. Paper trading
# (paper_trading_manager / paper_orders / paper_account) is intentionally NOT
# in this list -- the incubation bridge is allowed to drive paper trading.
_FORBIDDEN_LIVE_TOKENS = (
    "live_trading_manager",
    "live_broker",
    "submit_live_order",
    "place_live_order",
    "real_order",
    "execution_manager",
)

# Import / call patterns that would breach the boundary even if the literal
# token check above is dodged by aliasing.
_FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"^\s*from\s+\S*live_trading\S*\s+import", re.MULTILINE),
    re.compile(r"^\s*import\s+\S*live_trading", re.MULTILINE),
    re.compile(r"\bfrom\s+aiask_finance_mcp\b", re.MULTILINE),
)


def _python_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def test_strategy_factory_has_no_live_trading_tokens() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()
        for token in _FORBIDDEN_LIVE_TOKENS:
            if token in lowered:
                rel = str(path.relative_to(SRC_ROOT))
                offenders.append((rel, token))

    assert not offenders, (
        "strategy-factory must not reference a live-trading surface. "
        "Live order placement belongs in akshare-mcp live_trading_manager, "
        "gated by dry_run + confirm_token. Paper trading is allowed. "
        f"violations: {offenders[:10]}"
    )


def test_strategy_factory_does_not_import_finance_broker_package() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _FORBIDDEN_IMPORT_PATTERNS:
            for match in pattern.finditer(text):
                offenders.append((str(path.relative_to(SRC_ROOT)), match.group(0).strip()))

    assert not offenders, (
        "strategy-factory must not import a live-trading / broker package. "
        f"violations: {offenders[:10]}"
    )


def test_paper_trading_is_still_permitted() -> None:
    """Sanity check: the guard must not have outlawed paper trading.

    If this ever fails it means the paper bridge was removed or renamed --
    which would itself be a regression worth catching.
    """
    bridge = SRC_ROOT / "application" / "research" / "paper_trading_bridge.py"
    assert bridge.exists(), "paper_trading_bridge.py is expected to exist"
    text = bridge.read_text(encoding="utf-8", errors="ignore").lower()
    assert "paper" in text, "paper trading bridge should reference paper trading"
