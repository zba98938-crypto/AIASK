from __future__ import annotations

import pytest


class _Candidate:
    def __init__(self, name: str, *, quick_evidence: dict | None = None, fitness: float = 0.0) -> None:
        self.name = name
        self.generation_trace = {}
        self.validation_result = {}
        self.quick_evidence = dict(quick_evidence or {})
        self.fitness = fitness

    def to_validation_dict(self) -> dict:
        return {"name": self.name, "expression_dsl": "close / delay(close, 1) - 1"}


class _Context:
    validation_codes = ["000001", "000002", "000003"]


@pytest.mark.asyncio
async def test_factor_mining_strict_validation_candidate_limit(monkeypatch) -> None:
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
    import akshare_mcp.services.factor_validation_pipeline as validation_pipeline

    seen: list[str] = []

    async def fake_validate(_db, candidate, **_kwargs):
        seen.append(candidate["name"])
        return {
            "success": True,
            "stage": "strict",
            "rating": {"grade": "A"},
            "metrics": {},
            "ic": {"horizon_10": {"ic_mean": 0.12, "ic_ir": 0.9, "sample_count": 40}},
        }

    monkeypatch.setenv("FACTOR_MINING_STRICT_VALIDATION_CANDIDATE_LIMIT", "1")
    monkeypatch.setattr(validation_pipeline, "validate_factor_candidate_pipeline", fake_validate)
    monkeypatch.setattr(
        "akshare_mcp.services.factor_mining_factory.factory.evaluate_validation_evidence",
        lambda _result: {"passed": True},
    )

    factory = FactorMiningFactory()
    validated = await factory._validate_batch(
        object(),
        [_Candidate("factor_a"), _Candidate("factor_b")],
        _Context(),
    )

    assert seen == ["factor_a"]
    assert len(validated) == 1
    assert validated[0].generation_trace["strict_validation_candidate_limit"] == 1


@pytest.mark.asyncio
async def test_factor_mining_strict_validation_limit_prioritizes_quick_evidence(monkeypatch) -> None:
    from akshare_mcp.services.factor_mining_factory.factory import FactorMiningFactory
    import akshare_mcp.services.factor_validation_pipeline as validation_pipeline

    seen: list[str] = []

    async def fake_validate(_db, candidate, **_kwargs):
        seen.append(candidate["name"])
        return {
            "success": True,
            "stage": "strict",
            "rating": {"grade": "A"},
            "metrics": {},
            "ic": {"horizon_10": {"ic_mean": 0.12, "ic_ir": 0.9, "sample_count": 40}},
        }

    monkeypatch.setenv("FACTOR_MINING_STRICT_VALIDATION_CANDIDATE_LIMIT", "1")
    monkeypatch.setattr(validation_pipeline, "validate_factor_candidate_pipeline", fake_validate)
    monkeypatch.setattr(
        "akshare_mcp.services.factor_mining_factory.factory.evaluate_validation_evidence",
        lambda _result: {"passed": True},
    )

    weak = _Candidate(
        "weak_factor",
        quick_evidence={
            "passed": True,
            "quality_score": 12.0,
            "rank_ic_mean": 0.01,
            "rank_ic_ir": 0.08,
        },
    )
    strong = _Candidate(
        "strong_factor",
        quick_evidence={
            "passed": True,
            "quality_score": 88.0,
            "rank_ic_mean": 0.06,
            "rank_ic_ir": 0.7,
        },
    )

    factory = FactorMiningFactory()
    validated = await factory._validate_batch(object(), [weak, strong], _Context())

    assert seen == ["strong_factor"]
    assert len(validated) == 1
    assert validated[0].name == "strong_factor"
    assert validated[0].generation_trace["strict_validation_priority_rank"] == 1


@pytest.mark.asyncio
async def test_rule_seed_engine_generates_variants_once(monkeypatch) -> None:
    from akshare_mcp.services.factor_mining_factory.engines.base import (
        FactorCandidate,
        SearchBudget,
    )
    from akshare_mcp.services.factor_mining_factory.engines.rule_engine import RuleSeedEngine

    engine = RuleSeedEngine()
    calls: list[int] = []

    monkeypatch.setattr(engine, "_from_blueprints", lambda _context, _count: [])
    monkeypatch.setattr(engine, "_from_seed_library", lambda _count: [])

    def fake_variants(count: int):
        calls.append(count)
        return [
            FactorCandidate(
                name="variant_once",
                expression_dsl="close / delay(close, 1) - 1",
                generation_engine="rule_seed",
            )
        ]

    monkeypatch.setattr(engine, "_generate_variants", fake_variants)

    candidates = await engine.generate(object(), SearchBudget(candidate_count=3))

    assert calls == [3]
    assert [candidate.name for candidate in candidates] == ["variant_once"]
