"""A degraded stock universe must not be reported as the whole market.

``_db_sync_common.resolve_stock_universe`` picks the stock universe and reports
which rule produced it, because the previous version could not: it fell back to a
30-code pool named ``FULL_UNIVERSE`` and the sync printed it as 「全量股票池」, so
a data-source outage produced a 30-ticker run that every downstream reader
understood as the full A-share market. ``strategy_factory_full_market_scores``
carries that name to this day.

These tests run the real module in a subprocess, because importing it in-process
mutates ``AIASK_SQLITE_PATH`` for the whole pytest session. The Sina listing is
stubbed inside the probe so the suite never depends on the network, which is what
makes the precedence order assertable instead of merely observed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

AIASK_ROOT = Path(__file__).resolve().parents[3]

_PROBE = """
import json, sys
sys.path.insert(0, {scripts!r})

import akshare_mcp.data_source.sina_listing as sina
{stub}

from _db_sync_common import resolve_stock_universe
codes, source = resolve_stock_universe()
print(json.dumps({{"count": len(codes), "source": source}}))
"""

_SINA_STUB = {
    "symbols": [
        {"code": f"{i:06d}", "symbol": f"sz{i:06d}", "name": "x", "market": "深圳"}
        for i in range(5000)
    ],
    "source": "sina:Market_Center.getHQNodeData[hs_a]",
    "complete": True,
    "failed_pages": [],
    "pages": 63,
    "transports": ["direct"],
}

_STUB_AVAILABLE = f"sina.fetch_all_a_share_symbols = lambda **kwargs: {_SINA_STUB!r}"
_STUB_DOWN = (
    "def _down(**kwargs):\n"
    "    raise RuntimeError('sina unreachable for this test')\n"
    "sina.fetch_all_a_share_symbols = _down"
)


def _probe(env_overrides: dict[str, str], *, sina_available: bool) -> tuple[dict, str]:
    env = dict(os.environ)
    env.pop("TUSHARE_TOKEN", None)
    env.update(env_overrides)
    body = _PROBE.format(
        scripts=str(AIASK_ROOT / "scripts"),
        stub=_STUB_AVAILABLE if sina_available else _STUB_DOWN,
    )
    completed = subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        cwd=str(AIASK_ROOT),
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1]), completed.stderr


def _make_stocks_db(path: Path, count: int) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE stocks (stock_code TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO stocks (stock_code) VALUES (?)",
            [(f"{600000 + i:06d}",) for i in range(count)],
        )
        conn.commit()


def test_a_market_sized_table_is_trusted_and_needs_no_listing(tmp_path) -> None:
    """With a full table the fast local path wins, and Sina is never consulted."""

    db_path = tmp_path / "full.sqlite3"
    _make_stocks_db(db_path, 5000)

    payload, stderr = _probe(
        {"AIASK_SQLITE_PATH": str(db_path), "AKSHARE_MCP_SQLITE_PATH": str(db_path)},
        sina_available=False,
    )

    assert payload["source"] == "sqlite:stocks"
    assert payload["count"] == 5000
    assert "[universe]" not in stderr, "the fast path must not warn about a fallback"


def test_a_stub_table_is_not_a_stock_list(tmp_path) -> None:
    """Three rows in `stocks` must not define the market for a sync run.

    The floor used to be 100 rows, which is how a 165-row residue became the
    universe and stayed there: large enough to pass, far too small to be the
    market.
    """

    db_path = tmp_path / "stub.sqlite3"
    _make_stocks_db(db_path, 3)

    payload, _stderr = _probe(
        {"AIASK_SQLITE_PATH": str(db_path), "AKSHARE_MCP_SQLITE_PATH": str(db_path)},
        sina_available=True,
    )

    assert payload["source"] == "sina:Market_Center[hs_a]"
    assert payload["count"] == 5000


def test_when_every_source_is_gone_it_says_so(tmp_path) -> None:
    """The seed pool is a last resort, and reaching it must be visible."""

    db_path = tmp_path / "empty.sqlite3"
    payload, stderr = _probe(
        {"AIASK_SQLITE_PATH": str(db_path), "AKSHARE_MCP_SQLITE_PATH": str(db_path)},
        sina_available=False,
    )

    assert payload["source"] == "seed:30"
    assert payload["count"] == 30
    assert "[universe]" in stderr, "the fallback was silent"
    assert "seed pool" in stderr
    assert "not the full A-share market" in stderr
    assert "sina:Market_Center" in stderr, "the reason Sina was skipped must be reported"


def test_an_incomplete_listing_is_reported_as_incomplete(capsys) -> None:
    """A partial page read must not be presented as the whole market."""

    sys.path.insert(0, str(AIASK_ROOT / "scripts"))
    try:
        import akshare_mcp.data_source.sina_listing as sina
        import _db_sync_common as common

        partial = dict(_SINA_STUB, complete=False, failed_pages=[7, 8])
        original = sina.fetch_all_a_share_symbols
        sina.fetch_all_a_share_symbols = lambda **kwargs: partial
        try:
            codes, source = common.fetch_market_symbols()
        finally:
            sina.fetch_all_a_share_symbols = original
    finally:
        sys.path.pop(0)

    assert source == "sina:Market_Center[hs_a]"
    assert len(codes) == 5000
    captured = capsys.readouterr().err
    assert "INCOMPLETE" in captured
    assert "[7, 8]" in captured
