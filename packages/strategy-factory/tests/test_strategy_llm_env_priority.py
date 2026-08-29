from __future__ import annotations

import importlib
import os

from strategy_factory.application import run_models
from strategy_factory.infrastructure.env_loader import load_strategy_llm_env


def test_strategy_llm_env_file_overrides_process_env_without_overriding_non_llm(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "STRATEGY_LLM_MODEL=file-strategy-model",
                "STRATEGY_LLM_BASE_URL=https://file-strategy.example.test/v1",
                "STRATEGY_LLM_API_KEY=sk-file-strategy",
                "STRATEGY_FACTORY_LLM_FAN_OUT_COUNT=4",
                "STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD=0.42",
                "STRATEGY_FACTORY_AI_VALIDATION_ENABLED=true",
                "AI_VALIDATION_LAYER_A_MODEL=file-layer-a",
                "AI_VALIDATION_LAYER_A_FALLBACK=file-layer-a-fallback",
                "AI_VALIDATION_LAYER_C_ANALYST_MODEL=file-layer-c-analyst",
                "AI_VALIDATION_LAYER_C_JUDGE_MODEL=file-layer-c-judge",
                "STRATEGY_FACTORY_READINESS_HARD_BLOCK=1",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STRATEGY_FACTORY_ENV_FILE", str(env_file))
    monkeypatch.setenv("STRATEGY_LLM_MODEL", "process-strategy-model")
    monkeypatch.setenv("STRATEGY_LLM_BASE_URL", "https://process-strategy.example.test/v1")
    monkeypatch.setenv("STRATEGY_LLM_API_KEY", "sk-process-strategy")
    monkeypatch.setenv("STRATEGY_FACTORY_LLM_FAN_OUT_COUNT", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_LLM_TIMEOUT_PARTIAL_THRESHOLD", "0.10")
    monkeypatch.setenv("STRATEGY_FACTORY_AI_VALIDATION_ENABLED", "false")
    monkeypatch.setenv("AI_VALIDATION_LAYER_A_MODEL", "process-layer-a")
    monkeypatch.setenv("AI_VALIDATION_LAYER_A_FALLBACK", "process-layer-a-fallback")
    monkeypatch.setenv("AI_VALIDATION_LAYER_C_ANALYST_MODEL", "process-layer-c-analyst")
    monkeypatch.setenv("AI_VALIDATION_LAYER_C_JUDGE_MODEL", "process-layer-c-judge")
    monkeypatch.setenv("STRATEGY_FACTORY_READINESS_HARD_BLOCK", "0")

    loaded = load_strategy_llm_env()
    from strategy_factory.application.ai_validation import config as ai_validation_config

    ai_validation_config = importlib.reload(ai_validation_config)

    assert loaded == env_file
    assert os.environ["STRATEGY_LLM_MODEL"] == "file-strategy-model"
    assert os.environ["STRATEGY_LLM_BASE_URL"] == "https://file-strategy.example.test/v1"
    assert os.environ["STRATEGY_LLM_API_KEY"] == "sk-file-strategy"
    assert os.environ["STRATEGY_FACTORY_LLM_FAN_OUT_COUNT"] == "4"
    assert os.environ["STRATEGY_FACTORY_AI_VALIDATION_ENABLED"] == "true"
    assert os.environ["STRATEGY_FACTORY_READINESS_HARD_BLOCK"] == "0"
    assert run_models._resolve_llm_timeout_partial_threshold() == 0.42
    assert ai_validation_config.AI_VALIDATION_ENABLED is True
    assert ai_validation_config.AI_VALIDATION_CONFIG["layer_a"]["primary_model"] == "file-layer-a"
    assert ai_validation_config.AI_VALIDATION_CONFIG["layer_a"]["fallback_model"] == "file-layer-a-fallback"
    assert ai_validation_config.AI_VALIDATION_CONFIG["layer_c"]["analyst_model"] == "file-layer-c-analyst"
    assert ai_validation_config.AI_VALIDATION_CONFIG["layer_c"]["judge_model"] == "file-layer-c-judge"
