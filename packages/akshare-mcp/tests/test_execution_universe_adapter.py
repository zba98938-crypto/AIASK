from __future__ import annotations

import pytest

from akshare_mcp.services.execution_universe_adapter import (
    load_executable_strategies_with_fallback,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return list(self._rows)


class _ContractDb:
    async def execute(self, sql, _params):
        if "ia.stage = 'warmup'" in sql:
            return _Cursor(
                [
                    (
                        "strategy-1",
                        "paper candidate",
                        "momentum",
                        "submitted",
                        "2026-07-23T00:00:00+00:00",
                        "warmup",
                        "active",
                        "account-1",
                    )
                ]
            )
        return _Cursor([])


@pytest.mark.asyncio
async def test_contract_adapter_uses_canonical_signature_and_explicit_fields():
    result = await load_executable_strategies_with_fallback(_ContractDb())

    assert result == [
        {
            "id": "strategy-1",
            "strategy_id": "strategy-1",
            "name": "paper candidate",
            "status": "submitted",
            "incubating": "warmup",
            "account_id": "account-1",
            "execution_mode": "paper",
            "runtime_control_enabled": False,
            "runtime_control_reason": None,
            "strategy_type": "momentum",
            "params": {},
        }
    ]


@pytest.mark.asyncio
async def test_contract_failure_does_not_expand_to_legacy_universe(monkeypatch):
    class _BrokenDb:
        async def execute(self, _sql, _params):
            raise RuntimeError("database unavailable")

        async def list_strategies(self, _status, limit=500):
            del limit
            return [{"id": "must-not-leak", "status": "submitted"}]

    result = await load_executable_strategies_with_fallback(_BrokenDb())

    assert result == []


@pytest.mark.asyncio
async def test_legacy_universe_requires_explicit_opt_out():
    class _LegacyDb:
        async def list_strategies(self, status, limit=500):
            del limit
            return [{"id": f"{status}-1", "status": status}]

    result = await load_executable_strategies_with_fallback(
        _LegacyDb(),
        use_contract=False,
    )

    assert {item["id"] for item in result} == {
        "submitted-1",
        "incubating-1",
        "listed-1",
    }
