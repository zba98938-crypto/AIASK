"""A degraded stock universe must not be reported as the whole market.

`_db_sync_common` falls back to a 30-code seed pool when it cannot read a real
stock list. That pool used to be called ``FULL_UNIVERSE`` and printed as
「全量股票池」, so a data-source outage produced a sync over 30 tickers that every
downstream reader understood as the full A-share market — the origin of the
`strategy_factory_full_market_scores` table name.

The fallback still exists; what changed is that it says so. These tests run the
real module in a subprocess, because importing it in-process mutates
``AIASK_SQLITE_PATH`` for the whole pytest session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

AIASK_ROOT = Path(__file__).resolve().parents[3]

_PROBE = """
import json, sys
sys.path.insert(0, {scripts!r})
from _db_sync_common import resolve_stock_universe
codes, source = resolve_stock_universe()
print(json.dumps({{"count": len(codes), "source": source}}))
"""


def _probe(env_overrides: dict[str, str]) -> tuple[dict, str]:
    env = dict(os.environ)
    # A caller's own credentials must not decide the result of a fallback test.
    env.pop("TUSHARE_TOKEN", None)
    env.update(env_overrides)
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(scripts=str(AIASK_ROOT / "scripts"))],
        capture_output=True,
        text=True,
        cwd=str(AIASK_ROOT),
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1]), completed.stderr


def test_an_empty_database_falls_back_and_says_so(tmp_path) -> None:
    payload, stderr = _probe(
        {
            "AIASK_SQLITE_PATH": str(tmp_path / "empty.sqlite3"),
            "AKSHARE_MCP_SQLITE_PATH": str(tmp_path / "empty.sqlite3"),
        }
    )

    assert payload["source"] == "seed:30", (
        "an empty database must not be reported as a real stock list; "
        f"got source={payload['source']!r}"
    )
    assert payload["count"] == 30
    # The reason has to reach the operator, not just the return value.
    assert "[universe]" in stderr, "the fallback was silent"
    assert "seed pool" in stderr
    assert "not the full A-share market" in stderr


def test_a_seeded_database_is_reported_as_the_database(tmp_path) -> None:
    """When the DB really does hold a usable list, that source must be named.

    Otherwise the fallback warning above would be indistinguishable from the
    normal path, and the disclosure would carry no information.
    """

    import sqlite3

    db_path = tmp_path / "seeded.sqlite3"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE stocks (stock_code TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO stocks (stock_code) VALUES (?)",
            [(f"{600000 + i:06d}",) for i in range(150)],
        )
        conn.commit()

    payload, stderr = _probe(
        {
            "AIASK_SQLITE_PATH": str(db_path),
            "AKSHARE_MCP_SQLITE_PATH": str(db_path),
        }
    )

    assert payload["source"] == "sqlite:stocks"
    assert payload["count"] == 150
    assert "[universe]" not in stderr, "a successful read must not warn about a fallback"


def test_a_nearly_empty_stocks_table_does_not_count_as_a_stock_list(tmp_path) -> None:
    """The 100-row floor is the difference between a list and a stub.

    Three rows currently sitting in `stocks` would otherwise define "the market"
    for an entire sync run.
    """

    import sqlite3

    db_path = tmp_path / "stub.sqlite3"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE stocks (stock_code TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO stocks (stock_code) VALUES (?)", [("600519",), ("000858",), ("601318",)]
        )
        conn.commit()

    payload, stderr = _probe(
        {
            "AIASK_SQLITE_PATH": str(db_path),
            "AKSHARE_MCP_SQLITE_PATH": str(db_path),
        }
    )

    assert payload["source"] == "seed:30"
    assert "below the 100-row floor" in stderr
