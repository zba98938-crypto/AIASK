"""Compatibility facade for quant_manager artifact handlers."""

from __future__ import annotations

from .quant_mgr_registry import (
    handle_factor_candidate_registry,
    handle_factor_research_memory,
)
from .quant_mgr_model_registry import (
    handle_champion_challenger,
    handle_model_registry,
)
from .quant_mgr_replay import (
    handle_factor_ic_history,
    handle_feature_store,
    handle_replay_experiment,
    handle_replay_factor_episode,
)
from .quant_mgr_scheduler import (
    handle_scheduler_run_now,
    handle_scheduler_status,
)

__all__ = [
    "handle_champion_challenger",
    "handle_factor_candidate_registry",
    "handle_factor_ic_history",
    "handle_factor_research_memory",
    "handle_feature_store",
    "handle_model_registry",
    "handle_replay_experiment",
    "handle_replay_factor_episode",
    "handle_scheduler_run_now",
    "handle_scheduler_status",
]
