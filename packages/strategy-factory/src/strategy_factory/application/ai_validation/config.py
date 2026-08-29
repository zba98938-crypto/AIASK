"""AI validation system configuration."""

from __future__ import annotations

import os
from typing import Any

from ...infrastructure.env_loader import load_strategy_llm_env

load_strategy_llm_env()

# Feature flag: enable AI validation (shadow mode by default)
AI_VALIDATION_ENABLED = os.getenv("STRATEGY_FACTORY_AI_VALIDATION_ENABLED", "false").strip().lower() == "true"
AI_VALIDATION_MODE = os.getenv("STRATEGY_FACTORY_AI_VALIDATION_MODE", "shadow").strip().lower()  # shadow / active / disabled

# Model configuration
AI_VALIDATION_CONFIG = {
    "layer_a": {
        "primary_model": os.getenv("AI_VALIDATION_LAYER_A_MODEL", "deepseek-v4-pro"),
        "fallback_model": os.getenv("AI_VALIDATION_LAYER_A_FALLBACK", "claude-opus-4"),
        "temperature": float(os.getenv("AI_VALIDATION_LAYER_A_TEMPERATURE", "0.1")),
        "max_tokens": int(os.getenv("AI_VALIDATION_LAYER_A_MAX_TOKENS", "2000")),
        "timeout_sec": float(os.getenv("AI_VALIDATION_LAYER_A_TIMEOUT_SEC", "15")),
        "retry_count": int(os.getenv("AI_VALIDATION_LAYER_A_RETRY_COUNT", "1")),
    },
    "layer_b": {
        "model_type": "lightgbm",
        "model_path": os.getenv("AI_VALIDATION_LAYER_B_MODEL_PATH", ""),
        "min_training_samples": int(os.getenv("AI_VALIDATION_LAYER_B_MIN_SAMPLES", "100")),
        "retrain_interval_days": int(os.getenv("AI_VALIDATION_LAYER_B_RETRAIN_DAYS", "7")),
        "fallback_to_rules": True,
    },
    "layer_c": {
        "analyst_model": os.getenv("AI_VALIDATION_LAYER_C_ANALYST_MODEL", "deepseek-v4-pro"),
        "judge_model": os.getenv("AI_VALIDATION_LAYER_C_JUDGE_MODEL", "deepseek-v4-pro"),
        "debate_rounds": int(os.getenv("AI_VALIDATION_LAYER_C_DEBATE_ROUNDS", "2")),
        "timeout_per_agent_sec": float(os.getenv("AI_VALIDATION_LAYER_C_AGENT_TIMEOUT", "20")),
        "total_timeout_sec": float(os.getenv("AI_VALIDATION_LAYER_C_TOTAL_TIMEOUT", "90")),
        "consensus_threshold": float(os.getenv("AI_VALIDATION_LAYER_C_CONSENSUS", "0.6")),
        "min_quality_score_to_trigger": float(os.getenv("AI_VALIDATION_LAYER_C_MIN_SCORE", "0.5")),
    },
    "consensus": {
        "n_calls": int(os.getenv("AI_VALIDATION_CONSENSUS_CALLS", "3")),
        "agreement_min": int(os.getenv("AI_VALIDATION_CONSENSUS_MIN", "2")),
    },
    "fusion": {
        "layer_a_weight": float(os.getenv("AI_VALIDATION_WEIGHT_A", "0.20")),
        "layer_b_weight": float(os.getenv("AI_VALIDATION_WEIGHT_B", "0.45")),
        "layer_c_weight": float(os.getenv("AI_VALIDATION_WEIGHT_C", "0.35")),
        "approve_threshold": float(os.getenv("AI_VALIDATION_APPROVE_THRESHOLD", "0.6")),
        "observe_threshold": float(os.getenv("AI_VALIDATION_OBSERVE_THRESHOLD", "0.4")),
    },
}
