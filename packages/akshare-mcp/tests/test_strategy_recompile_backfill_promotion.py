"""Tests for trend strategy recompile backfill + observe->formal promotion (P0-b / P1)."""

from __future__ import annotations

import math

import pytest

from akshare_mcp.services.strategy_recompile_backfill import (
    backfill_historical_trend_strategies,
    build_trend_strategy_recompile_backfill,
    build_factor_strategy_recompile_backfill,
    _recompiled_formal_ready,
    _prioritize_recompile_rows,
)


def _synth_trend_klines(n: int = 220, *, start: float = 10.0) -> list[dict]:
    rows: list[dict] = []
    close = start
    prev_close = start
    for i in range(n):
        ret = 0.004 + 0.02 * math.sin(i / 7.0)
        close = max(0.5, prev_close * (1.0 + ret))
        high = close * (1.0 + 0.015 + 0.005 * abs(math.sin(i / 3.0)))
        low = close * (1.0 - 0.015 - 0.005 * abs(math.cos(i / 3.0)))
        open_ = prev_close * (1.0 + 0.003 * math.sin(i / 5.0))
        volume = 1_000_000 * (1.0 + 0.4 * abs(math.sin(i / 4.0)))
        rows.append(
            {
                "time": f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                "code": "600000",
                "open": round(open_, 4),
                "high": round(max(high, open_, close), 4),
                "low": round(min(low, open_, close), 4),
                "close": round(close, 4),
                "volume": round(volume, 2),
                "turnover": round(volume * close, 2),
            }
        )
        prev_close = close
    return rows


def _strong_signal_stats() -> dict:
    """强前向 skill:5d/10d hit_rate≈0.62,样本充足 → primary_skill_lcb>0。

    signal_stats 的指标按 horizon (1/5/10/20) 分桶,derive_signal_quality 默认 primary=5d。
    """
    return {
        "raw_signal_count": 60,
        "signals_with_forward_returns_count": 60,
        "hit_rate": {1: 0.6, 5: 0.62, 10: 0.6, 20: 0.58},
        "hit_rate_lcb": {1: 0.55, 5: 0.57, 10: 0.55, 20: 0.53},
        "null_hit_rate": {1: 0.5, 5: 0.5, 10: 0.5, 20: 0.5},
        "sample_count": {1: 60, 5: 60, 10: 60, 20: 55},
        "effective_n": {1: 40, 5: 40, 10: 35, 20: 30},
    }


def _weak_signal_stats() -> dict:
    """弱前向 skill:hit_rate_lcb 低于 null → primary_skill_lcb<=0,应被转正门拦住。"""
    return {
        "raw_signal_count": 60,
        "signals_with_forward_returns_count": 60,
        "hit_rate": {1: 0.48, 5: 0.47, 10: 0.46, 20: 0.45},
        "hit_rate_lcb": {1: 0.42, 5: 0.41, 10: 0.40, 20: 0.39},
        "null_hit_rate": {1: 0.5, 5: 0.5, 10: 0.5, 20: 0.5},
        "sample_count": {1: 60, 5: 60, 10: 60, 20: 55},
        "effective_n": {1: 40, 5: 40, 10: 35, 20: 30},
    }


def _thin_signal_stats() -> dict:
    """前向样本不足:effective_n 低于 min_effective_n(12),即便方向对也不放行。"""
    return {
        "raw_signal_count": 6,
        "signals_with_forward_returns_count": 6,
        "hit_rate": {5: 0.7, 10: 0.68},
        "hit_rate_lcb": {5: 0.6, 10: 0.58},
        "null_hit_rate": {5: 0.5, 10: 0.5},
        "sample_count": {5: 6, 10: 6},
        "effective_n": {5: 4, 10: 3},
    }


class _FakeDB:
    def __init__(
        self,
        strategies: list[dict],
        quality_reports: dict[str, dict] | None = None,
        signal_stats: dict[str, dict] | None = None,
    ) -> None:
        self._strategies = {s["id"]: dict(s) for s in strategies}
        self._quality_reports = dict(quality_reports or {})
        # 默认给每个策略一份强前向 skill 统计,使结构性 readiness 满足者能通过新的
        # 前向 skill 转正门。需测试"弱/缺前向 skill 被门拦住"时传入显式 signal_stats
        # 或 signal_stats={} 关闭默认。
        self._signal_stats = dict(signal_stats) if signal_stats is not None else None
        self.saved: list[dict] = []
        self.klines = _synth_trend_klines(220)

    async def get_strategy(self, strategy_id):
        s = self._strategies.get(strategy_id)
        return dict(s) if s else None

    async def list_strategies(self, status=None, limit=None, offset=0):
        rows = list(self._strategies.values())
        if status:
            statuses = status if isinstance(status, (list, tuple, set)) else [status]
            rows = [r for r in rows if r.get("status") in set(statuses)]
        return [dict(r) for r in rows[offset: (offset + limit) if limit else None]]

    async def get_strategy_metrics(self, strategy_id):
        return [{"period": "backtest", "trade_count": 14, "max_drawdown": 0.09}]

    async def get_strategy_quality_report(self, strategy_id, report_type="submission"):
        _ = report_type
        report = self._quality_reports.get(strategy_id)
        return dict(report) if report else None

    async def get_signal_stats(self, strategy_id):
        if self._signal_stats is not None:
            return dict(self._signal_stats.get(strategy_id) or {})
        # 默认强前向 skill:5d/10d hit_rate 高、样本充足 → primary_skill_lcb>0。
        return _strong_signal_stats()

    async def get_klines(self, code, start_date=None, end_date=None, limit=None):
        rows = list(self.klines)
        return rows[-limit:] if limit else rows

    async def get_financials(self, code, limit=4):
        return [
            {
                "code": code,
                "report_date": "2025-03-31",
                "roe": 0.16,
                "roa": 0.09,
                "gross_margin": 0.43,
                "net_margin": 0.22,
                "eps": 1.3,
                "bvps": 8.7,
                "debt_ratio": 0.38,
                "current_ratio": 1.9,
                "revenue_growth": 0.19,
                "profit_growth": 0.23,
            }
        ]

    async def save_strategy(self, payload):
        self.saved.append(dict(payload))
        self._strategies[payload["id"]] = dict(payload)
        return dict(payload)


def _momentum_strategy(strategy_id: str = "mom-1", *, strict_ready: bool = True) -> dict:
    from strategy_factory.application.trade_prediction_contract import (
        freeze_trade_prediction_contract,
    )

    frozen = freeze_trade_prediction_contract(
        {
            "strategy_id": strategy_id,
            "stock_code": "600000",
            "prediction_as_of": "2026-06-05T09:30:00+08:00",
            "target_trading_date": "2026-06-08",
            "direction": "up",
            "confidence": 0.7,
            "horizon": "next_day",
            "evidence_refs": ["ev-1"],
        }
    )
    return {
        "id": strategy_id,
        "name": "momentum single name",
        "strategy_type": "momentum",
        "status": "submitted",
        "target_symbols": ["600000"],
        "params": {
            "target_symbols": ["600000"],
            "submission_lane": "observe_incubation",
            "strict_incubation_ready": strict_ready,
            "trade_prediction_contract": frozen["contract"],
            "trade_prediction_contract_status": frozen["status"],
            "trade_prediction_contract_hash": frozen["contract_hash"],
            "holding_horizon": {"min_days": 5, "max_days": 20},
            "trade_plan": {"entry_bias": "momentum_breakout", "exit_bias": "momentum_decay"},
            "risk_rules": {"stop_loss_pct": 0.06, "take_profit_pct": 0.15},
            "evidence_chain": {"evidences": [{"evidence_id": "ev-1", "source": "price_runtime"}]},
            "confidence_contract": {
                "confidence": 0.7,
                "calibration": "historical_hit_rate",
                "sample_n": 40,
            },
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "c1",
                        "target_trading_date": "2026-06-08",
                        "horizon": "next_day",
                        "evidence_ids": ["ev-1"],
                    }
                ]
            },
        },
    }


def test_recompile_produces_compiled_dsl_with_measured_profile() -> None:
    strategy = _momentum_strategy()
    summary_rows = _synth_trend_klines(220)
    from akshare_mcp.services.instrument_profile_measurement import measure_instrument_profile

    summary = measure_instrument_profile(summary_rows)
    result = build_trend_strategy_recompile_backfill(
        strategy,
        backtest_metrics={"trade_count": 14},
        measured_profile_summary=summary,
    )
    assert result["status"] == "recompiled"
    params = dict(result["updated_payload"]["params"])
    assert params["execution_semantic_mode"] == "compiled_dsl"
    assert params["dsl_compiled"] is True
    profile = dict(params.get("instrument_profile") or {})
    assert profile.get("measurement_source") in {"measured", "measured_runtime"}
    assert profile.get("measured_profile_complete") is True


def test_recompile_refreshes_stale_execution_readiness_tier() -> None:
    """重编译必须用重算判决覆盖提交期冻结的旧 tier/blocker。

    回归用例:样本提交时 tier=missing_executable_contract(无 compiled_dsl),
    重编译补齐 DSL + measured profile 后,若 _merge_params 保留旧 tier,
    _recompiled_formal_ready 永远读到 missing_executable_contract 无法转正。
    """
    strategy = _momentum_strategy("mom-stale-tier")
    # 模拟提交期落库的陈旧判决字段
    strategy["params"]["execution_semantic_mode"] = "missing_executable_contract"
    strategy["params"]["execution_readiness_tier"] = "missing_executable_contract"
    strategy["params"]["diagnostic_only"] = True
    strategy["params"]["dsl_compiled"] = False

    summary_rows = _synth_trend_klines(220)
    from akshare_mcp.services.instrument_profile_measurement import measure_instrument_profile

    summary = measure_instrument_profile(summary_rows)
    result = build_trend_strategy_recompile_backfill(
        strategy,
        backtest_metrics={"trade_count": 14},
        measured_profile_summary=summary,
    )
    params = dict(result["updated_payload"]["params"])
    # 旧的 missing_executable_contract 判决必须被重算结果覆盖(样本带齐语义契约 → formal_runtime_ready)
    assert params["execution_readiness_tier"] != "missing_executable_contract"
    assert params["execution_readiness_tier"] == "formal_runtime_ready"
    assert params["dsl_compiled"] is True
    assert "execution_readiness_tier" in result["applied_param_fields"]


def test_recompile_does_not_fabricate_missing_semantic_contracts() -> None:
    """诚实边界:缺语义契约的样本重编译后 tier 只能到 observe_diagnostic_only,
    不得伪造 evidence/prediction/confidence 把它顶成 formal_runtime_ready。
    """
    strategy = _momentum_strategy("mom-no-contracts")
    for field in ("evidence_chain", "prediction_contract", "confidence_contract"):
        strategy["params"].pop(field, None)
    strategy["params"]["execution_readiness_tier"] = "missing_executable_contract"

    summary_rows = _synth_trend_klines(220)
    from akshare_mcp.services.instrument_profile_measurement import measure_instrument_profile

    summary = measure_instrument_profile(summary_rows)
    result = build_trend_strategy_recompile_backfill(
        strategy,
        backtest_metrics={"trade_count": 14},
        measured_profile_summary=summary,
    )
    params = dict(result["updated_payload"]["params"])
    assert params["execution_readiness_tier"] == "observe_diagnostic_only"
    assert params.get("diagnostic_only") is True
    assert set(params.get("semantic_contract_missing_fields") or []) >= {
        "evidence_chain",
        "prediction_contract",
        "confidence_contract",
    }
    assert not params.get("evidence_chain")
    assert not params.get("prediction_contract")
    assert not params.get("confidence_contract")


@pytest.mark.asyncio
async def test_backfill_promotes_ready_observe_to_formal() -> None:
    db = _FakeDB([_momentum_strategy("mom-promote", strict_ready=True)])
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-promote"], measure_profile=True, promote_ready=True
    )
    assert report["recompiled"] == 1
    assert report["promoted_to_formal"] == 1
    saved = db.saved[-1]
    assert saved["status"] == "incubating"
    assert dict(saved["params"]).get("submission_lane") == "formal_incubation"


@pytest.mark.asyncio
async def test_backfill_blocks_promotion_when_forward_skill_not_positive() -> None:
    # 结构性 readiness 满足,但真实前向 skill_lcb<=0 → 不升 formal(防伪转正)。
    db = _FakeDB(
        [_momentum_strategy("mom-weak", strict_ready=True)],
        signal_stats={"mom-weak": _weak_signal_stats()},
    )
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-weak"], measure_profile=True, promote_ready=True
    )
    assert report["recompiled"] == 1
    assert report["promoted_to_formal"] == 0
    assert report["promotion_forward_skill_blocked"] == 1
    if db.saved:
        assert db.saved[-1]["status"] != "incubating"
    item = next(i for i in report["items"] if i["strategy_id"] == "mom-weak")
    assert item["forward_skill_gate"]["reason"] == "forward_skill_lcb_not_positive"


@pytest.mark.asyncio
async def test_backfill_blocks_promotion_when_forward_samples_insufficient() -> None:
    # 前向样本量不足(effective_n < min)即便方向对也不放行。
    db = _FakeDB(
        [_momentum_strategy("mom-thin", strict_ready=True)],
        signal_stats={"mom-thin": _thin_signal_stats()},
    )
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-thin"], measure_profile=True, promote_ready=True
    )
    assert report["promoted_to_formal"] == 0
    assert report["promotion_forward_skill_blocked"] == 1
    item = next(i for i in report["items"] if i["strategy_id"] == "mom-thin")
    assert item["forward_skill_gate"]["reason"] == "insufficient_forward_samples"


@pytest.mark.asyncio
async def test_backfill_blocks_promotion_when_signal_stats_unavailable() -> None:
    # db 无 get_signal_stats(缺前向证据来源)→ 守诚实边界,不伪转正。
    db = _FakeDB([_momentum_strategy("mom-nostats", strict_ready=True)])
    db.get_signal_stats = None  # 使 callable() 判定为 False,模拟缺数据来源环境
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-nostats"], measure_profile=True, promote_ready=True
    )
    assert report["promoted_to_formal"] == 0
    item = next(i for i in report["items"] if i["strategy_id"] == "mom-nostats")
    assert item["forward_skill_gate"]["reason"] == "signal_stats_unavailable"


@pytest.mark.asyncio
async def test_backfill_forward_skill_gate_can_be_disabled(monkeypatch) -> None:
    # 关闭门(toggle=0)恢复旧行为:仅凭结构性条件转正(用于显式回退,不建议)。
    monkeypatch.setenv(
        "INCUBATION_FACTORY_RECOMPILE_PROMOTION_FORWARD_SKILL_GATE_ENABLED", "0"
    )
    db = _FakeDB(
        [_momentum_strategy("mom-gateoff", strict_ready=True)],
        signal_stats={"mom-gateoff": _weak_signal_stats()},
    )
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-gateoff"], measure_profile=True, promote_ready=True
    )
    # 门关闭 → 即使前向 skill 弱也转正(旧行为)
    assert report["promoted_to_formal"] == 1
    item = next(i for i in report["items"] if i["strategy_id"] == "mom-gateoff")
    assert item["forward_skill_gate"]["gate"] == "disabled"


@pytest.mark.asyncio
async def test_backfill_does_not_promote_without_strict_gate() -> None:
    db = _FakeDB([_momentum_strategy("mom-nostrict", strict_ready=False)])
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-nostrict"], measure_profile=True, promote_ready=True
    )
    # 重编译可发生,但缺 strict gate 证据 → 不升 formal
    assert report["promoted_to_formal"] == 0
    if db.saved:
        assert db.saved[-1]["status"] != "incubating"


@pytest.mark.asyncio
async def test_backfill_promotes_runtime_repairable_quality_pass_to_formal() -> None:
    strategy = _momentum_strategy("mom-runtime-repair", strict_ready=False)
    db = _FakeDB(
        [strategy],
        quality_reports={
            "mom-runtime-repair": {
                "passed": False,
                "summary": {
                    "review_passed": True,
                    "validation_grade": "A",
                    "admission_block_reasons": [
                        "default_profile_not_allowed_for_single_name_runtime",
                        "diagnostic_only_not_allowed_for_incubation",
                        "execution_readiness_tier:missing_executable_contract",
                    ],
                },
                "quality_gate": {
                    "admission_block_reasons": [
                        "default_profile_not_allowed_for_single_name_runtime",
                    ],
                },
            }
        },
    )
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-runtime-repair"], measure_profile=True, promote_ready=True
    )
    assert report["recompiled"] == 1
    assert report["promoted_to_formal"] == 1
    saved = db.saved[-1]
    assert saved["status"] == "incubating"
    saved_params = dict(saved["params"])
    assert saved_params["submission_lane"] == "formal_incubation"
    assert saved_params["formal_track_auto_corrected"] is True


@pytest.mark.asyncio
async def test_backfill_does_not_promote_non_repairable_quality_blocker() -> None:
    strategy = _momentum_strategy("mom-profit-factor-blocked", strict_ready=False)
    db = _FakeDB(
        [strategy],
        quality_reports={
            "mom-profit-factor-blocked": {
                "summary": {
                    "review_passed": True,
                    "validation_grade": "A",
                    "admission_block_reasons": [
                        "default_profile_not_allowed_for_single_name_runtime",
                        "profit_factor 1.332 < 1.800",
                    ],
                },
            }
        },
    )

    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["mom-profit-factor-blocked"], measure_profile=True, promote_ready=True
    )

    assert report["recompiled"] == 1
    assert report["promoted_to_formal"] == 0
    if db.saved:
        saved = db.saved[-1]
        assert saved["status"] == "submitted"
        assert dict(saved["params"]).get("submission_lane") != "formal_incubation"


@pytest.mark.asyncio
async def test_backfill_prioritizes_recent_repairable_observe_sample_with_small_limit() -> None:
    old_compiled = _momentum_strategy("mom-old-compiled", strict_ready=True)
    old_compiled["updated_at"] = "2026-06-01T00:00:00+00:00"
    old_compiled["params"].update(
        {
            "submission_lane": "observe_incubation",
            "execution_semantic_mode": "compiled_dsl",
            "dsl_compiled": True,
            "runtime_recompile_backfill": {"status": "recompiled"},
            "instrument_profile": {
                "measurement_source": "measured_runtime",
                "measured_profile_complete": True,
            },
            "execution_readiness_tier": "formal_runtime_ready",
            "semantic_runtime_match": True,
            "proxy_runtime_used": False,
            "diagnostic_only": False,
        }
    )
    recent_repairable = _momentum_strategy("mom-recent-repair", strict_ready=True)
    recent_repairable["updated_at"] = "2026-06-15T15:30:00+00:00"
    recent_repairable["params"].update(
        {
            "submission_lane": "observe_incubation",
            "formal_track_blockers": [
                "execution_readiness_tier:missing_executable_contract",
                "default_profile_not_allowed_for_single_name_runtime",
            ],
            "execution_semantic_mode": "missing_executable_contract",
            "execution_readiness_tier": "missing_executable_contract",
        }
    )
    db = _FakeDB([old_compiled, recent_repairable])

    report = await backfill_historical_trend_strategies(
        db,
        statuses=["submitted"],
        limit=1,
        batch_size=2,
        measure_profile=True,
        promote_ready=False,
    )

    assert report["scanned"] == 1
    assert report["items"][0]["strategy_id"] == "mom-recent-repair"
    assert db.saved[-1]["id"] == "mom-recent-repair"


def test_prioritize_recompile_rows_demotes_already_compiled_backlog() -> None:
    old_compiled = _momentum_strategy("mom-old-compiled", strict_ready=True)
    old_compiled["params"].update(
        {
            "submission_lane": "observe_incubation",
            "execution_semantic_mode": "compiled_dsl",
            "dsl_compiled": True,
            "runtime_recompile_backfill": {"status": "recompiled"},
        }
    )
    recent_repairable = _momentum_strategy("mom-recent-repair", strict_ready=True)
    recent_repairable["updated_at"] = "2026-06-15T15:30:00+00:00"
    recent_repairable["params"].update(
        {
            "submission_lane": "observe_incubation",
            "formal_track_blockers": ["missing_executable_contract"],
        }
    )

    prioritized = _prioritize_recompile_rows([old_compiled, recent_repairable], limit=1)

    assert [item["id"] for item in prioritized] == ["mom-recent-repair"]


def _quality_factor_strategy(strategy_id: str = "qf-1", *, strict_ready: bool = True) -> dict:
    from strategy_factory.application.trade_prediction_contract import (
        freeze_trade_prediction_contract,
    )

    frozen = freeze_trade_prediction_contract(
        {
            "strategy_id": strategy_id,
            "stock_code": "600000",
            "prediction_as_of": "2026-06-05T09:30:00+08:00",
            "target_trading_date": "2026-06-08",
            "direction": "up",
            "confidence": 0.68,
            "horizon": "next_day",
            "evidence_refs": ["ev-f1"],
        }
    )
    return {
        "id": strategy_id,
        "name": "quality factor single name",
        "strategy_type": "quality_factor",
        "status": "submitted",
        "target_symbols": ["600000"],
        "params": {
            "target_symbols": ["600000"],
            "submission_lane": "observe_incubation",
            "strict_incubation_ready": strict_ready,
            "trade_prediction_contract": frozen["contract"],
            "trade_prediction_contract_status": frozen["status"],
            "trade_prediction_contract_hash": frozen["contract_hash"],
            "holding_horizon": {"min_days": 30, "max_days": 84},
            "trade_plan": {"entry_bias": "cross_sectional_rank", "exit_bias": "rank_decay"},
            "risk_rules": {"stop_loss_pct": 0.08, "take_profit_pct": 0.18},
            "evidence_chain": {"evidences": [{"evidence_id": "ev-f1", "source": "fundamental_runtime"}]},
            "confidence_contract": {"confidence": 0.68, "calibration": "historical_hit_rate", "sample_n": 36},
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "cf1",
                        "target_trading_date": "2026-06-08",
                        "horizon": "next_day",
                        "evidence_ids": ["ev-f1"],
                    }
                ]
            },
        },
    }


def test_factor_recompile_attaches_fundamental_runtime_contract() -> None:
    from akshare_mcp.services.fundamental_runtime_contract import (
        build_fundamental_runtime_contract,
    )

    strategy = _quality_factor_strategy()
    contract = build_fundamental_runtime_contract(
        "quality_factor",
        [
            {
                "code": "600000",
                "report_date": "2025-03-31",
                "roe": 0.16,
                "roa": 0.09,
                "gross_margin": 0.43,
                "net_margin": 0.22,
            }
        ],
        code="600000",
    )
    result = build_factor_strategy_recompile_backfill(strategy, fundamental_contract=contract)
    assert result["status"] == "recompiled"
    params = dict(result["updated_payload"]["params"])
    assert params["runtime_family_data_source"] == "fundamental_runtime"
    assert params["fundamental_runtime_contract"]["measured_fields"]
    assert not params.get("proxy_runtime_used")


def test_factor_recompile_without_financials_stays_proxy() -> None:
    strategy = _quality_factor_strategy()
    result = build_factor_strategy_recompile_backfill(strategy, fundamental_contract=None)
    assert result["status"] == "revision_required"
    assert result["reason"] == "fundamental_runtime_contract_unavailable"


@pytest.mark.asyncio
async def test_backfill_promotes_factor_family_with_real_financials() -> None:
    db = _FakeDB([_quality_factor_strategy("qf-promote", strict_ready=True)])
    report = await backfill_historical_trend_strategies(
        db, strategy_ids=["qf-promote"], promote_ready=True
    )
    assert report["recompiled"] == 1
    assert report["promoted_to_formal"] == 1
    saved = db.saved[-1] if db.saved else {}
    assert saved.get("status") == "incubating"
    saved_params = dict(saved.get("params") or {})
    assert saved_params.get("runtime_family_data_source") == "fundamental_runtime"
    assert saved_params.get("submission_lane") == "formal_incubation"
    assert saved_params.get("fundamental_runtime_contract", {}).get("measured_fields")
