"""skills-category tool contracts (split from tool_catalog)."""

from __future__ import annotations

from typing import Any

from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract

CONTRACTS: dict[str, dict[str, Any]] = {
    "run_skill": _contract(
        name="run_skill",
        title="Run Skill",
        category="skills",
        description="Execute a bundled orchestrated skill for higher-level domain workflows.",
        required_params=["skill_id"],
        input_schema={
            "type": "object",
            "properties": {"skill_id": {"type": "string"}, "params": {"type": "object"}},
            "required": ["skill_id"],
            "additionalProperties": True,
        },
        side_effect_level="read_only",
        freshness="depends_on_skill_and_inputs",
        examples=[{"description": "Run factor mining skill", "arguments": {"skill_id": "akshare-factor-mining", "params": {"task": "candidate_pipeline"}}}],
        tags=["skills", "workflow"],
    ),
    # ── Key bottom-level managers ────────────────────────────────────────────
}
