from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


def _ok(data, **kwargs):
    payload = {"success": True, "data": data, "error": None}
    payload.update(kwargs)
    return payload


def test_start_server_prefers_workspace_src_before_imports() -> None:
    start_server = Path(__file__).resolve().parents[1] / "start_server.py"
    lines = start_server.read_text(encoding="utf-8", errors="replace").splitlines()
    path_insert = next(
        idx for idx, line in enumerate(lines)
        if 'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))' in line
    )
    first_akshare_import = next(
        idx for idx, line in enumerate(lines)
        if "from akshare_mcp.env_loader import load_mcp_env" in line
    )
    assert path_insert < first_akshare_import


def test_start_server_supports_env_file_logging() -> None:
    start_server = Path(__file__).resolve().parents[1] / "start_server.py"
    text = start_server.read_text(encoding="utf-8", errors="replace")
    assert "AKSHARE_MCP_LOG_FILE" in text
    assert "MCP_LOG_FILE" in text
    assert "RotatingFileHandler" in text
    assert "stream=_sys.stderr" in text


def test_sqlite_prepare_keeps_date_function_and_converts_date_type() -> None:
    from akshare_mcp.storage.sqlite.schema_base import _prepare_statement

    query_sql, query_args = _prepare_statement(
        "SELECT date('now') AS today, date('now', '+' || $1 || ' days') AS future",
        (7,),
    )
    assert "TEXT(" not in query_sql
    assert "date('now')" in query_sql
    assert query_args == (7,)

    ddl_sql, _ = _prepare_statement("CREATE TABLE sample_events (event_date DATE)", ())
    assert "event_date TEXT" in ddl_sql


def test_scheduler_run_now_returns_background_job(monkeypatch) -> None:
    from akshare_mcp.services import factor_scheduler
    from akshare_mcp.tools.managers import quant_mgr_scheduler

    seen: dict[str, object] = {}

    class FakeScheduler:
        def __init__(self):
            self.universe = ["600519", "000001", "000858", "002304", "510050"]
            self.factors = ["momentum", "value", "quality"]
            self.batch_size = 50

        def status(self):
            return {"running": False, "last_result": None}

        async def run_once(self):
            seen["universe"] = list(self.universe)
            seen["factors"] = list(self.factors)
            seen["batch_size"] = self.batch_size
            seen["skip_dynamic_universe"] = getattr(self, "_skip_dynamic_universe", False)
            await asyncio.sleep(0.05)
            return {
                "run_id": "test-run",
                "status": "success",
                "computed": 3,
                "errors": 0,
                "elapsed_seconds": 0.01,
                "quality_status": "fresh",
            }

    monkeypatch.setattr(factor_scheduler, "get_factor_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(factor_scheduler, "FactorScheduler", FakeScheduler)
    quant_mgr_scheduler._RUN_NOW_TASK = None
    quant_mgr_scheduler._RUN_NOW_THREAD = None
    quant_mgr_scheduler._RUN_NOW_JOB = None

    async def scenario():
        started = asyncio.get_running_loop().time()
        response = await quant_mgr_scheduler.handle_scheduler_run_now(
            kw={
                "codes": ["600519", "000001"],
                "factors": ["momentum"],
                "batch_size": 1,
            },
            ok=_ok,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert response["success"] is True
        assert elapsed < 0.5
        data = response["data"]
        assert data["status"] in {"scheduled", "running", "completed"}
        assert data["job_id"].startswith("quant_scheduler_run_now_")

        status_while_running = await quant_mgr_scheduler.handle_scheduler_status(ok=_ok)
        assert status_while_running["success"] is True

        deadline = asyncio.get_running_loop().time() + 1
        while quant_mgr_scheduler._RUN_NOW_THREAD and quant_mgr_scheduler._RUN_NOW_THREAD.is_alive():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("scheduler background thread did not finish")
            await asyncio.sleep(0.01)
        status_response = await quant_mgr_scheduler.handle_scheduler_status(ok=_ok)
        job = status_response["data"]["run_now_job"]
        assert job["status"] == "completed"
        assert job["result_summary"]["run_id"] == "test-run"
        assert seen["universe"] == ["600519", "000001"]
        assert seen["factors"] == ["momentum"]
        assert seen["batch_size"] == 1
        assert seen["skip_dynamic_universe"] is True

    asyncio.run(scenario())


def test_quant_manager_parses_params_json_string_for_scheduler(monkeypatch) -> None:
    from akshare_mcp.services import factor_scheduler
    from akshare_mcp.tools.managers import quant_mgr_scheduler, quant_manager as quant_manager_module

    seen: dict[str, object] = {}

    class FakeScheduler:
        def __init__(self):
            self.universe = ["600519", "000001", "000858"]
            self.factors = ["momentum", "value"]
            self.batch_size = 50

        def status(self):
            return {"running": False, "last_result": None}

        async def run_once(self):
            seen["universe"] = list(self.universe)
            seen["factors"] = list(self.factors)
            return {"run_id": "params-json-run", "status": "success", "computed": 1, "errors": 0}

    monkeypatch.setattr(factor_scheduler, "get_factor_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(factor_scheduler, "FactorScheduler", FakeScheduler)
    quant_mgr_scheduler._RUN_NOW_TASK = None
    quant_mgr_scheduler._RUN_NOW_THREAD = None
    quant_mgr_scheduler._RUN_NOW_JOB = None

    async def scenario():
        response = await quant_manager_module.quant_manager(
            action="scheduler_run_now",
            params=json.dumps(
                {
                    "codes": ["600519"],
                    "factors": ["momentum"],
                    "wait": True,
                    "timeout_sec": 1,
                }
            ),
        )
        assert response["success"] is True
        assert response["data"]["job_id"].startswith("quant_scheduler_run_now_")
        assert seen["universe"] == ["600519"]
        assert seen["factors"] == ["momentum"]

    asyncio.run(scenario())


def test_llm_factor_mining_respects_max_candidates_and_marks_unpersisted() -> None:
    from akshare_mcp.tools.managers.quant_mgr_generation import handle_llm_factor_mining

    class FakeProvider:
        config = SimpleNamespace(provider="fake_provider", model="fake_model")

        def __init__(self):
            self.seen_candidate_count = None

        def is_enabled(self):
            return True

        async def generate_candidates(self, prompt, *, candidate_count):
            self.seen_candidate_count = candidate_count
            return {
                "provider": "fake_provider",
                "model": "fake_model",
                "candidates": [
                    {
                        "name": f"candidate_{idx}",
                        "hypothesis": "test hypothesis",
                        "family": "momentum",
                        "inputs": ["close"],
                        "expression_dsl": "return_5d",
                    }
                    for idx in range(candidate_count)
                ],
                "warnings": [],
                "analysis": {},
            }

    class FakeMemoryService:
        async def build_prompt_memory_context(self, **kwargs):
            return {"available": False}

        async def annotate_generated_candidates(self, candidates, **kwargs):
            return {"candidates": candidates, "warnings": []}

    class FakePrompt:
        source_chain = ["fake.prompt"]
        context_summary = {"rows": []}
        request_payload = {"field_hints": []}
        schema_path = "fake-schema.json"
        system_prompt = "system"
        user_prompt = "user"

    provider = FakeProvider()

    async def build_prompt(**kwargs):
        assert kwargs["candidate_count"] == 2
        return FakePrompt()

    async def scenario():
        response = await handle_llm_factor_mining(
            kw={
                "codes": ["600519"],
                "max_candidates": 2,
                "startup_warmup": False,
                "persist_artifact": False,
                "_dry_run": True,
            },
            code=None,
            db=object(),
            ok=_ok,
            fail=lambda message, **kwargs: {"success": False, "error": message, **kwargs},
            get_factor_llm_provider_fn=lambda: provider,
            memory_service_factory=FakeMemoryService,
            build_factor_mining_prompt_fn=build_prompt,
            run_runtime_data_warmup_fn=lambda **kwargs: {},
            register_artifact_async_fn=lambda payload: (_ for _ in ()).throw(AssertionError("should not persist")),
            missing_kline_fn=lambda *args, **kwargs: [],
            compile_factor_candidate_fn=lambda candidate: {"valid": True, "referenced_fields": ["close"]},
            sort_klines_ascending_fn=lambda rows: rows,
            coerce_bool_fn=lambda value, default=False: bool(value) if value is not None else default,
            env_bool_fn=lambda name, default: default,
        )

        assert response["success"] is True
        data = response["data"]
        assert provider.seen_candidate_count == 2
        assert data["requested_candidate_count"] == 2
        assert data["candidate_count"] == 2
        assert data["artifact_persisted"] is False
        assert data["artifact_reusable"] is False
        assert data["dry_run"] is True
        assert any("dry_run_artifact_not_persisted" in item for item in data["warnings"])

    asyncio.run(scenario())


def test_validate_factor_candidate_artifact_not_found_mentions_dry_run_hint() -> None:
    from akshare_mcp.tools.managers.quant_mgr_validation import _resolve_candidate_for_validation

    async def missing_artifact(_artifact_id):
        return None

    async def scenario():
        try:
            await _resolve_candidate_for_validation(
                {"artifact_id": "factor_llm_123"},
                get_artifact_async_fn=missing_artifact,
            )
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected artifact lookup to fail")

        assert "artifact not found: factor_llm_123" in message
        assert "dry_run=true" in message
        assert "persist_artifact=false" in message

    asyncio.run(scenario())


def test_factor_robustness_accepts_day_suffix_and_rejects_factor_names(monkeypatch) -> None:
    from akshare_mcp.tools import quant

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    captured = {}

    async def fake_robustness(**kwargs):
        captured.update(kwargs)
        return {"success": True, "data": kwargs, "error": None}

    monkeypatch.setattr(quant, "run_factor_robustness_check", fake_robustness)

    mcp = FakeMCP()
    quant.register(mcp)
    tool = mcp.tools["factor_robustness_check"]

    result = asyncio.run(
        tool(
            codes=["600519", "000001", "000858", "002304"],
            factor="momentum",
            windows=["60d", "90d"],
            param_variations=["10d", "20"],
        )
    )
    assert result["success"] is True
    assert captured["windows"] == [60, 90]
    assert captured["param_variations"] == [10, 20]

    rejected = asyncio.run(
        tool(
            codes=["600519", "000001", "000858", "002304"],
            factor="momentum",
            windows=["60d"],
            param_variations=["rsi_6"],
        )
    )
    assert rejected["success"] is False
    assert "param_variations must be lookback integers" in rejected["error"]


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def _register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _register


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_stock_list_degrades_to_db_when_live_list_unavailable(monkeypatch):
    from akshare_mcp.tools.market import stock_list

    class Conn:
        async def fetch(self, query, *args):
            if "pragma_table_info('stocks')" in query:
                return [{"column_name": "stock_code"}, {"column_name": "stock_name"}, {"column_name": "industry"}]
            if "FROM stocks" in query:
                return [{"code": "600519", "name": "贵州茅台", "industry": "白酒"}]
            return []

        async def fetchval(self, *_args):
            return 1

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(stock_list, "get_stock_list_cached", lambda: (_ for _ in ()).throw(RuntimeError("provider down")))
    monkeypatch.setattr(
        stock_list,
        "_stock_list_from_sqlite",
        lambda _limit, _offset: ([{"code": "600519", "name": "璐靛窞鑼呭彴", "industry": "鐧介厭"}], 1),
    )
    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: Db())

    result = stock_list.get_stock_list.__wrapped__(limit=10)
    assert result["success"] is True
    assert result["degraded"] is True
    assert result["data"]["stocks"][0]["code"] == "600519"


def test_stock_info_uses_db_degraded_when_akshare_profile_missing(monkeypatch):
    from akshare_mcp.tools import finance

    class Db:
        async def get_stock_info(self, _code):
            return {"code": "600519", "name": "贵州茅台", "industry": "白酒", "list_date": "20010827"}

    monkeypatch.setattr(finance.data_source, "get_tushare_pro", lambda: None)
    monkeypatch.setattr(finance, "ak", None)
    monkeypatch.setattr(finance, "_call_with_retry", lambda _fn: None)
    monkeypatch.setattr(
        finance,
        "_stock_info_from_sqlite",
        lambda _code: {"code": "600519", "name": "贵州茅台", "industry": "白酒", "listDate": "20010827", "raw": {}},
    )
    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: Db())

    result = finance.get_stock_info.__wrapped__(code="600519")
    assert result["success"] is True
    assert result["data"]["name"] == "贵州茅台"
    assert result["degraded"] is True
    assert result["meta"]["provider_contract"]["provider_used"] == "db.stocks"


def test_cb_info_returns_successful_degraded_empty_when_provider_fails(monkeypatch):
    from akshare_mcp.tools import basic_data

    mcp = FakeMcp()
    basic_data.register(mcp)
    monkeypatch.setattr(
        basic_data.data_source,
        "get_cb_info",
        lambda stock_code: {
            "success": False,
            "data": {},
            "message": "all providers unavailable",
            "source": "none",
            "backend_requested": "tqcenter",
            "backend_used": "none",
            "fallback_used": True,
            "fallback_reason": "all_backends_failed",
        },
    )

    result = asyncio.run(mcp.tools["get_cb_info"](code="123039"))
    assert result["success"] is True
    assert result["data"]["cb_info"] == {}
    assert result["meta"]["degraded"] is True


def test_stock_capital_returns_degraded_empty_when_provider_unavailable(monkeypatch):
    from akshare_mcp.tools import basic_data, finance

    mcp = FakeMcp()
    basic_data.register(mcp)
    monkeypatch.setattr(
        basic_data.data_source,
        "get_gb_info",
        lambda **_kwargs: {
            "success": False,
            "data": [],
            "message": "tqcenter unavailable and legacy fallback disabled",
            "source": "none",
            "backend_requested": "tqcenter",
            "backend_used": "none",
            "fallback_used": True,
            "fallback_reason": "tdx_only_mode",
            "quality_flags": ["provider_unavailable"],
        },
    )
    monkeypatch.setattr(
        finance,
        "get_stock_info",
        lambda _code: {"success": True, "data": {"code": "600519", "name": "贵州茅台"}},
    )

    result = asyncio.run(mcp.tools["get_stock_capital"](code="600519"))
    assert result["success"] is True
    assert result["data"]["capital_data"] == []
    assert result["data"]["count"] == 0
    assert result["degraded"] is True
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "tdx_only_mode"
    assert "provider_unavailable" in result["quality_flags"]
    assert "empty" in result["quality_flags"]
    assert result["source_chain"] == ["basic_data.get_stock_capital", "market_data.tqcenter"]


def test_generate_daily_report_marks_zero_index_summary_degraded(monkeypatch):
    from akshare_mcp.tools.semantic import daily_report

    class Db:
        pass

    async def stats(_db, _date):
        return {"up_count": 1, "down_count": 1, "limit_up_count": 0, "limit_down_count": 0, "total_count": 2}

    async def sectors(_db):
        return []

    async def flow(_db, _date):
        return {}

    monkeypatch.setattr(daily_report, "get_db", lambda: Db())
    monkeypatch.setattr(
        daily_report,
        "_fetch_index_quotes",
        lambda: {
            "000001": {"name": "SSE Composite", "close": 0, "change_pct": 0, "volume": 1, "amount": 1},
            "399001": {"name": "SZSE Component", "close": None, "change_pct": 0, "volume": 1, "amount": 1},
        },
    )
    monkeypatch.setattr(daily_report, "_fetch_stats", stats)
    monkeypatch.setattr(daily_report, "_fetch_hot_sectors", sectors)
    monkeypatch.setattr(daily_report, "_fetch_capital_flow", flow)

    result = asyncio.run(daily_report.generate_daily_report("2026-05-20"))
    assert result["success"] is True
    assert result["degraded"] is True
    assert result["fallback_used"] is True
    assert "market_summary_zero_or_null_index_values" in result["quality_flags"]
    assert result["data"]["degraded"] is True


def test_dragon_tiger_returns_degraded_empty_when_akshare_missing(monkeypatch):
    from akshare_mcp.tools import fund_flow_market

    class Db:
        def acquire(self):
            class Conn:
                async def fetch(self, *_args):
                    return []

            return FakeAcquire(Conn())

    monkeypatch.setattr(fund_flow_market, "ak", None)
    monkeypatch.setattr(fund_flow_market, "get_db", lambda: Db())

    result = fund_flow_market.get_dragon_tiger(date="2026-05-20")
    assert result["success"] is True
    assert result["data"] == []
    assert result["degraded"] is True
    assert result["meta"]["provider_contract"]["provider_used"] == "none"


def test_performance_manager_accepts_string_created_at(monkeypatch):
    from akshare_mcp.tools.managers import performance_manager

    class Conn:
        async def fetchrow(self, *_args):
            return {
                "id": 1,
                "initial_capital": 100000,
                "current_value": 101000,
                "created_at": "2026-05-20T10:00:00+08:00",
            }

        async def fetch(self, query, *_args):
            if "FROM holdings" in query:
                return []
            return []

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(performance_manager, "get_db", lambda: Db())
    async def empty_daily_returns(*_args, **_kwargs):
        return []

    monkeypatch.setattr(performance_manager, "_build_portfolio_daily_returns", empty_daily_returns)

    mcp = FakeMcp()
    performance_manager.register_performance_manager(mcp)
    result = asyncio.run(mcp.tools["performance_manager"](action="calculate_metrics", params={"portfolio_id": 1}))
    assert result["success"] is True, result
    assert result["data"]["portfolio_id"] == 1


def test_performance_backtest_metrics_non_backtest_artifact_returns_typed_error(monkeypatch):
    from akshare_mcp.tools.managers import performance_manager

    class Conn:
        async def fetchrow(self, *_args):
            return None

    class Db:
        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(performance_manager, "get_db", lambda: Db())

    mcp = FakeMcp()
    performance_manager.register_performance_manager(mcp)
    result = asyncio.run(
        mcp.tools["performance_manager"](
            action="backtest_metrics",
            params={"artifact_id": "codex_full_mcp_20260522_s01_quality_artifact"},
        )
    )
    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARTIFACT_TYPE"
    assert result["data"]["expected_artifact_type"] == "backtest_result"
    assert result["data"]["artifact_id"] == "codex_full_mcp_20260522_s01_quality_artifact"
    assert result["source_chain"] == ["performance_manager", "db.backtest_results"]
    assert "artifact_lookup_miss" in result["quality_flags"]
    assert result["meta"]["quality"]["status"] == "not_found"
    assert result["meta"]["side_effect"]["level"] == "read_only"


def test_screener_manager_supports_stock_code_schema(monkeypatch):
    from akshare_mcp.tools.managers import screener_manager

    class Conn:
        async def fetch(self, query, *args):
            if "pragma_table_info('stocks')" in query:
                return [
                    {"column_name": "stock_code"},
                    {"column_name": "stock_name"},
                    {"column_name": "market_cap"},
                    {"column_name": "pe_ratio"},
                    {"column_name": "pb_ratio"},
                    {"column_name": "industry"},
                ]
            if "FROM stocks s" in query:
                assert "s.stock_code AS code" in query
                return [
                    {
                        "code": "600519",
                        "stock_name": "贵州茅台",
                        "market_cap": 2.0e12,
                        "pe_ratio": 25,
                        "pb_ratio": 8,
                        "roe": 20,
                        "revenue_growth": 10,
                        "debt_ratio": 20,
                        "industry": "白酒",
                    }
                ]
            return []

    class Db:
        async def _financials_code_column(self, _conn):
            return "stock_code"

        def acquire(self):
            return FakeAcquire(Conn())

    monkeypatch.setattr(screener_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    screener_manager.register_screener_manager(mcp)

    result = asyncio.run(mcp.tools["screener_manager"](action="screen", params={"limit": 5}))
    assert result["success"] is True
    assert result["data"]["stocks"][0]["code"] == "600519"


def test_run_skill_executes_runtime_contract_without_markdown_registry(monkeypatch):
    from akshare_mcp.tools import skills
    from akshare_mcp.tools import skills_registry

    monkeypatch.setattr(skills_registry, "_list_skill_roots", lambda: [])
    monkeypatch.setattr(skills, "_list_skill_roots", lambda: [])
    mcp = FakeMcp()
    skills.register(mcp)

    result = asyncio.run(mcp.tools["run_skill"]("akshare-ips-discipline", {"task": "smoke_test"}))
    assert result["success"] is True
    assert result["data"]["skill"]["executable"] is True


def test_user_manager_get_profile_synthetic_and_update_preferences_upserts(monkeypatch):
    from akshare_mcp.tools.managers import user_manager

    class Conn:
        def __init__(self):
            self.user = None

        async def fetchrow(self, query, *args):
            if "SELECT id, username" in query:
                return self.user
            if "SELECT settings" in query:
                return {"settings": json.dumps({"risk": "balanced"})}
            return None

        async def execute(self, query, *args):
            if "INSERT INTO users" in query:
                self.user = {"id": args[0], "username": args[0], "settings": "{}", "created_at": None}
            if "UPDATE users" in query:
                self.user["settings"] = args[0]
            return "OK"

    conn = Conn()

    class Db:
        def acquire(self):
            return FakeAcquire(conn)

    monkeypatch.setattr(user_manager, "get_db", lambda: Db())
    mcp = FakeMcp()
    user_manager.register_user_manager(mcp)

    profile = asyncio.run(mcp.tools["user_manager"](action="get_profile", params={"user_id": "u1", "actor_user_id": "u1"}))
    assert profile["success"] is True
    assert profile["data"]["profile_exists"] is False

    updated = asyncio.run(
        mcp.tools["user_manager"](
            action="update_preferences",
            params={"user_id": "u1", "actor_user_id": "u1", "preferences": {"style": "value"}},
        )
    )
    assert updated["success"] is True
    assert updated["data"]["preferences"]["style"] == "value"


def test_quant_batch_compute_store_false_does_not_persist():
    from akshare_mcp.tools.managers.quant_mgr_classic import handle_batch_compute_factors

    class Db:
        async def get_financials(self, *_args, **_kwargs):
            return []

        async def get_klines(self, *_args, **_kwargs):
            return [{"date": f"2026-01-{(idx % 28) + 1:02d}", "close": 10 + idx * 0.1} for idx in range(80)]

        async def save_factor_values_batch(self, *_args):
            raise AssertionError("store=false must not persist")

        async def save_factor_values(self, *_args):
            raise AssertionError("store=false must not persist")

        async def save_factor_ic(self, *_args):
            raise AssertionError("store=false must not persist")

    async def scenario():
        result = await handle_batch_compute_factors(
            kw={"codes": ["600519"], "factors": ["momentum"], "store": False, "compute_ic": False},
            ok=_ok,
            fail=lambda message, **kwargs: {"success": False, "error": message, **kwargs},
            get_db_fn=lambda: Db(),
        )
        assert result["success"] is True
        assert result["data"]["persisted"] is False
        assert result["data"]["store"] is False

    asyncio.run(scenario())


def test_parse_selection_query_extracts_liquor_and_rsi_threshold():
    from akshare_mcp.tools.semantic.query_parser import parse_selection_query

    result = parse_selection_query("筛选白酒行业且RSI低于30的股票")
    assert result["success"] is True
    assert result["data"]["industry"] == "白酒"
    assert {"id": "rsi_below", "params": {"threshold": 30.0}} in result["data"]["technical_conditions"]


def test_research_empty_results_are_degraded_success(monkeypatch):
    from akshare_mcp.tools.news import research

    monkeypatch.setattr(research.data_source, "get_tushare_pro", lambda: None)
    monkeypatch.setattr(research, "_fetch_eastmoney_research", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(research, "ak", None)

    result = research.get_stock_research.__wrapped__(code="600519", limit=5)
    assert result["success"] is True
    assert result["data"]["reports"] == []
    assert result["degraded"] is True
