from __future__ import annotations

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
                "FACTOR_LLM_MODEL=file-factor-model",
                "STRATEGY_EMBEDDING_MODEL=file-embedding-model",
                "STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC=77",
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
    monkeypatch.setenv("FACTOR_LLM_MODEL", "process-factor-model")
    monkeypatch.setenv("STRATEGY_EMBEDDING_MODEL", "process-embedding-model")
    monkeypatch.setenv("STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC", "11")
    monkeypatch.setenv("STRATEGY_FACTORY_READINESS_HARD_BLOCK", "0")

    loaded = load_strategy_llm_env()

    assert loaded == env_file
    assert os.environ["STRATEGY_LLM_MODEL"] == "file-strategy-model"
    assert os.environ["STRATEGY_LLM_BASE_URL"] == "https://file-strategy.example.test/v1"
    assert os.environ["STRATEGY_LLM_API_KEY"] == "sk-file-strategy"
    assert os.environ["STRATEGY_FACTORY_LLM_FAN_OUT_COUNT"] == "4"
    # Every LLM_ENV_PREFIXES entry the file sets must win over the process env.
    assert os.environ["FACTOR_LLM_MODEL"] == "file-factor-model"
    assert os.environ["STRATEGY_EMBEDDING_MODEL"] == "file-embedding-model"
    assert os.environ["STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC"] == "77"
    # A non-LLM key must NOT be overridden by the file.
    assert os.environ["STRATEGY_FACTORY_READINESS_HARD_BLOCK"] == "0"
    assert run_models._resolve_llm_timeout_partial_threshold() == 0.42
