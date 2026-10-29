"""decision_consensus must query the tools its contract names.

The tool exists to enforce "cross-validate >= 3 tools before acting" and its
docstring lists five participants. Two of them never ran:

* ``smart_stock_diagnosis`` was imported as ``from . import smart_diagnosis``,
  a module that does not exist (the real one is ``semantic.diagnosis``), and the
  ``except ImportError: pass`` below it hid the mistake.
* ``get_unified_decision`` appeared only in the docstring; nothing called it.

That left ``should_i_buy`` + ``build_stock_context`` — two voters, so a
three-tool agreement was unreachable and one dissent always produced
``agreement_ratio == 0.5`` -> ``split`` -> a forced hold.
"""

from __future__ import annotations

import json
import os

import pytest


_CODE = "000001"

_CONTRACT_TOOLS = {
    "should_i_buy",
    "build_stock_context",
    "smart_stock_diagnosis",
    "get_unified_decision",
}


async def _consensus(**args) -> dict:
    os.environ.setdefault("AKSHARE_MCP_STARTUP_PROFILE", "tool-only")
    from akshare_mcp import server as server_mod

    raw = await server_mod.mcp.call_tool("decision_consensus", {"code": _CODE, **args})
    return json.loads(raw.content[0].text)


def _by_tool(envelope: dict) -> dict[str, dict]:
    decisions = (envelope.get("data") or {}).get("decisions") or []
    return {str(d.get("tool")): d for d in decisions}


@pytest.mark.asyncio
class TestAllContractedToolsAreQueried:
    async def test_every_contract_tool_appears_in_the_decisions_list(self) -> None:
        envelope = await _consensus()
        if not envelope.get("success"):
            pytest.skip(f"consensus unavailable locally: {envelope.get('error')}")
        seen = set(_by_tool(envelope))
        missing = _CONTRACT_TOOLS - seen
        assert not missing, f"contract names these tools but never queried them: {sorted(missing)}"

    async def test_smart_stock_diagnosis_resolves_its_module(self) -> None:
        envelope = await _consensus()
        if not envelope.get("success"):
            pytest.skip("consensus unavailable locally")
        entry = _by_tool(envelope).get("smart_stock_diagnosis")
        assert entry is not None
        # If it is unavailable it must at least say why — never vanish silently.
        if not entry.get("available"):
            assert entry.get("error")
            assert "ModuleNotFoundError" not in str(entry["error"]), (
                "smart_stock_diagnosis is still being imported from the wrong module"
            )

    async def test_get_unified_decision_actually_votes(self) -> None:
        envelope = await _consensus()
        if not envelope.get("success"):
            pytest.skip("consensus unavailable locally")
        entry = _by_tool(envelope).get("get_unified_decision")
        assert entry is not None, "get_unified_decision is documented but not queried"

    async def test_a_three_tool_agreement_is_reachable(self, monkeypatch) -> None:
        # The whole point of the tool. With two voters it was not. The voters
        # are stubbed so the verdict does not depend on what a live database
        # happens to hold; the aggregation under test is the real one.
        _stub_voters(monkeypatch, buy="buy", context="buy", diagnosis="buy", unified="buy")
        envelope = await _consensus()
        assert envelope.get("success"), envelope.get("error")
        data = envelope["data"]
        available = [d for d in _by_tool(envelope).values() if d.get("available")]
        assert len(available) >= 3, (
            f"only {len(available)} tools voted; the >=3 same-direction rule cannot be met"
        )
        # With every voter stubbed, every contracted voter must take part: a
        # participant that silently drops out still leaves three that "agree".
        assert {d["tool"] for d in available} == _CONTRACT_TOOLS
        assert data["cross_tool_consensus"] == "agree"
        assert data["actionable_recommendation"] == "buy"
        assert len(data["tools_agree"]) >= 3

    async def test_three_of_four_still_agree(self, monkeypatch) -> None:
        _stub_voters(monkeypatch, buy="buy", context="buy", diagnosis="buy", unified="sell")
        data = (await _consensus())["data"]
        assert data["cross_tool_consensus"] == "agree"
        assert data["agreement_ratio"] == 0.75
        assert data["tools_split"] == ["get_unified_decision"]

    async def test_four_different_directions_do_not_produce_an_action(self, monkeypatch) -> None:
        _stub_voters(monkeypatch, buy="buy", context="sell", diagnosis="hold", unified="watch")
        data = (await _consensus())["data"]
        assert data["cross_tool_consensus"] == "divergent"
        assert data["actionable_recommendation"] == "no_action"


def _stub_voters(monkeypatch, *, buy: str, context: str, diagnosis: str, unified: str) -> None:
    """Replace the four voters with fixed answers; keep the real consensus code."""
    from akshare_mcp import utils as utils_mod
    from akshare_mcp.tools import _decision_buy, _decision_context, decision
    from akshare_mcp.tools import decision_consensus as consensus_mod
    from akshare_mcp.tools.semantic import diagnosis as diagnosis_mod

    def answer(action: str):
        async def _vote(*_args, **_kwargs):
            return {"success": True, "data": {"recommendation": action, "score": 60, "reason": "stub"}}

        return _vote

    async def _resolve(code, *_args, **_kwargs):
        return str(code)

    monkeypatch.setattr(consensus_mod, "resolve_existing_security_code_async", _resolve)
    monkeypatch.setattr(utils_mod, "resolve_existing_security_code_async", _resolve, raising=False)
    monkeypatch.setattr(_decision_buy, "should_i_buy", answer(buy))
    monkeypatch.setattr(_decision_context, "build_stock_context", answer(context))
    monkeypatch.setattr(diagnosis_mod, "smart_stock_diagnosis", answer(diagnosis))
    monkeypatch.setattr(decision, "get_unified_decision_summary", answer(unified))


@pytest.mark.asyncio
class TestUnavailableToolsAreDisclosed:
    async def test_an_unavailable_participant_is_recorded_not_dropped(self) -> None:
        envelope = await _consensus()
        if not envelope.get("success"):
            pytest.skip("consensus unavailable locally")
        for name, entry in _by_tool(envelope).items():
            if not entry.get("available"):
                assert entry.get("error"), f"{name} is unavailable with no reason given"

    async def test_should_i_sell_is_skipped_only_without_a_buy_price(self) -> None:
        without = _by_tool(await _consensus())
        assert "should_i_sell" not in without

        withprice = await _consensus(buy_price=10.0, holding_days=5)
        if not withprice.get("success"):
            pytest.skip("consensus unavailable locally")
        assert "should_i_sell" in _by_tool(withprice)
