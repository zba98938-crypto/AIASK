from __future__ import annotations

from datetime import date, time as dt_time

import pytest

from strategy_factory.infrastructure.runtime_services import (
    clear_runtime_services,
    configure_runtime_services,
)


@pytest.fixture(autouse=True)
def _reset_runtime_services():
    clear_runtime_services()
    yield
    clear_runtime_services()


@pytest.mark.asyncio
async def test_incubation_runtime_uses_configured_support_factory() -> None:
    class _Runner:
        def __init__(
            self,
            *,
            run_time=None,
            dry_run=False,
            auto_apply_review=True,
            owns_paper_trading=None,
        ) -> None:
            self.run_time = run_time
            self.dry_run = dry_run
            self.auto_apply_review = auto_apply_review
            self.owns_paper_trading = owns_paper_trading
            self._error_backoff_sec = 1
            self._run_count = 0
            self._error_count = 0
            self._last_run_at = None
            self._last_result = None
            self._intake = type("Intake", (), {"scan_and_accept": staticmethod(_scan_and_accept)})()
            self._signal_generator = type("Signals", (), {"generate": staticmethod(_generate)})()
            self._forward_verifier = type("Verifier", (), {"verify": staticmethod(_verify)})()
            self._metrics_recorder = type("Metrics", (), {"record": staticmethod(_record)})()
            self._trade_prediction_verifier = type("TradePred", (), {"verify_pending": staticmethod(_verify_pending)})()
            self._reporter = type("Reporter", (), {"generate": staticmethod(_report)})()
            self._feedback_writer = type("Feedback", (), {"write": staticmethod(_write)})()
            self._accelerator = type("Accel", (), {"evaluate_batch": staticmethod(_accelerate)})()
            self._alert_monitor = type("Alert", (), {"check": staticmethod(_check)})()

        def status(self) -> dict:
            return {
                "run_time": str(self.run_time),
                "dry_run": self.dry_run,
                "auto_apply_review": self.auto_apply_review,
                "owns_paper_trading": self.owns_paper_trading,
            }

        async def _get_db(self):
            return object()

        async def _close_db(self, _db):
            return None

        async def _run_recompile_remediation(self, _db):
            return {}

        async def _list_incubating(self, _db):
            return []

        async def _list_paper_observation(self, _db):
            return []

        async def _list_diagnostic_observation(self, _db):
            return []

        async def _run_signal_only_paper_execution_backlog(self, _db, *, strategies=None):
            return {}

        async def _run_exit_signal_paper_execution(self, _db, *, strategies=None, as_of=None):
            return {}

        async def _run_stale_paper_position_closure(self, _db, *, strategies=None):
            return {}

        async def _run_native_execution_evidence_backfill(self, _db, *, strategies=None):
            return {}

        async def _run_execution_audit_acceptance(self, _db, *, strategies=None):
            return {}

        async def _run_execution_audit_remediation(self, _db, *, strategies=None, acceptance_result=None):
            return {}

        async def _run_pipeline(self, _db, *, strategies=None):
            return {"count": 0, "auto_promoted": 0, "stage_counts": {}}

        async def _heartbeat(self, _db, _run_id):
            return None

        async def _start_paper_trading_daemons(self) -> None:
            self.started = True

        async def _stop_paper_trading_daemons(self) -> None:
            self.stopped = True

    async def _scan_and_accept(_db):
        return {"accepted": 0}

    async def _generate(_db, _strategy):
        return {"signals_generated": 0}

    async def _verify(_db, _strategy):
        return {}

    async def _record(_db, _strategy, _verification):
        return None

    async def _verify_pending(_db, **_kwargs):
        return {}

    async def _report(_db, _all, _verifications, _pipeline, **_kwargs):
        return {"hit_rate_dashboard": {"overall": {}}}

    async def _write(_db, _report):
        return {}

    async def _accelerate(_db, _incubating, _verifications):
        return {}

    async def _check(_db, run_result=None):
        return {}

    configure_runtime_services(
        incubation_runtime_factory=_Runner,
        incubation_runtime_support_factory=_Runner,
    )

    from strategy_factory.runtime.incubation import build_incubation_runtime

    runtime = build_incubation_runtime(
        run_time=dt_time(19, 15),
        dry_run=True,
        owns_paper_trading=False,
    )

    assert runtime.preflight()["runtime_type"] == "_Runner"
    assert runtime.status()["dry_run"] is True
    assert (await runtime.run_once())["status"] == "completed"


@pytest.mark.asyncio
async def test_signal_tracker_runtime_uses_configured_support_factory() -> None:
    class _Tracker:
        def __init__(self) -> None:
            self.run_time = None
            self.last_run = None
            self.last_result = None
            self._forward_days = [1]
            self._forward_return_batch_limit = 10
            self._forward_return_max_rounds = 2

        def status(self) -> dict:
            return {"running": False, "provider": "test"}

        def _phase_timeout_seconds(self, _phase_name: str) -> float:
            return 5.0

        def _get_default_universe(self) -> list[str]:
            return ["600519"]

        async def _load_executable_strategies_with_fallback(self, db, *, limit=500, use_contract=True):
            del db, limit, use_contract
            return [
                {
                    "id": "s1",
                    "strategy_type": "missing_strategy_type",
                    "params": {},
                    "status": "incubating",
                }
            ]

        async def _load_runtime_submitted_strategies(self, db, *, limit=200):
            del db, limit
            return []

        async def _load_runtime_observation_strategies(self, db, *, limit=200):
            del db, limit
            return []

        @staticmethod
        def _merge_unique_strategies(*groups):
            merged = []
            seen = set()
            for group in groups:
                for item in list(group or []):
                    sid = str((item or {}).get("id") or "")
                    if sid and sid not in seen:
                        seen.add(sid)
                        merged.append(item)
            return merged

        @staticmethod
        def _get_runtime_control_service():
            class _Control:
                @staticmethod
                def is_blocking_mode(_mode):
                    return False

            return _Control()

        @staticmethod
        async def backfill_forward_returns(_db, *, forward_days_list=None, batch_limit=0, max_rounds=0):
            del forward_days_list, batch_limit, max_rounds
            return {"computed": 0, "windows": {"1D": {"stalled": False}}}

        @staticmethod
        async def _run_lifecycle_scan(_db):
            return {"transitions": []}

        @staticmethod
        def _resolve_strategy_universe(_strategy, default_universe):
            return list(default_universe or [])

        @staticmethod
        async def _get_klines_with_fallback(_db, _code, limit=200):
            del limit
            return []

        @staticmethod
        def _build_signal_tracking_artifacts(_instance, _klines, *, execution_semantic_mode: str):
            del execution_semantic_mode
            return {"snapshot": None, "signal_row": None}

        @staticmethod
        def _resolve_signal_record_date(_signal_row):
            return None

    class _Db:
        async def initialize(self):
            return None

        async def save_strategy_task_run(self, payload):
            self.payload = dict(payload)
            return {"id": 11, "trace_id": "trace-1"}

        async def get_strategy_runtime_control(self, _strategy_id):
            return {}

        async def update_strategy_task_run(self, task_run_id, **kwargs):
            self.updated = (task_run_id, dict(kwargs))
            return None

        async def save_strategy_domain_event(self, payload):
            self.event = dict(payload)
            return None

    class _IncubationService:
        async def process_strategies(self, _db, strategies, signal_date=None):
            del signal_date
            return {
                "orders_created": len(strategies),
                "orders_filled": 0,
                "nav_snapshots": 0,
                "metrics_recorded": 0,
            }

    class _PipelineService:
        async def run_batch(self, _db, **_kwargs):
            return {"count": 0, "auto_promoted": 0}

        async def run_strategy(self, _db, _strategy, **_kwargs):
            return {"ok": True}

    class _RiskService:
        async def scan(self, _db, strategies, enforce_actions=True):
            del enforce_actions
            return {"event_count": len(strategies), "action_count": 0}

    class _VectorGovernanceService:
        async def reconcile_registry(self, _db, **_kwargs):
            return {"registry_updated": 0}

    class _DomainProjectionService:
        async def rebuild_batch(self, _db, **_kwargs):
            return {"count": 0}

    db = _Db()

    configure_runtime_services(
        db_provider=lambda: db,
        signal_tracker_runtime_factory=lambda: _Tracker(),
        signal_tracker_runtime_support_factory=lambda run_time=None: _Tracker(),
        strategy_incubation_service_factory=lambda: _IncubationService(),
        strategy_incubation_pipeline_service_factory=lambda: _PipelineService(),
        strategy_runtime_risk_service_factory=lambda: _RiskService(),
        strategy_vector_governance_service_factory=lambda: _VectorGovernanceService(),
        strategy_domain_projection_service_factory=lambda: _DomainProjectionService(),
        strategy_lifecycle_scan_runner=_Tracker._run_lifecycle_scan,
    )

    from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime

    runtime = get_signal_tracker_runtime()
    result = await runtime.run_once()

    assert runtime.preflight()["runtime_type"] == "_Tracker"
    assert runtime.status()["provider"] == "test"
    assert result["signals_generated"] == 0
    assert result["risk_events"] == 1
    assert result["runtime_universe"]["strategies"] == 1
    assert result["task_run_id"] == 11


@pytest.mark.asyncio
async def test_market_event_ingest_runtime_uses_configured_support_factory() -> None:
    class _Db:
        def __init__(self) -> None:
            self.saved: list[tuple[str, str, list[dict[str, object]]]] = []

        async def initialize(self) -> None:
            return None

        async def save_market_documents(
            self,
            stock_code,
            doc_type,
            items,
            *,
            embed=True,
            chunk_size=0,
            overlap=0,
            version="v1",
        ):
            del embed, chunk_size, overlap, version
            self.saved.append((stock_code, doc_type, list(items)))
            return {"documents": len(items), "chunks": len(items), "embedded_chunks": 0, "headline_labels": 0}

    class _Support:
        def status(self) -> dict:
            return {"provider": "test"}

        @staticmethod
        def _resolve_runtime_args(kwargs: dict[str, object]) -> dict[str, object]:
            return {
                "requested_doc_types": ["notice"],
                "requested_codes": list(kwargs.get("stock_codes") or []),
                "news_limit": 0,
                "notice_limit": 1,
                "official_notice_limit": 1,
                "notice_days": 5,
                "code_notice_limit": 0,
                "code_notice_code_limit": 0,
                "research_code_limit": 0,
                "research_per_code": 0,
                "chunk_size": 1000,
                "overlap": 120,
                "version": "v1",
                "embed": False,
                "build_snapshot": False,
                "activate_snapshot": False,
                "allow_network": True,
                "dry_run": False,
                "start_date": date(2026, 6, 1),
                "end_date": date(2026, 6, 6),
                "args_payload": {"stock_codes": list(kwargs.get("stock_codes") or []), "allow_network": True},
            }

        @staticmethod
        def _event_source_status() -> dict[str, object]:
            return {"cninfo": {"status": "ok"}}

        @staticmethod
        async def _ingest_news(*_args, **_kwargs) -> None:
            raise AssertionError("news ingestion should not run")

        @staticmethod
        async def _build_market_doc_snapshots(_db, *, doc_types, activate, dry_run):
            del doc_types, activate, dry_run
            return []

        @staticmethod
        async def _load_final_counts(_db) -> dict[str, object]:
            return {"market_documents_by_type": [{"doc_type": "notice", "rows": 2}]}

        @staticmethod
        async def _bridge_normalized_events_to_strategy_factory(_db, *, limit=50) -> dict[str, object]:
            return {"enabled": True, "bridged_events": 1, "signals": 1, "limit": limit}

        @staticmethod
        def _fetch_official_market_event_documents(*_args, **_kwargs) -> dict[str, object]:
            return {
                "items": [
                    {"code": "600519", "stock_code": "600519", "title": "official notice 1"},
                    {"code": "000001", "stock_code": "000001", "title": "official notice 2"},
                ],
                "sources": {"cninfo": {"status": "ok", "fetched": 2}},
                "degraded_count": 0,
            }

        @staticmethod
        def _clean_text(value, limit=20000):
            text = str(value or "")
            return text[:limit]

        @staticmethod
        def _merge_saved_totals(target: dict[str, object], saved: dict[str, object]) -> None:
            for key in ("documents", "chunks", "embedded_chunks", "headline_labels"):
                target[key] = int(target.get(key) or 0) + int(saved.get(key) or 0)

        @staticmethod
        async def _persist_normalized_events(_db, stock_code: str, doc_type: str, items) -> dict[str, object]:
            return {
                "total": len(list(items)),
                "verified": len(list(items)),
                "provisional": 0,
                "degraded": 0,
                "rejected": 0,
                "latest": [f"{stock_code}:{doc_type}"],
            }

        @staticmethod
        def _merge_event_summary(bucket: dict[str, object], event_summary: dict[str, object]) -> None:
            for key in ("total", "verified", "provisional", "degraded", "rejected"):
                bucket[key] = int(bucket.get(key) or 0) + int(event_summary.get(key) or 0)
            bucket.setdefault("latest", []).extend(list(event_summary.get("latest") or []))

        @staticmethod
        def _fetch_notice_head(*_args, **_kwargs):
            return []

        @staticmethod
        def _map_notice_item(item):
            return dict(item)

        @staticmethod
        async def _insert_news_cache(_db, _rows, *, stock_code, news_type):
            del stock_code, news_type
            return 0

        @staticmethod
        async def _select_stock_universe(_db, *, limit, extra_codes):
            del limit, extra_codes
            return []

        @staticmethod
        async def _fetch_code_notice_items(**_kwargs):
            return []

        @staticmethod
        async def _ingest_research(*_args, **_kwargs):
            raise AssertionError("research ingestion should not run")

    db = _Db()
    configure_runtime_services(
        db_provider=lambda: db,
        market_event_ingest_runner=lambda *_args, **_kwargs: None,
        market_event_ingest_support_factory=lambda: _Support(),
    )

    from strategy_factory.runtime.market_event_ingest import get_market_event_ingest_runtime

    runtime = get_market_event_ingest_runtime()
    result = await runtime.run_once(stock_codes=["600519"], allow_network=True)

    assert runtime.preflight()["db_provider_available"] is True
    assert runtime.preflight()["runtime_type"] == "_Support"
    assert runtime.status()["provider"] == "test"
    assert result["saved"]["official_notice"]["documents"] == 2
    assert result["strategy_factory_bridge"]["bridged_events"] == 1
    assert result["final_counts"]["market_documents_by_type"][0]["doc_type"] == "notice"
    assert [item[0] for item in db.saved] == ["600519", "000001"]


@pytest.mark.asyncio
async def test_factor_mining_runtime_uses_configured_support_factory() -> None:
    class _Pool:
        size = 8

        async def admit_batch(self, validated):
            return [{"record": {"name": item.name, "validation_summary": {"quality_status": "promoted"}}} for item in validated]

    class _Scheduler:
        last_engines_used = ["rule"]

        def status(self) -> dict:
            return {"initialized": True, "pool_size": 7}

        async def search(self, **kwargs) -> list:
            candidate = type("Candidate", (), {})()
            candidate.name = "factor_a"
            candidate.quick_evidence = {"passed": True}
            candidate.generation_engine = "rule"
            candidate.blueprint_id = "bp_a"
            candidate.expression_dsl = "ts_mean(close, 20)"
            candidate.fitness = 1.0
            return [candidate]

    class _Evolution:
        async def evolve(self, *, candidates, **_kwargs):
            return list(candidates)

    class _DecayMonitor:
        async def daily_check(self, _pool, db=None):
            return {"updated_records": [], "measurements": [], "db": db}

    class _Support:
        def __init__(self) -> None:
            self._initialized = True
            self._last_run_at = None
            self._run_count = 0
            self._pool_loaded_from_db = True
            self._engine_scheduler = _Scheduler()
            self._evolutionary_optimizer = _Evolution()
            self._active_pool = _Pool()
            self._decay_monitor = _DecayMonitor()
            self.persisted: list[dict] = []

        def status(self) -> dict:
            return {"initialized": True, "pool_size": 7}

        def _ensure_initialized(self):
            return None

        async def _get_db(self):
            return object()

        async def _ensure_persistent_pool(self, _db):
            return None

        async def _build_mining_context(self, *, db, codes=None):
            context = type("Context", (), {})()
            context.validation_codes = ["000001"] * 200
            context.validation_universe_health = {"codes": len(context.validation_codes)}
            return context

        def _install_quick_evidence_evaluators(self, db, context):
            async def _ic(_candidate):
                return 0.1
            return _ic

        async def _quick_filter_candidates(self, candidates, context):
            return list(candidates)

        async def _validate_batch(self, db, candidates, context):
            for item in candidates:
                item.validation_result = {"success": True, "rating": {"grade": "A"}}
            return list(candidates)

        def _build_quality_summary(self, raw, evolved, validated, admitted, context):
            return {
                "quarantine_count": 0,
                "active_promoted_count": 1,
                "quality_funnel": {"promoted": 1},
            }

        async def _persist_admitted_factors(self, db, admitted):
            return None

        async def _record_feedback(self, run_id, raw, evolved, validated, admitted):
            return None

        async def _persist_mining_run(self, db, report):
            self.persisted.append(dict(report))

        async def _reappraise_quarantine_factors(self, db, *, limit=200):
            return {"scanned": 0, "promoted": 0, "kept_quarantine": 0}

        async def _persist_decay_report(self, db, decay_report):
            return None

        async def _persist_decay_updates(self, db, decay_report):
            return None

        async def _promote_quarantine_factors(self, db):
            return {"promoted_count": 0}

        async def _run_qc_pipeline(self, db):
            return {"enabled": False, "skipped": True}

    configure_runtime_services(
        factor_mining_support_factory=lambda: _Support(),
        factor_mining_factory=lambda: object(),
    )

    from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

    runtime = get_factor_mining_runtime()

    assert runtime.preflight()["runtime_type"] == "_Support"
    assert runtime.status()["pool_size"] == 7
    result = await runtime.run_once(trigger="manual")
    assert result["success"] is True
    assert result["trigger"] == "manual"
    assert result["admitted_count"] == 1
    assert (await runtime.run_maintenance())["pool_size"] == 8
