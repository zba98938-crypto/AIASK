from __future__ import annotations

from types import SimpleNamespace


class CompleteRepository:
    def __getattr__(self, name):
        from strategy_factory.application.runtime_boundary import REQUIRED_REPOSITORY_METHODS

        if name not in REQUIRED_REPOSITORY_METHODS:
            raise AttributeError(name)

        async def _method(*_args, **_kwargs):
            return []

        return _method


def _runtime_adapters(db):
    return SimpleNamespace(
        repository=SimpleNamespace(raw=db),
        vector_search=object(),
        autonomy=object(),
        factor_research=object(),
        incubation=object(),
        validation=object(),
        risk=object(),
    )


def test_validate_strategy_factory_runtime_reports_missing_methods_and_adapters():
    from strategy_factory.api.runtime import validate_strategy_factory_runtime

    report = validate_strategy_factory_runtime(object(), None)

    assert report.ok is False
    assert report.status == "runtime_boundary_failed"
    assert "get_klines" in report.missing_repository_methods
    assert "repository" in report.missing_runtime_adapters
    assert "missing_repository_method:get_klines" in report.blocking_reason_codes


def test_validate_strategy_factory_runtime_accepts_complete_runtime():
    from strategy_factory.api.runtime import validate_strategy_factory_runtime

    db = CompleteRepository()
    report = validate_strategy_factory_runtime(db, _runtime_adapters(db))

    assert report.ok is True
    assert report.status == "ok"
    assert report.to_dict()["contract_version"] == "strategy_factory.runtime_boundary.v1"


def test_repository_adapter_covers_strategy_factory_repository_protocol():
    from strategy_factory.api.contracts import StrategyFactoryRepository
    from strategy_factory.api.runtime import StrategyFactoryRepositoryAdapter

    protocol_methods = {
        name
        for name, value in StrategyFactoryRepository.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    adapter_methods = {
        name
        for name in dir(StrategyFactoryRepositoryAdapter)
        if not name.startswith("_") and callable(getattr(StrategyFactoryRepositoryAdapter, name))
    }

    assert protocol_methods - adapter_methods == set()
