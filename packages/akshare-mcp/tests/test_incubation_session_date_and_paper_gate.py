"""Behaviour of three 2026-10-25 fixes, each against a real SQLiteAdapter.

* session_date: incubation output is labelled with the newest synced
  ``kline_1d`` session, not the wall clock (a Saturday run wrote 2026-10-24).
* apply_paper_promotion_gate: a losing paper account cannot promote, and a
  failed evidence read blocks instead of passing silently.
* record_metrics: execution counters fold every active incubation account
  (the fold previously built ``IN (1)`` instead of ``IN ($1)`` and swallowed
  the error).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
import pytest_asyncio

from akshare_mcp.services.incubation import StrategyIncubationService
from akshare_mcp.services.incubation_factory import session_date as sd
from akshare_mcp.services.strategy_lifecycle_shared.overview import (
    _load_latest_paper_evidence,
    apply_paper_promotion_gate,
)


@pytest_asyncio.fixture
async def adapter(initialized_db):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter
    from strategy_factory.infrastructure.factory_schema import register_factory_schema

    # Strategy tables are owned by strategy-factory; without its provider the
    # adapter creates market tables only.
    register_factory_schema()
    db = SQLiteAdapter(path=initialized_db)
    await db.initialize()
    sd.clear_cache()
    try:
        yield db
    finally:
        sd.clear_cache()
        await db.close()


async def _bars(db, day: str, n_codes: int) -> None:
    async with db.acquire() as conn:
        for i in range(n_codes):
            await conn.execute(
                "INSERT INTO kline_1d (time, code, open, high, low, close, volume) VALUES ($1, $2, 1, 1, 1, 1, 1)",
                f"{day}T15:00:00+08:00",
                f"{600000 + i:06d}",
            )


# --- session date -----------------------------------------------------------


@pytest.mark.asyncio
async def test_session_date_is_newest_fully_synced_day(adapter):
    await _bars(adapter, "2026-10-23", 1200)  # Friday, full market
    result = await sd.resolve_session_date(adapter, use_cache=False)
    assert result["date"] == date(2026, 10, 23)
    assert result["source"] == sd.SOURCE_SYNCED
    assert result["coverage"] == 1200


@pytest.mark.asyncio
async def test_partial_days_do_not_count_as_a_session(adapter):
    # Live store on 2026-10-25: 10-16 full, 10-19..10-23 only 53 codes each.
    await _bars(adapter, "2026-10-16", 1200)
    for day in ("2026-10-19", "2026-10-20", "2026-10-23"):
        await _bars(adapter, day, 53)
    result = await sd.resolve_session_date(adapter, use_cache=False)
    assert result["date"] == date(2026, 10, 16)
    assert result["source"] == sd.SOURCE_SYNCED
    assert result["latest_bar_date"] == date(2026, 10, 23)


@pytest.mark.asyncio
async def test_only_partial_days_is_labelled_partial(adapter):
    await _bars(adapter, "2026-10-23", 53)
    result = await sd.resolve_session_date(adapter, use_cache=False)
    assert result["date"] == date(2026, 10, 23)
    assert result["source"] == sd.SOURCE_PARTIAL


@pytest.mark.asyncio
async def test_empty_store_falls_back_to_wall_clock_and_says_so(adapter):
    result = await sd.resolve_session_date(adapter, use_cache=False)
    assert result["source"] == sd.SOURCE_WALL_CLOCK
    assert result["date"] == date.today()


@pytest.mark.asyncio
async def test_metrics_recorder_without_date_uses_session_not_today(adapter):
    from akshare_mcp.services.incubation_factory.metrics_recorder import MetricsRecorder

    await _bars(adapter, "2026-10-23", 1200)
    saved = {}

    async def _save(sid, metric_date, metric):
        saved["metric_date"] = metric_date
        return metric

    adapter.save_strategy_incubation_metric = _save
    await MetricsRecorder().record(adapter, {"id": "s1"}, {"primary_skill_lcb": 0.1, "recent_primary_skill_lcb": 0.1,
                                                          "primary_effective_n": 10, "secondary_effective_n": 5,
                                                          "coverage_ratio": 1.0})
    assert saved["metric_date"] == date(2026, 10, 23)


# --- paper promotion gate ---------------------------------------------------


def test_losing_paper_account_cannot_promote():
    blockers: list[str] = []
    ready = apply_paper_promotion_gate(True, blockers, {"status": "ok", "nav": 0.781, "max_drawdown": 0.221})
    assert ready is False
    assert any(b.startswith("paper_nav_below_promotion_floor") for b in blockers)
    assert any(b.startswith("paper_nav_mdd_exceeds_promotion_ceiling") for b in blockers)


def test_healthy_paper_account_keeps_promotion():
    blockers: list[str] = []
    assert apply_paper_promotion_gate(True, blockers, {"status": "ok", "nav": 0.976, "max_drawdown": 0.074}) is True
    assert blockers == []


def test_failed_evidence_read_blocks_instead_of_passing():
    blockers: list[str] = []
    ready = apply_paper_promotion_gate(True, blockers, {"status": "error", "nav": None, "max_drawdown": None})
    assert ready is False
    assert blockers == ["paper_evidence_unavailable"]


def test_missing_metrics_row_does_not_invent_a_veto():
    blockers: list[str] = []
    assert apply_paper_promotion_gate(True, blockers, {"status": "missing", "nav": None, "max_drawdown": None}) is True


def test_gate_never_turns_a_blocked_strategy_ready():
    blockers = ["Sharpe 0.00 ≤ 0.50"]
    assert apply_paper_promotion_gate(False, blockers, {"status": "ok", "nav": 1.0, "max_drawdown": 0.0}) is False
    assert blockers == ["Sharpe 0.00 ≤ 0.50"]


@pytest.mark.asyncio
async def test_paper_evidence_reads_latest_metrics_row(adapter):
    async with adapter.acquire() as conn:
        await conn.execute(
            "INSERT INTO strategies (id, name, strategy_type, params, status, created_at, updated_at) "
            "VALUES ('lsmap_x', 'lsmap', 'multi_factor', '{}', 'incubating', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        for metric_date, nav, mdd in (("2026-10-19", 0.95, 0.05), ("2026-10-23", 0.781, 0.221)):
            await conn.execute(
                "INSERT INTO strategy_incubation_metrics (strategy_id, metric_date, nav, max_drawdown) VALUES ($1, $2, $3, $4)",
                "lsmap_x", metric_date, nav, mdd,
            )
    evidence = await _load_latest_paper_evidence(adapter, "lsmap_x")
    assert evidence == {"status": "ok", "nav": 0.781, "max_drawdown": 0.221}
    assert (await _load_latest_paper_evidence(adapter, "absent"))["status"] == "missing"


@pytest.mark.asyncio
async def test_record_metrics_folds_every_active_account(adapter):
    service = StrategyIncubationService()
    strategy = {"id": "multi-acct", "name": "multi", "strategy_type": "momentum", "params": {}}
    async with adapter.acquire() as conn:
        await conn.execute(
            "INSERT INTO strategies (id, name, strategy_type, params, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, '{}', 'incubating', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            strategy["id"], strategy["name"], strategy["strategy_type"],
        )
    bound = (await service.ensure_account(adapter, strategy))["account"]["id"]
    second = (await service._save_strategy_account(adapter, {
        "id": "inc_multi_second", "user_id": "strategy_factory", "name": "second",
        "initial_capital": 100000.0, "current_capital": 100000.0, "total_value": 100000.0,
        "strategy_id": strategy["id"], "account_type": "incubation",
        "incubation_stage": "warmup", "status": "active",
    }))["id"]
    async with adapter.acquire() as conn:
        await conn.execute(
            "INSERT INTO strategy_incubation_accounts (strategy_id, account_id, stage, status, bound_at) "
            "VALUES ($1, $2, 'warmup', 'active', '2026-07-16 04:32:30')",
            strategy["id"], second,
        )
        for account_id, n in ((bound, 2), (second, 3)):
            for i in range(n):
                await conn.execute(
                    "INSERT INTO paper_trades (account_id, stock_code, stock_name, trade_type, price, quantity, amount, trade_time, strategy_id) "
                    "VALUES ($1, '600000', 'x', 'buy', 10, 100, 1000, '2026-10-23 10:00:00', $2)",
                    account_id, strategy["id"],
                )

    metric = await service.record_metrics(adapter, strategy, date(2026, 10, 23))

    metadata = metric["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["execution_aggregation_errors"] == []
    assert set(metadata["execution_accounts"]) == {bound, second}
    assert int(metric["total_trades"]) == 5


@pytest.mark.asyncio
async def test_neutral_signals_are_excluded_from_hit_rate_not_scored_long():
    from akshare_mcp.services.incubation_factory.forward_verifier import ForwardVerifier

    def _ev(direction, ret5):
        return {"direction": direction, "forward_returns": {"5": ret5, "10": ret5}}

    # 4 directional signals (2 hits) + 6 neutral ones on rising prices.  Scored
    # as long, the neutrals would add 6 hits: n=10, hit=0.8.
    evidence = [_ev("up", 0.02), _ev("up", -0.01), _ev("down", -0.03), _ev("down", 0.01)]
    evidence += [_ev("neutral", 0.02) for _ in range(6)]

    class _Db:
        async def list_strategy_signal_evidence(self, strategy_id, limit):
            del strategy_id, limit
            return evidence

    result = await ForwardVerifier().verify(_Db(), {"id": "s1", "strategy_type": "momentum"})
    assert result["primary_effective_n"] == 4
    assert result["primary_hit_rate"] == 0.5
    assert result["excluded_directions"] == {"neutral": 6, "unrecognized": 0}


@pytest.mark.asyncio
async def test_paper_evidence_read_failure_is_reported():
    class _Broken:
        def acquire(self):
            raise RuntimeError("db locked")

    evidence = await _load_latest_paper_evidence(_Broken(), "s1")
    assert evidence["status"] == "error"
    assert "db locked" in evidence["error"]
