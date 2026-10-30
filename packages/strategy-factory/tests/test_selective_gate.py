"""Behaviour tests for WP-C selective gate and WP-0.5 daily prediction writer.

The gate's job is to make "no probability, no order" the default. These tests
assert the refusal paths first, because that is the state the system is in
today: there is no active calibration model.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from strategy_factory.application.research.daily_prediction_writer import (
    DailyPredictionWriter,
    _closed_bars,
)
from strategy_factory.application.selective_gate import (
    ACTION_ABSTAINED,
    ACTION_ORDERED,
    REASON_BELOW_THRESHOLD,
    REASON_CALIBRATION_UNAVAILABLE,
    REASON_NEUTRAL,
    REASON_TARGET_MISMATCH,
    SelectiveGate,
    apply_model,
    decide,
    find_predictions_for_session,
)
from test_paper_trading_bridge import _FakePaperDb, _buy_signals

MARKET_TZ = ZoneInfo("Asia/Shanghai")


def _prediction(**overrides):
    payload = {
        "prediction_id": "tp-1",
        "strategy_id": "s-1",
        "stock_code": "600519",
        "prediction_as_of": "2026-10-15",
        "target_trading_date": "2026-10-16",
        "direction": "up",
        "confidence": 0.9,
        "horizon": "5d",
    }
    payload.update(overrides)
    return payload


def test_without_active_model_everything_abstains() -> None:
    decision = decide(_prediction(), signal_date=date(2026, 10, 16), model=None)
    assert decision.action == ACTION_ABSTAINED
    assert decision.reason == REASON_CALIBRATION_UNAVAILABLE
    assert decision.calibrated_confidence is None


def test_model_applies_and_threshold_decides() -> None:
    model = {"model_hash": "cm_1", "params_json": '{"a": 20.0, "b": -15.0}'}
    low = decide(_prediction(confidence=0.5), signal_date=date(2026, 10, 16), model=model, threshold=0.85)
    high = decide(_prediction(confidence=0.95), signal_date=date(2026, 10, 16), model=model, threshold=0.85)
    assert low.action == ACTION_ABSTAINED and low.reason == REASON_BELOW_THRESHOLD
    assert low.calibrated_confidence == pytest.approx(apply_model(model, 0.5))
    assert high.action == ACTION_ORDERED
    assert high.calibration_model_hash == "cm_1"


def test_neutral_and_stale_predictions_never_trade() -> None:
    model = {"model_hash": "cm_1", "params_json": '{"a": 20.0, "b": 0.0}'}
    neutral = decide(_prediction(direction="neutral"), signal_date=date(2026, 10, 16), model=model)
    assert neutral.reason == REASON_NEUTRAL
    stale = decide(_prediction(target_trading_date="2026-04-01"), signal_date=date(2026, 10, 16), model=model)
    assert stale.reason == REASON_TARGET_MISMATCH


def test_threshold_default_is_eighty_five() -> None:
    from strategy_factory.application.selective_gate import DEFAULT_THRESHOLD

    assert DEFAULT_THRESHOLD == 0.85


class _GateDB:
    def __init__(self, predictions):
        self.predictions = predictions
        self.written: list[dict] = []

    async def fetch(self, sql, *args):
        if "calibration_models" in sql:
            return []
        return list(self.predictions)

    async def execute(self, sql, *args):
        self.written.append(args)
        return 1


@pytest.mark.asyncio
async def test_session_evaluation_records_every_abstention() -> None:
    db = _GateDB([_prediction(), _prediction(prediction_id="tp-2", stock_code="000001")])
    result = await SelectiveGate().evaluate_session(db, signal_date=date(2026, 10, 16))
    assert result["matched_predictions"] == 2
    assert result["ordered"] == 0
    assert result["abstained"] == 2
    assert result["reason_counts"] == {REASON_CALIBRATION_UNAVAILABLE: 2}
    assert len(db.written) == 2


class _ActiveModelDB(_GateDB):
    """A gate db with an active calibration model, so decisions can be ``ordered``."""

    async def fetch(self, sql, *args):
        if "calibration_models" in sql:
            return [{"model_hash": "cm_1", "params_json": '{"a": 40.0, "b": -30.0}', "feature_version": "v1", "fit_end": "2026-10-01"}]
        return list(self.predictions)


class _RecordingExecutor:
    def __init__(self, *, order_id: str = "41", placed: bool = True):
        self.calls: list[dict] = []
        self.order_id = order_id
        self.placed = placed

    async def place_order_for_execution(self, *, strategy, code, signal_date, prediction_id, direction):
        self.calls.append(
            {"strategy_id": strategy.get("id"), "code": code, "signal_date": signal_date,
             "prediction_id": prediction_id, "direction": direction}
        )
        if not self.placed:
            return {"placed": False, "reason": "no_closed_price", "prediction_id": prediction_id}
        return {"placed": True, "order_id": self.order_id, "prediction_id": prediction_id}


@pytest.mark.asyncio
async def test_ordered_decision_places_an_order_and_writes_back_its_id() -> None:
    """A gate that says "order" must actually place one, and record the id.

    Without this the gate reports ``ordered`` while no paper order exists, and
    ``prediction_executions.paper_order_id`` stays NULL — the gap the plan's
    §13.4 named.
    """
    db = _ActiveModelDB([_prediction(confidence=0.99)])
    executor = _RecordingExecutor(order_id="41")
    result = await SelectiveGate().evaluate_session(
        db,
        signal_date=date(2026, 10, 16),
        strategies={"s-1": {"id": "s-1"}},
        executor=executor,
    )
    assert result["ordered"] == 1
    assert result["orders_placed"] == 1
    # The order carries the decision day (prediction_as_of 10-15); paper
    # settlement fills it at the open of the next bar, the 10-16 target.
    assert executor.calls == [
        {"strategy_id": "s-1", "code": "600519", "signal_date": "2026-10-15",
         "prediction_id": "tp-1", "direction": "up"}
    ]
    # paper_order_id is bound to the real integer order id, not a string.
    order_arg = [arg for arg in db.written[0] if arg == 41]
    assert order_arg, "paper_order_id 41 was not written to prediction_executions"


@pytest.mark.asyncio
async def test_ordered_but_unfillable_is_recorded_not_counted_as_placed() -> None:
    db = _ActiveModelDB([_prediction(confidence=0.99)])
    executor = _RecordingExecutor(placed=False)
    result = await SelectiveGate().evaluate_session(
        db,
        signal_date=date(2026, 10, 16),
        strategies={"s-1": {"id": "s-1"}},
        executor=executor,
    )
    assert result["ordered"] == 1
    assert result["orders_placed"] == 0


@pytest.mark.asyncio
async def test_ordered_without_a_loaded_strategy_is_refused() -> None:
    db = _ActiveModelDB([_prediction(confidence=0.99)])
    executor = _RecordingExecutor()
    result = await SelectiveGate().evaluate_session(
        db,
        signal_date=date(2026, 10, 16),
        strategies={},
        executor=executor,
    )
    assert result["ordered"] == 1
    assert result["orders_placed"] == 0
    assert executor.calls == []


@pytest.mark.asyncio
async def test_abstained_decisions_never_reach_the_executor() -> None:
    db = _GateDB([_prediction()])  # no active model -> abstain
    executor = _RecordingExecutor()
    result = await SelectiveGate().evaluate_session(
        db,
        signal_date=date(2026, 10, 16),
        strategies={"s-1": {"id": "s-1"}},
        executor=executor,
    )
    assert result["abstained"] == 1
    assert executor.calls == []


@pytest.mark.asyncio
async def test_stale_prediction_does_not_match_a_later_session() -> None:
    db = _GateDB([_prediction(prediction_id="old", prediction_as_of="2026-04-01", target_trading_date="2026-04-02")])
    matched = await find_predictions_for_session(db, signal_date=date(2026, 10, 16))
    assert matched == []


@pytest.mark.asyncio
async def test_only_latest_as_of_matches_even_inside_the_window() -> None:
    db = _GateDB(
        [
            _prediction(prediction_id="old", prediction_as_of="2026-10-12", target_trading_date="2026-10-13"),
            _prediction(prediction_id="new", prediction_as_of="2026-10-15", target_trading_date="2026-10-16"),
        ]
    )
    matched = await find_predictions_for_session(db, signal_date=date(2026, 10, 16))
    assert [row["prediction_id"] for row in matched] == ["new"]


@pytest.mark.asyncio
async def test_scheduler_writes_prediction_and_executes_ordered_decision_end_to_end(
    monkeypatch,
) -> None:
    """§13.4 gap: with both hitrate toggles on, the scheduler must (a) write the
    prediction for the just-closed bar, (b) place a real paper order for every
    ``ordered`` gate decision, and (c) persist that order id back into
    ``prediction_executions.paper_order_id``. Before this wiring the gate
    reported ``ordered`` while ``paper_order_id`` stayed NULL.

    Assertions are at three levels:

    * (a) the writer saved a prediction with ``prediction_as_of`` derived from
      the closed bar (never the wall clock) and the target from the calendar
      fallback;
    * (b) the gate's production code path (``find_predictions_for_session``
      → ``decide`` → ``place_order_for_execution``) produced one real paper
      order and one execution record;
    * (c) the execution record's ``paper_order_id`` bound to the placed order's
      integer id.
    """
    import strategy_factory.application.hitrate_toggles as toggles
    from strategy_factory.application.research.paper_trading_scheduler import PaperTradingScheduler

    monkeypatch.setenv("STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED", "1")
    assert toggles.daily_prediction_writing_enabled() is True
    assert toggles.selective_gate_enabled() is True

    saved: list[dict] = []
    persisted: list[tuple] = []

    class _SchedulerDB(_FakePaperDb):
        def __init__(self):
            super().__init__()
            self.conn.executescript(
                """CREATE TABLE strategy_trade_predictions (
                    prediction_id TEXT PRIMARY KEY, strategy_id TEXT, stock_code TEXT,
                    prediction_as_of TEXT, target_trading_date TEXT, direction TEXT,
                    confidence REAL, horizon TEXT
                );"""
            )

        async def save_strategy_trade_prediction(self, payload):
            saved.append(payload)
            # Persist under the same identity the gate searches by, but carry a
            # non-null confidence so the calibration model can produce a
            # calibrated number and the ordered path is exercised. The
            # writer's honest "no rule confidence" (None) is asserted in
            # section (a); here the seeded row stands in for the production
            # case where the strategy family does carry a calibrated probability.
            contract = payload["contract_json"]
            self.conn.execute(
                "INSERT INTO strategy_trade_predictions "
                "(prediction_id, strategy_id, stock_code, prediction_as_of, "
                "target_trading_date, direction, confidence, horizon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["prediction_id"],
                    contract["strategy_id"],
                    contract["stock_code"],
                    contract["prediction_as_of"],
                    contract["target_trading_date"],
                    contract["direction"],
                    0.95,
                    contract.get("horizon"),
                ),
            )
            self.conn.commit()
            return payload

        async def fetch(self, query: str, *args):
            if "calibration_models" in query:
                return [
                    {
                        "model_hash": "cm_1",
                        "params_json": '{"a": 40.0, "b": -30.0}',
                        "feature_version": "v1",
                        "fit_end": "2026-10-01",
                    }
                ]
            if "trading_dates" in query:
                # The writer's calendar lookup is answered from the fake table
                # (as_of=2026-10-15 → next session 2026-10-16). The table stores
                # compact YYYYMMDD strings, so increment the digit string.
                if "$1" in query:
                    day = str(args[0])
                    token = day[-8:] if len(day) >= 8 else day
                    try:
                        next_day = date(int(token[:4]), int(token[4:6]), int(token[6:8])) + __import__("datetime").timedelta(days=1)
                        nxt = next_day.strftime("%Y%m%d")
                    except ValueError:
                        nxt = None
                    return [{"next": nxt}]
                return []
            return await super().fetch(query, *args)

        async def execute(self, query: str, *args):
            if "prediction_executions" in query:
                persisted.append((query, args))
                return 1
            await super().execute(query, *args)

    db = _SchedulerDB()
    db.strategies = [
        {
            "id": "strategy-1",
            "strategy_type": "momentum",
            "name": "momentum",
            "status": "incubating",
            "params": {
                "target_symbols": ["600519"],
                "backtest_win_rate": 0.5,
                "candidate_contract_hash": "candidate-1",
                "tested_object_hash": "tested-1",
            },
        }
    ]
    # 31 bars with the last on the signal day; the bridge requires >= 30 closed bars.
    db.histories = {
        "600519": [
            {"date": f"2026-10-{day:02d}", "close": 10.0 + day * 0.01, "volume": 100}
            for day in range(1, 16)
        ]
        + [
            {"date": f"2026-09-{day:02d}", "close": 9.0 + day * 0.01, "volume": 100}
            for day in range(16, 31)
        ]
    }

    import strategy_factory.application.research.paper_trading_scheduler as scheduler_module
    import strategy_factory.application.research.paper_trading_bridge as bridge_module
    import strategy_factory.application.research.daily_prediction_writer as writer_module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 16, 0, tzinfo=MARKET_TZ)

    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    monkeypatch.setattr(bridge_module, "datetime", Clock)
    monkeypatch.setattr(writer_module, "datetime", Clock)
    monkeypatch.setattr(scheduler_module, "_closed_bars", lambda bars, now=None: bars)
    _buy_signals(monkeypatch)

    scheduler = PaperTradingScheduler(db)
    result = await scheduler.run_daily_paper_trading_cycle(signal_date="2026-10-15")

    # (a) prediction written off the closed 2026-10-15 bar, next weekday target
    assert len(saved) == 1
    contract = saved[0]["contract_json"]
    assert contract["prediction_as_of"] == "2026-10-15"
    assert contract["target_trading_date"] == "2026-10-16"
    assert contract["direction"] == "up"
    assert contract["confidence"] is None

    # (b) the gate's production code path matched the prediction and placed
    # a real paper order for the ``ordered`` decision
    assert result["selective_gate"]["ordered"] == 1
    assert result["selective_gate"]["orders_placed"] == 1
    assert result["selective_gate"]["matched_predictions"] == 1
    assert len(db.orders) == 1
    order = db.orders[0]
    assert order["code"] == "600519"
    assert order["direction"] == "buy"

    # (c) the order id round-tripped into prediction_executions.paper_order_id
    assert persisted, "gate did not persist a prediction_executions row"
    write_sql, write_args = persisted[-1]
    assert "INSERT INTO prediction_executions" in write_sql
    assert write_args[12] == int(order["id"]), "paper_order_id was not bound to the placed order"

    assert result["order_count"] == 1


def test_closed_bars_drop_the_forming_session() -> None:
    bars = [
        {"date": "2026-10-16", "open": 10, "close": 11},
        {"date": "2026-10-19", "open": 11, "close": 12},
    ]
    # 2026-10-19 is a Monday; at 11:00 the bar is still forming.
    closed = _closed_bars(bars, now=datetime(2026, 10, 19, 11, 0, tzinfo=MARKET_TZ))
    assert [bar["date"] for bar in closed] == ["2026-10-16"]
    after_close = _closed_bars(bars, now=datetime(2026, 10, 19, 15, 30, tzinfo=MARKET_TZ))
    assert [bar["date"] for bar in after_close] == ["2026-10-16", "2026-10-19"]


class _WriterDB:
    def __init__(self, calendar=None):
        self.saved: list[dict] = []
        self.calendar = calendar or []

    async def fetch(self, sql, *args):
        return self.calendar

    async def save_strategy_trade_prediction(self, payload):
        self.saved.append(payload)
        return payload


@pytest.mark.asyncio
async def test_writer_derives_target_from_calendar_and_never_wall_clock() -> None:
    db = _WriterDB(calendar=[{"next": "20261019"}])
    writer = DailyPredictionWriter()
    result = await writer.write(
        db,
        strategy={"id": "s-1", "params": {"target_symbols": ["600519"]}},
        code="600519",
        klines=[{"date": "2026-10-16", "open": 10, "close": 11}],
        signal=1,
        now=datetime(2026, 10, 18, 12, 0, tzinfo=MARKET_TZ),  # a Sunday
    )
    assert result["status"] == "ok"
    assert result["as_of"] == "2026-10-16"
    assert result["target_trading_date"] == "2026-10-19"
    assert result["calendar_source"] == "trading_calendar"
    contract = db.saved[0]["contract_json"]
    assert contract["prediction_as_of"] == "2026-10-16"
    assert contract["confidence"] is None  # no rule constant is invented


@pytest.mark.asyncio
async def test_writer_falls_back_to_weekday_and_reports_it() -> None:
    db = _WriterDB(calendar=[])
    writer = DailyPredictionWriter()
    result = await writer.write(
        db,
        strategy={"id": "s-1"},
        code="600519",
        klines=[{"date": "2026-10-16", "open": 10, "close": 11}],
        signal=-1,
    )
    assert result["calendar_source"] == "weekday_fallback"
    assert result["direction"] == "down"


@pytest.mark.asyncio
async def test_writer_is_idempotent_per_strategy_symbol_day() -> None:
    db = _WriterDB(calendar=[{"next": "20261019"}])
    writer = DailyPredictionWriter()
    args = dict(
        strategy={"id": "s-1"},
        code="600519",
        klines=[{"date": "2026-10-16", "open": 10, "close": 11}],
        signal=1,
    )
    first = await writer.write(db, **args)
    second = await writer.write(db, **args)
    assert first["prediction_id"] == second["prediction_id"]
