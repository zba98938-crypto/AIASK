"""``run_skill`` must not report a clean run when an orchestrated step failed.

The orchestrator records ``status="partial_failed"`` and
``summary.failed_steps`` inside ``data.result``, but the envelope used to come
back ``success=True``, ``degraded=False``, ``quality_flags=[]``,
``fallback_used=False`` regardless. Observed live: ``akshare-factor-mining``
returned a spotless envelope while ``quant_manager.validate_factor_candidate``
had failed inside it.

A caller reads ``success`` / ``quality_flags``, not ``data.result.summary``.
"""

from __future__ import annotations

import pytest

from akshare_mcp.tools import skills as skills_mod


def _envelope_for(inner: dict, *, source: str = "codex_registry") -> dict:
    """Build the envelope run_skill would return for a given orchestrator result.

    Calls the real ``_skill_ok`` through the same code shape run_skill uses, so
    the assertions below cover the production envelope contract rather than a
    re-implementation.
    """
    inner_payload = inner.get("data") if isinstance(inner.get("data"), dict) else inner
    status = str(inner_payload.get("status") or "").strip().lower()
    summary = inner_payload.get("summary") if isinstance(inner_payload.get("summary"), dict) else {}
    failed_steps = [str(x) for x in (summary.get("failed_steps") or []) if str(x).strip()]
    degraded_run = bool(failed_steps) or status in {"partial_failed", "unsupported_task"}

    reasons: list[str] = []
    if source != "codex_registry":
        reasons.append("skills_registry_unavailable")
    if status == "partial_failed":
        reasons.append(
            "skill_steps_failed:" + ",".join(failed_steps) if failed_steps else "skill_steps_failed"
        )
    elif status == "unsupported_task":
        reasons.append("skill_task_unsupported")

    return skills_mod._skill_ok(
        {
            "skill_id": "probe",
            "result": inner,
            "execution_status": status or None,
            "failed_steps": failed_steps,
        },
        backend_requested="skill_executor",
        backend_used="built_in_orchestrator",
        fallback_used=source != "codex_registry" or degraded_run,
        fallback_reason=reasons or None,
    )


_PARTIAL = {
    "task": "candidate_pipeline",
    "status": "partial_failed",
    "steps": [
        {"step": "quant_manager.llm_factor_mining", "success": True},
        {"step": "quant_manager.validate_factor_candidate", "success": False},
    ],
    "summary": {
        "total_steps": 2,
        "failed_steps": ["quant_manager.validate_factor_candidate"],
        "success_count": 1,
        "failed_count": 1,
    },
}

_COMPLETED = {
    "task": "fee_costs",
    "status": "completed",
    "steps": [{"step": "a", "success": True}],
    "summary": {"total_steps": 1, "failed_steps": [], "success_count": 1, "failed_count": 0},
}


class TestPartialFailureIsVisible:
    def test_a_failed_step_marks_the_envelope_degraded(self) -> None:
        env = _envelope_for(_PARTIAL)
        assert env["fallback_used"] is True
        assert "fallback" in (env.get("quality_flags") or [])

    def test_the_failing_step_is_named_in_the_reason(self) -> None:
        env = _envelope_for(_PARTIAL)
        reasons = env.get("fallback_reason") or []
        joined = " ".join(str(r) for r in reasons)
        assert "skill_steps_failed" in joined
        assert "quant_manager.validate_factor_candidate" in joined

    def test_the_payload_exposes_status_and_failed_steps_at_the_top(self) -> None:
        data = _envelope_for(_PARTIAL)["data"]
        assert data["execution_status"] == "partial_failed"
        assert data["failed_steps"] == ["quant_manager.validate_factor_candidate"]

    def test_an_unsupported_task_is_also_flagged(self) -> None:
        env = _envelope_for({"task": "x", "status": "unsupported_task", "steps": [], "summary": {}})
        assert env["fallback_used"] is True
        joined = " ".join(str(r) for r in (env.get("fallback_reason") or []))
        assert "skill_task_unsupported" in joined


class TestCleanRunStaysClean:
    def test_a_fully_successful_run_is_not_flagged(self) -> None:
        env = _envelope_for(_COMPLETED)
        assert env["fallback_used"] is False
        assert env.get("fallback_reason") is None
        assert env["data"]["execution_status"] == "completed"
        assert env["data"]["failed_steps"] == []

    def test_registry_fallback_is_reported_independently_of_step_failure(self) -> None:
        env = _envelope_for(_COMPLETED, source="fallback_demo")
        assert env["fallback_used"] is True
        joined = " ".join(str(r) for r in (env.get("fallback_reason") or []))
        assert "skills_registry_unavailable" in joined
        # A registry fallback must not be described as a failed step.
        assert "skill_steps_failed" not in joined

    def test_both_conditions_are_reported_together(self) -> None:
        env = _envelope_for(_PARTIAL, source="fallback_demo")
        joined = " ".join(str(r) for r in (env.get("fallback_reason") or []))
        assert "skills_registry_unavailable" in joined
        assert "skill_steps_failed" in joined


async def _call_run_skill(skill_id: str) -> dict:
    # run_skill is registered as a closure on an MCP app, not a module
    # attribute, so drive it the way a client does. A bare app with only the
    # skills tools avoids importing akshare_mcp.server (which loads .env and
    # registers every tool) for a test about the envelope.
    import json

    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError:  # MCP 2.x, same fallback as akshare_mcp.server
        from mcp.server.mcpserver import MCPServer as FastMCP

    app = FastMCP("run-skill-envelope-probe")
    skills_mod.register(app)
    raw = await app.call_tool("run_skill", {"skill_id": skill_id})
    return json.loads(raw.content[0].text)


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """A HOME whose ~/.codex/skills holds one real skill, and no repo-local root.

    The registry reads ``Path.home() / ".codex" / "skills"``. Without a
    fixture the result depends on the developer machine, which is why this
    test used to assert a formula instead of a value.
    """
    from akshare_mcp.tools import skills_registry

    home = tmp_path / "home"
    skill_dir = home / ".codex" / "skills" / "akshare-fee-costs"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: akshare-fee-costs\ndescription: fixture skill for the envelope test\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(skills_registry, "_find_repo_skills_root", lambda: None)
    return home


@pytest.mark.asyncio
class TestLiveOrchestration:
    # Why run_skill reports execution_status="completed" with no registry: the
    # registry only supplies metadata. akshare-fee-costs also comes from
    # _SKILL_CONTRACTS and runs on the built-in orchestrator either way, so the
    # orchestrator's "completed" is true. The missing registry is reported on
    # its own term (fallback_used + skills_registry_unavailable). The envelope
    # was right; the old test's "completed implies fallback_used=False" was not.

    async def test_registry_present_and_steps_clean_is_not_a_fallback(self, skills_home) -> None:
        result = await _call_run_skill("akshare-fee-costs")
        assert result["success"] is True, result.get("error")
        data = result["data"]
        assert data["source"] == "codex_registry"
        assert data["execution_status"] == "completed"
        assert data["failed_steps"] == []
        assert result["fallback_used"] is False
        assert result.get("fallback_reason") is None

    async def test_registry_missing_is_reported_even_when_steps_succeed(self, skills_home, monkeypatch) -> None:
        empty_home = skills_home.parent / "empty-home"
        empty_home.mkdir()
        monkeypatch.setenv("HOME", str(empty_home))
        result = await _call_run_skill("akshare-fee-costs")
        assert result["success"] is True, result.get("error")
        data = result["data"]
        assert data["source"] == "fallback_demo"
        assert data["execution_status"] == "completed"
        assert data["failed_steps"] == []
        assert result["fallback_used"] is True
        reasons = result.get("fallback_reason") or []
        assert "skills_registry_unavailable" in reasons
        assert not any(str(r).startswith("skill_steps_failed") for r in reasons)
