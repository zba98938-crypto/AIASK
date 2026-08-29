from __future__ import annotations

import os
from pathlib import Path


def aiask_agent_home() -> Path:
    raw = str(os.getenv("AIASK_AGENT_HOME", "")).strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aiask-agent"


def default_state_db_path() -> Path:
    raw = str(os.getenv("AIASK_AGENT_STATE_DB", "")).strip()
    if raw:
        return Path(raw).expanduser()
    return aiask_agent_home() / "state.sqlite3"


def default_intent_db_path() -> Path:
    raw = str(os.getenv("AIASK_AGENT_INTENT_DB", "")).strip()
    if raw:
        return Path(raw).expanduser()
    return aiask_agent_home() / "action_intents.sqlite3"


def default_quant_research_db_path() -> Path:
    raw = str(os.getenv("AIASK_AGENT_QUANT_RESEARCH_DB", "")).strip()
    if raw:
        return Path(raw).expanduser()
    return aiask_agent_home() / "quant_research.sqlite3"
