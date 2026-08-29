from __future__ import annotations

import asyncio
from typing import Any

from aiask_agent import native_capabilities as native
from aiask_agent.native_capabilities import SkillStore, build_native_capability_handlers
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tools.policy import GENERAL_FULL_TOOLSET, ToolPolicy


def _handlers(tmp_path, *, skill_store: SkillStore | None = None) -> dict[str, Any]:
    return build_native_capability_handlers(
        policy=ToolPolicy(GENERAL_FULL_TOOLSET, True, (str(tmp_path),)),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        skill_store=skill_store,
    )


def test_skill_store_tolerates_corrupt_numeric_metadata(tmp_path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n\nUse this skill for testing.", encoding="utf-8")
    (root / ".usage.json").write_text(
        '{"demo": {"view_count": "nan", "use_count": "bad", "patch_count": "oops"}}',
        encoding="utf-8",
    )
    store = SkillStore(root)

    [item] = store.list()
    assert item["view_count"] == 0
    assert item["use_count"] == 0

    viewed = store.view("demo", max_chars="bad")  # type: ignore[arg-type]
    assert viewed["truncated"] is False
    assert store.list()[0]["view_count"] == 1


def test_native_web_extract_bounds_invalid_numeric_inputs(tmp_path, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_url(url: str, *, max_bytes: int, timeout: float) -> tuple[str, str, int]:
        captured.update({"url": url, "max_bytes": max_bytes, "timeout": timeout})
        return "<html><body><p>Hello</p></body></html>", "text/html", 200

    monkeypatch.setattr(native, "_fetch_url", fake_fetch_url)
    handlers = _handlers(tmp_path)

    result = asyncio.run(
        handlers["agent_web_extract"](
            {
                "url": "https://example.com",
                "max_bytes": "bad",
                "timeout_seconds": "nan",
                "max_chars": "bad",
            }
        )
    )

    assert result["success"] is True
    assert captured == {"url": "https://example.com", "max_bytes": 262144, "timeout": 15.0}


def test_native_list_tools_bound_invalid_limits(tmp_path) -> None:
    handlers = _handlers(tmp_path)

    terminal = asyncio.run(handlers["agent_terminal_backends"]({"action": "sessions", "limit": "nan"}))
    gateway = asyncio.run(handlers["agent_gateway_history"]({"limit": "bad"}))
    learning = asyncio.run(handlers["agent_learning_review"]({"limit": "inf"}))
    rl_runs = asyncio.run(handlers["agent_rl_list_runs"]({"limit": "bad"}))

    assert terminal["success"] is True
    assert gateway["success"] is True
    assert learning["success"] is True
    assert rl_runs["success"] is True
