from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _EmptyConfirmationProvider:
    def is_enabled(self):
        return True

    async def call_stage(self, **kwargs):
        return {"confirmations": []}


class _BadJsonProvider:
    def is_enabled(self):
        return True

    async def call_stage(self, **kwargs):
        from akshare_mcp.services.strategy_llm_provider import StrategyLLMRequestError

        raise StrategyLLMRequestError(
            "call_stage(market_confirmation) failed after repair",
            metrics={
                "stage_id": "market_confirmation",
                "status": "failed",
                "last_error_type": "StrategyLLMResponseParseError",
                "last_error": "response content is not valid JSON",
                "attempts": [
                    {
                        "status": "failed",
                        "error_type": "StrategyLLMResponseParseError",
                        "error": "response content is not valid JSON",
                    }
                ],
            },
        )


class _FakeDb:
    async def get_klines(self, symbol, limit=30):
        return [
            {"close": 10.0},
            {"close": 10.2},
            {"close": 10.4},
            {"close": 10.8},
            {"close": 11.1},
            {"close": 11.4},
        ]


class _EnabledProvider:
    config = SimpleNamespace(provider="test", model="test-model", strict=False)

    def is_enabled(self):
        return True


class _NoFallbackMiner:
    def generate_factor_candidates(self, *args, **kwargs):
        raise AssertionError("local factor miner must not run after staged pipeline suppression")


class _NoFallbackRuleGenerator:
    def generate(self, *args, **kwargs):
        from akshare_mcp.services.strategy_spec import StrategySpec

        return [
            StrategySpec(
                strategy_type="ma_cross",
                params={"short_period": 5, "long_period": 20},
                name="rule should be suppressed",
                metadata={"generator_type": "rule"},
            )
        ]


class _SuppressedLLMGenerator:
    replay_called = False

    async def generate(self, *args, **kwargs):
        return []

    def get_last_report(self):
        return {
            "post_pipeline_fallback_suppressed": True,
            "post_pipeline_suppression_reason": "provider_output_format_failure",
            "external_provider": {
                "status": "failed_output_format",
                "monolithic_fallback_suppressed": True,
                "local_fallback_suppressed": True,
            },
            "local_generator": {
                "status": "skipped_after_pipeline_failure",
                "local_fallback_suppressed": True,
            },
        }

    async def replay_persisted_specs(self, *args, **kwargs):
        self.replay_called = True
        raise AssertionError("hypothesis replay must not run after staged pipeline suppression")


class _NoFallbackOptimizer:
    evolve_called = False

    async def evolve(self, *args, **kwargs):
        self.evolve_called = True
        raise AssertionError("optimizer must not run after staged pipeline suppression")


def test_market_confirmation_empty_output_is_valid_not_fallback():
    # P0-A: LLM 返回 {"confirmations": []} 是合法保守输出(熊市/无确认),
    # 不应被判失败/触发 fallback。修复前此处误判 used_fallback=True 并伪造非空输出。
    from akshare_mcp.services.strategy_pipeline import MultiStageStrategyPipeline
    from akshare_mcp.services.strategy_stages import get_stage_registry

    pipeline = MultiStageStrategyPipeline(provider=_EmptyConfirmationProvider())
    stage_result = asyncio.run(
        pipeline.run_stage(
            db=_FakeDb(),
            stage_id="market_confirmation",
            input_data={
                "research_task": {
                    "target_symbols": ["600000"],
                    "candidate_family": "value_factor",
                }
            },
            snapshot={},
            stage_def=get_stage_registry()["market_confirmation"],
        )
    )

    # 合法空:不触发 fallback,output 保持 LLM 返回的空数组(不伪造内容)
    assert stage_result.used_fallback is False
    assert stage_result.llm_attempted is True
    assert stage_result.output["confirmations"] == []


def test_pipeline_candidate_canonicalizes_validation_profile_before_precompile():
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator

    research_task = {
        "target_symbols": ["300750", "600519"],
        "allowed_strategy_types": ["multi_factor"],
        "validation_focus": "target_plus_representative",
    }
    candidate = {
        "name": "LLM multi factor",
        "strategy_type": "multi_factor",
        "target_symbols": ["300750", "600519"],
        "stock_pool": {"selection_mode": "explicit", "symbols": ["300750", "600519"]},
        "research_task": dict(research_task),
        "params": {"factor_weights": {"momentum": 0.6, "quality": 0.4}},
        "portfolio_spec": {
            "position_assumption": "equal_weight_proxy",
            "target_weight_scheme": "equal_weight",
        },
        "execution_assumptions": {
            "commission_rate": 0.00025,
            "slippage_bps": 5,
            "tradability_filter": True,
            "slippage_model": "fixed",
        },
        "validation_profile": {
            "profile": "trade_rule_validation",
            "validation_focus": "target_plus_representative",
            "primary_validation_layer": "target",
            "objective_profile": "balanced",
        },
    }

    spec = LLMProxyStrategyGenerator._pipeline_candidate_to_spec(
        candidate,
        provenance={"stages": {}},
    )

    assert spec is not None
    validation_profile = spec.metadata["validation_profile"]
    assert validation_profile["profile"] == "factor_rank_validation"
    assert validation_profile["validation_focus"] == "target_plus_representative"
    assert validation_profile["primary_validation_layer"] == "combined"
    assert validation_profile["objective_profile"] == "balanced"


def test_stage_provider_format_failure_suppresses_local_fallback():
    from akshare_mcp.services.strategy_pipeline import MultiStageStrategyPipeline
    from akshare_mcp.services.strategy_stages import get_stage_registry

    pipeline = MultiStageStrategyPipeline(provider=_BadJsonProvider())
    stage_result = asyncio.run(
        pipeline.run_stage(
            db=_FakeDb(),
            stage_id="market_confirmation",
            input_data={
                "research_task": {
                    "target_symbols": ["600000"],
                    "candidate_family": "value_factor",
                }
            },
            snapshot={},
            stage_def=get_stage_registry()["market_confirmation"],
        )
    )

    assert stage_result.used_fallback is False
    assert stage_result.output == {}
    assert stage_result.error == "llm output format failed for market_confirmation"
    assert stage_result.llm_error_metrics["local_fallback_suppressed"] is True
    assert stage_result.llm_error_metrics["suppression_reason"] == "provider_output_format_failure"


def test_generator_suppresses_monolithic_and_local_fallback_after_bad_staged_output(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_stages import StageResult

    class _BadOutputPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "market_confirmation": StageResult(
                        stage_id="market_confirmation",
                        output={},
                        used_fallback=False,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=21,
                        elapsed_sec=0.01,
                        error="llm output format failed for market_confirmation",
                        llm_error="response content is not valid JSON",
                        llm_error_type="StrategyLLMResponseParseError",
                        llm_error_metrics={
                            "status": "failed",
                            "local_fallback_suppressed": True,
                            "suppression_reason": "provider_output_format_failure",
                        },
                    )
                },
                candidates=[],
                elapsed_sec=0.01,
            )

    monkeypatch.delenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", raising=False)
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _BadOutputPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _NoFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        raise AssertionError("monolithic external provider must not run after staged suppression")

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=2,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["dsl_rule"]},
        )
    )

    report = generator.get_last_report()
    requests = report["pipeline_staged_provenance"]["stages"]["market_confirmation"]["llm_error_metrics"]
    assert specs == []
    assert report["post_pipeline_fallback_suppressed"] is True
    assert report["post_pipeline_suppression_reason"] == "provider_output_format_failure"
    assert report["external_provider"]["monolithic_fallback_suppressed"] is True
    assert report["external_provider"]["local_fallback_suppressed"] is True
    assert report["local_generator"]["local_fallback_suppressed"] is True
    assert requests["local_fallback_suppressed"] is True


def test_generator_skips_monolithic_but_keeps_local_fallback_after_empty_pipeline(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_spec import StrategySpec
    from akshare_mcp.services.strategy_stages import StageResult

    class _EmptyPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "strategy_generation": StageResult(
                        stage_id="strategy_generation",
                        output={"candidates": []},
                        used_fallback=True,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=21,
                        elapsed_sec=0.01,
                        error="no executable candidates",
                        llm_error_metrics={
                            "status": "non_executable",
                            "validation_failure_reason": "empty_candidates",
                        },
                    )
                },
                candidates=[],
                elapsed_sec=0.01,
            )

    class _LocalFallbackMiner:
        called = False

        def generate_factor_candidates(self, *args, **kwargs):
            self.called = True
            return [
                {
                    "name": "local momentum fallback",
                    "description": "local candidate",
                    "formula": "close / close.shift(20) - 1",
                    "category": "momentum",
                    "rationale": "pipeline empty fallback",
                    "_engine": "local_rule_v1",
                }
            ]

    # P1-D: 测试 SKIP=1 单独生效(ALLOW=0)→ staged 空后跳过 monolithic、走 local。
    # (旧测试同时设 ALLOW=1+SKIP=1 矛盾配置;修复后 ALLOW 优先,故此处明确只开 SKIP。)
    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_PIPELINE_EMPTY_SKIP_MONOLITHIC_FALLBACK", "1")
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _EmptyPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _LocalFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        raise AssertionError("monolithic external provider must not run after empty staged pipeline")

    def _local_candidate_to_spec(candidate, research_task=None):
        return StrategySpec(
            strategy_type="momentum",
            params={"lookback": 20, "threshold": 0.02},
            name=str(candidate.get("name") or "local fallback"),
            metadata={"generator_type": "local_rule_v1"},
        )

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)
    monkeypatch.setattr(generator, "_local_candidate_to_spec", _local_candidate_to_spec)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=2,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["momentum"]},
        )
    )

    report = generator.get_last_report()
    assert len(specs) == 1
    assert generator.miner.called is True
    assert report["post_pipeline_fallback_suppressed"] is False
    assert report["post_pipeline_suppression_reason"] is None
    assert report["external_provider"]["status"] == "skipped_after_pipeline_empty"
    assert report["external_provider"]["monolithic_external_provider_skipped"] is True
    assert report["external_provider"]["monolithic_external_provider_skip_reason"] == "staged_pipeline_empty"
    assert "monolithic_fallback_suppressed" not in report["external_provider"]
    assert "local_fallback_suppressed" not in report["external_provider"]
    assert report["local_generator"]["status"] == "succeeded"


def test_generator_reports_normalize_rejected_empty_pipeline(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_spec import StrategySpec
    from akshare_mcp.services.strategy_stages import StageResult

    class _NormalizeRejectedPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "strategy_generation": StageResult(
                        stage_id="strategy_generation",
                        output={"candidates": ["candidate"]},
                        used_fallback=False,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=21,
                        elapsed_sec=0.01,
                    )
                },
                candidates=[
                    {
                        "name": "momentum should be rejected in conservative task",
                        "strategy_type": "momentum",
                        "params": {"lookback": 20},
                        "target_symbols": ["600000"],
                    }
                ],
                elapsed_sec=0.01,
            )

    class _LocalFallbackMiner:
        called = False

        def generate_factor_candidates(self, *args, **kwargs):
            self.called = True
            return [
                {
                    "name": "local fallback after normalize reject",
                    "formula": "close / close.shift(20) - 1",
                    "category": "momentum",
                    "_engine": "local_rule_v1",
                }
            ]

    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_PIPELINE_EMPTY_SKIP_MONOLITHIC_FALLBACK", "1")
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _NormalizeRejectedPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _LocalFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        raise AssertionError("monolithic external provider must not run after normalized-empty staged pipeline")

    def _local_candidate_to_spec(candidate, research_task=None):
        return StrategySpec(
            strategy_type="momentum",
            params={"lookback": 20, "threshold": 0.02},
            name=str(candidate.get("name") or "local fallback"),
            metadata={"generator_type": "local_rule_v1"},
        )

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)
    monkeypatch.setattr(generator, "_local_candidate_to_spec", _local_candidate_to_spec)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=2,
            snapshot={},
            parent_strategies=[],
            research_task={
                "target_symbols": ["600000"],
                "candidate_family": "ma_cross",
                "allowed_strategy_types": ["ma_cross"],
            },
        )
    )

    report = generator.get_last_report()
    assert len(specs) == 1
    assert generator.miner.called is True
    assert report["pipeline_staged_fallback_reason"] == "returned_empty:normalize_rejected"
    assert report["pipeline_staged_candidate_funnel"] == {
        "candidate_total": 1,
        "spec_selected": 0,
        "normalize_rejected": 1,
        "precompile_rejected": 0,
        "other_dropped": 0,
    }
    assert report["pipeline_staged_normalize_rejections"][0]["reject_reason"] == (
        "strategy_type_not_in_conservative_allowlist:momentum"
    )
    assert report["external_provider"]["monolithic_external_provider_skip_detail"] == (
        "returned_empty:normalize_rejected"
    )
    assert report["external_provider"]["pipeline_empty_detail"] == (
        "returned_empty:normalize_rejected"
    )
    assert report["external_provider"]["pipeline_candidate_funnel"]["normalize_rejected"] == 1


def test_generator_default_suppresses_fallback_after_empty_pipeline(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_stages import StageResult

    class _EmptyPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "strategy_generation": StageResult(
                        stage_id="strategy_generation",
                        output={"candidates": []},
                        used_fallback=False,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=21,
                        elapsed_sec=0.01,
                    )
                },
                candidates=[],
                elapsed_sec=0.01,
            )

    monolithic_calls: list[int] = []

    monkeypatch.delenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", raising=False)
    monkeypatch.delenv("STRATEGY_FACTORY_PIPELINE_EMPTY_SKIP_MONOLITHIC_FALLBACK", raising=False)
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "LLM_FAN_OUT_COUNT", 1, raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _EmptyPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _NoFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        monolithic_calls.append(int(kwargs.get("request_index") or 1))
        raise AssertionError("monolithic external provider must be opt-in after empty staged pipeline")

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=1,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["momentum"]},
        )
    )

    report = generator.get_last_report()
    assert monolithic_calls == []
    assert specs == []
    assert report["post_pipeline_fallback_suppressed"] is True
    assert report["post_pipeline_suppression_reason"] == "staged_pipeline_empty"
    assert report["external_provider"]["status"] == "non_executable"
    assert report["external_provider"]["monolithic_fallback_suppressed"] is True
    assert report["external_provider"]["local_fallback_suppressed"] is True
    assert report["local_generator"]["status"] == "skipped_after_pipeline_failure"
    assert report["local_generator"]["local_fallback_suppressed"] is True


def test_generator_skips_monolithic_after_provider_failed_empty_pipeline(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_spec import StrategySpec
    from akshare_mcp.services.strategy_stages import StageResult

    class _ProviderFailedPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "event_recognition": StageResult(
                        stage_id="event_recognition",
                        output={},
                        used_fallback=True,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=0,
                        elapsed_sec=60.0,
                        error="ReadTimeout",
                        llm_error="ReadTimeout",
                        llm_error_type="ReadTimeout",
                        llm_error_metrics={
                            "status": "failed",
                            "last_error_type": "ReadTimeout",
                        },
                    )
                },
                candidates=[],
                elapsed_sec=60.0,
            )

    class _LocalFallbackMiner:
        called = False

        def generate_factor_candidates(self, *args, **kwargs):
            self.called = True
            return [
                {
                    "name": "provider failure local fallback",
                    "description": "local candidate after provider failed staged pipeline",
                    "formula": "close / close.shift(20) - 1",
                    "category": "momentum",
                    "rationale": "provider failure staged fallback",
                    "_engine": "local_rule_v1",
                }
            ]

    monkeypatch.delenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", raising=False)
    monkeypatch.delenv("STRATEGY_FACTORY_PIPELINE_EMPTY_SKIP_MONOLITHIC_FALLBACK", raising=False)
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _ProviderFailedPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _LocalFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        raise AssertionError("monolithic external provider must not run after provider-failed staged pipeline")

    def _local_candidate_to_spec(candidate, research_task=None):
        return StrategySpec(
            strategy_type="momentum",
            params={"lookback": 20, "threshold": 0.02},
            name=str(candidate.get("name") or "provider failure fallback"),
            metadata={"generator_type": "local_rule_v1"},
        )

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)
    monkeypatch.setattr(generator, "_local_candidate_to_spec", _local_candidate_to_spec)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=2,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["momentum"]},
        )
    )

    report = generator.get_last_report()
    assert len(specs) == 1
    assert generator.miner.called is True
    assert report["external_provider"]["status"] == "skipped_after_pipeline_provider_failure"
    assert report["external_provider"]["monolithic_external_provider_skipped"] is True
    assert (
        report["external_provider"]["monolithic_external_provider_skip_reason"]
        == "staged_pipeline_empty_after_provider_failure"
    )
    assert report["local_generator"]["status"] == "succeeded"


def test_generator_allow_override_runs_monolithic_after_empty_pipeline(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_spec import StrategySpec
    from akshare_mcp.services.strategy_stages import StageResult

    class _EmptyPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "strategy_generation": StageResult(
                        stage_id="strategy_generation",
                        output={"candidates": []},
                        used_fallback=True,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=21,
                        elapsed_sec=0.01,
                        error="no executable candidates",
                        llm_error_metrics={
                            "status": "non_executable",
                            "validation_failure_reason": "empty_candidates",
                        },
                    )
                },
                candidates=[],
                elapsed_sec=0.01,
            )

    class _LocalFallbackMiner:
        called = False

        def generate_factor_candidates(self, *args, **kwargs):
            self.called = True
            return [
                {
                    "name": "local safety fallback",
                    "description": "local candidate after empty staged pipeline",
                    "formula": "close / close.shift(12) - 1",
                    "category": "momentum",
                    "rationale": "empty staged pipeline safety fallback",
                    "_engine": "local_rule_v1",
                }
            ]

    monolithic_calls: list[int] = []

    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_PIPELINE_EMPTY_SKIP_MONOLITHIC_FALLBACK", "1")
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "LLM_FAN_OUT_COUNT", 1, raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _EmptyPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _LocalFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        monolithic_calls.append(int(kwargs.get("request_index") or 1))
        return {
            "status": "succeeded",
            "viable_specs": [
                StrategySpec(
                    strategy_type="momentum",
                    params={"lookback": 18, "threshold": 0.02},
                    name="monolithic retry",
                    metadata={"generator_type": "external_llm"},
                )
            ],
            "all_specs": [],
            "successful_without_specs": False,
            "request_report": {
                "request_index": int(kwargs.get("request_index") or 1),
                "status": "succeeded",
            },
            "analysis": {},
        }

    def _local_candidate_to_spec(candidate, research_task=None):
        return StrategySpec(
            strategy_type="momentum",
            params={"lookback": 12, "threshold": 0.03},
            name=str(candidate.get("name") or "local safety fallback"),
            metadata={"generator_type": "local_rule_v1"},
        )

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)
    monkeypatch.setattr(generator, "_local_candidate_to_spec", _local_candidate_to_spec)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=1,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["momentum"]},
        )
    )

    report = generator.get_last_report()
    assert monolithic_calls == [1]
    assert len(specs) == 1
    assert specs[0].metadata["generator_type"] == "external_llm"
    assert generator.miner.called is False
    assert report["post_pipeline_fallback_suppressed"] is False
    assert report["external_provider"]["status"] == "succeeded"
    assert report["external_provider"]["selected_count"] == 1
    assert "monolithic_external_provider_skipped" not in report["external_provider"]
    assert report["local_generator"]["status"] == "skipped_external_selected"
    assert "monolithic_fallback_suppressed" not in report["external_provider"]


def test_generator_allow_override_times_out_monolithic_and_uses_local_after_empty_pipeline(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_pipeline import PipelineResult
    from akshare_mcp.services.strategy_spec import StrategySpec
    from akshare_mcp.services.strategy_stages import StageResult

    class _EmptyPipeline:
        async def run_pipeline(self, **kwargs):
            return PipelineResult(
                stages={
                    "strategy_generation": StageResult(
                        stage_id="strategy_generation",
                        output={"candidates": []},
                        used_fallback=False,
                        llm_attempted=True,
                        prompt_chars=12,
                        response_chars=21,
                        elapsed_sec=0.01,
                    )
                },
                candidates=[],
                elapsed_sec=0.01,
            )

    class _LocalFallbackMiner:
        called = False

        def generate_factor_candidates(self, *args, **kwargs):
            self.called = True
            return [
                {
                    "name": "local fallback after monolithic timeout",
                    "description": "local candidate after monolithic timeout",
                    "formula": "close / close.shift(20) - 1",
                    "category": "momentum",
                    "rationale": "bounded monolithic timeout fallback",
                    "_engine": "local_rule_v1",
                }
            ]

    monolithic_calls: list[int] = []

    monkeypatch.setenv("STRATEGY_FACTORY_ALLOW_PIPELINE_EMPTY_MONOLITHIC_FALLBACK", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_PIPELINE_EMPTY_SKIP_MONOLITHIC_FALLBACK", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_PIPELINE_EMPTY_MONOLITHIC_TIMEOUT_SEC", "0.01")
    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "LLM_FAN_OUT_COUNT", 1, raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _EmptyPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _LocalFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        monolithic_calls.append(int(kwargs.get("request_index") or 1))
        await asyncio.sleep(1.0)
        raise AssertionError("timeout should cancel this request")

    def _local_candidate_to_spec(candidate, research_task=None):
        return StrategySpec(
            strategy_type="momentum",
            params={"lookback": 20, "threshold": 0.02},
            name=str(candidate.get("name") or "local fallback"),
            metadata={"generator_type": "local_rule_v1"},
        )

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)
    monkeypatch.setattr(generator, "_local_candidate_to_spec", _local_candidate_to_spec)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=1,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["momentum"]},
        )
    )

    report = generator.get_last_report()
    assert monolithic_calls == [1]
    assert len(specs) == 1
    assert specs[0].metadata["generator_type"] == "local_rule_v1"
    assert generator.miner.called is True
    assert report["external_provider"]["status"] == "skipped_after_pipeline_empty_monolithic_timeout"
    assert report["external_provider"]["monolithic_external_provider_timed_out"] is True
    assert (
        report["external_provider"]["monolithic_external_provider_skip_reason"]
        == "staged_pipeline_empty_monolithic_timeout"
    )
    assert report["local_generator"]["status"] == "succeeded"


def test_generator_keeps_local_fallback_after_pipeline_timeout(monkeypatch):
    import pandas as pd

    import akshare_mcp.services._strategy_generators_generate as generate_module
    import akshare_mcp.services.strategy_generators as public_generators
    from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
    from akshare_mcp.services.strategy_spec import StrategySpec

    class _TimeoutPipeline:
        async def run_pipeline(self, **kwargs):
            raise asyncio.TimeoutError()

    class _LocalFallbackMiner:
        called = False

        def generate_factor_candidates(self, *args, **kwargs):
            self.called = True
            return [
                {
                    "name": "timeout local fallback",
                    "description": "local candidate after pipeline timeout",
                    "formula": "close / close.shift(10) - 1",
                    "category": "momentum",
                    "rationale": "pipeline timeout fallback",
                    "_engine": "local_rule_v1",
                }
            ]

    monkeypatch.setattr(public_generators, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(generate_module, "PIPELINE_MODE", "staged", raising=False)
    monkeypatch.setattr(public_generators, "get_strategy_pipeline", lambda: _TimeoutPipeline(), raising=False)

    generator = LLMProxyStrategyGenerator()
    generator.external_provider = _EnabledProvider()
    generator.miner = _LocalFallbackMiner()

    async def _recent_experiments(*args, **kwargs):
        return []

    async def _build_research_context(*args, **kwargs):
        return {"task_target_context": {}, "blocked_by_target_universe": False}

    async def _build_market_frame(*args, **kwargs):
        return pd.DataFrame({"close": [10.0, 10.2, 10.4], "volume": [1000, 1100, 1200]})

    async def _build_symbol_frame_cache(*args, **kwargs):
        return {}

    async def _run_external_provider_request(*args, **kwargs):
        raise AssertionError("monolithic external provider must not run after staged pipeline timeout")

    def _local_candidate_to_spec(candidate, research_task=None):
        return StrategySpec(
            strategy_type="momentum",
            params={"lookback": 10, "threshold": 0.01},
            name=str(candidate.get("name") or "timeout fallback"),
            metadata={"generator_type": "local_rule_v1"},
        )

    monkeypatch.setattr(generator, "_recent_experiments", _recent_experiments)
    monkeypatch.setattr(generator, "_build_research_context", _build_research_context)
    monkeypatch.setattr(generator, "_build_market_frame", _build_market_frame)
    monkeypatch.setattr(generator, "_build_symbol_frame_cache", _build_symbol_frame_cache)
    monkeypatch.setattr(generator, "_run_external_provider_request", _run_external_provider_request)
    monkeypatch.setattr(generator, "_local_candidate_to_spec", _local_candidate_to_spec)

    specs = asyncio.run(
        generator.generate(
            _FakeDb(),
            limit=2,
            snapshot={},
            parent_strategies=[],
            research_task={"target_symbols": ["600000"], "allowed_strategy_types": ["momentum"]},
        )
    )

    report = generator.get_last_report()
    assert len(specs) == 1
    assert generator.miner.called is True
    assert report["pipeline_staged_fallback_reason"] == "pipeline_timeout"
    assert report["post_pipeline_fallback_suppressed"] is False
    assert report["post_pipeline_suppression_reason"] is None
    assert report["external_provider"]["status"] == "skipped_after_pipeline_timeout"
    assert report["external_provider"]["monolithic_external_provider_skipped"] is True
    assert report["external_provider"]["monolithic_external_provider_skip_reason"] == "pipeline_timeout"
    assert "local_fallback_suppressed" not in report["external_provider"]
    assert report["local_generator"]["status"] == "succeeded"
    assert report["selected_generators"]["local_rule_v1"] == 1


def test_candidate_generation_suppresses_rule_replay_and_optimizer_fallbacks():
    from akshare_mcp.services.strategy_autonomy_components import CandidateGenerationService

    llm_generator = _SuppressedLLMGenerator()
    optimizer = _NoFallbackOptimizer()
    service = CandidateGenerationService(
        rule_generator=_NoFallbackRuleGenerator(),
        llm_generator=llm_generator,
        optimizer=optimizer,
    )

    result = asyncio.run(
        service.generate(
            _FakeDb(),
            snapshot={
                "fear_greed_index": 50,
                "_shared_generation_context": {
                    "parent_strategies": [{"id": "parent_1"}],
                    "history_summary": [{"experiment_id": "old"}],
                },
            },
            limit=3,
            research_task={"target_symbols": ["600000"], "candidate_family": "ma_cross"},
        )
    )

    assert result["rule_specs"] == []
    assert result["replay_specs"] == []
    assert result["evolved_specs"] == []
    assert result["merged_specs"] == []
    assert result["llm_report"]["candidate_fallback_suppressed"] is True
    assert result["llm_report"]["replay_provider"]["status"] == "skipped_after_llm_failure"
    assert result["llm_report"]["optimizer"]["status"] == "skipped_after_llm_failure"
    assert llm_generator.replay_called is False
    assert optimizer.evolve_called is False


def test_skip_llm_only_counts_real_llm_errors():
    # P0-B: skip_llm 熔断只数真实 LLM 错误(timeout/parse/overload),
    # 合法空、prefer_fallback、provider 未启用的非错误 fallback 不计入。
    from akshare_mcp.services.strategy_pipeline import _is_real_llm_error_fallback
    from akshare_mcp.services.strategy_stages import StageResult

    # 真实 LLM 错误 fallback → 计入
    real_err = StageResult(
        stage_id="market_confirmation", output={}, used_fallback=True,
        llm_attempted=True, llm_error_type="StrategyLLMResponseParseError",
    )
    assert _is_real_llm_error_fallback(real_err) is True

    # P0-A 合法空 → used_fallback=False → 不计入
    valid_empty = StageResult(stage_id="theme_propagation", output={"themes": []}, used_fallback=False)
    assert _is_real_llm_error_fallback(valid_empty) is False

    # prefer_fallback / provider 未启用 → llm_attempted=False → 不计入
    prefer_fb = StageResult(stage_id="x", output={}, used_fallback=True, llm_attempted=False)
    assert _is_real_llm_error_fallback(prefer_fb) is False

    # fallback 但无 error_type(异常兜底构造) → 不计入
    no_errtype = StageResult(stage_id="x", output={}, used_fallback=True, llm_attempted=True, llm_error_type=None)
    assert _is_real_llm_error_fallback(no_errtype) is False
