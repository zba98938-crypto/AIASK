from __future__ import annotations

import os

from akshare_mcp.env_loader import load_mcp_env
from akshare_mcp.services._strategy_llm_provider_normalize import StrategyLLMConfig as NormalizeStrategyLLMConfig
from akshare_mcp.services._strategy_llm_provider_prompt import StrategyLLMConfig as PromptStrategyLLMConfig
from akshare_mcp.services.factor_llm_provider import FactorLLMConfig
from akshare_mcp.services.strategy_llm_provider import StrategyLLMConfig


def test_mcp_llm_env_file_overrides_process_env_without_overriding_non_llm(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "STRATEGY_LLM_ENABLED=1",
                "STRATEGY_LLM_MODEL=file-strategy-model",
                "STRATEGY_LLM_BASE_URL=https://file-strategy.example.test/v1",
                "STRATEGY_LLM_API_KEY=sk-file-strategy",
                "FACTOR_LLM_MODEL=file-factor-model",
                "FACTOR_LLM_BASE_URL=https://file-factor.example.test/v1",
                "FACTOR_LLM_API_KEY=sk-file-factor",
                "AKSHARE_MCP_SQLITE_PATH=file-shadow.sqlite3",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AKSHARE_MCP_ENV", str(env_file))
    monkeypatch.setenv("STRATEGY_LLM_MODEL", "process-strategy-model")
    monkeypatch.setenv("STRATEGY_LLM_BASE_URL", "https://process-strategy.example.test/v1")
    monkeypatch.setenv("STRATEGY_LLM_API_KEY", "sk-process-strategy")
    monkeypatch.setenv("FACTOR_LLM_MODEL", "process-factor-model")
    monkeypatch.setenv("FACTOR_LLM_BASE_URL", "https://process-factor.example.test/v1")
    monkeypatch.setenv("FACTOR_LLM_API_KEY", "sk-process-factor")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", "process.sqlite3")

    load_mcp_env(override=False)
    strategy = StrategyLLMConfig.from_env()
    factor = FactorLLMConfig.from_env()

    assert strategy.enabled is True
    assert strategy.model == "file-strategy-model"
    assert strategy.base_url == "https://file-strategy.example.test/v1"
    assert strategy.api_key == "sk-file-strategy"
    assert factor.model == "file-factor-model"
    assert factor.base_url == "https://file-factor.example.test/v1"
    assert factor.api_key == "sk-file-factor"
    assert os.environ["AKSHARE_MCP_SQLITE_PATH"] == "process.sqlite3"


def test_strategy_llm_config_variants_share_lenient_timeout_defaults(monkeypatch) -> None:
    for key in (
        "STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK",
        "STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC",
    ):
        monkeypatch.delenv(key, raising=False)

    public_config = StrategyLLMConfig.from_env()
    normalize_config = NormalizeStrategyLLMConfig.from_env()
    prompt_config = PromptStrategyLLMConfig.from_env()

    assert public_config.recent_timeout_minimal_streak == 3
    assert public_config.recent_timeout_cooldown_sec == 120.0
    assert normalize_config.recent_timeout_minimal_streak == 3
    assert normalize_config.recent_timeout_cooldown_sec == 120.0
    assert prompt_config.recent_timeout_minimal_streak == 3
    assert prompt_config.recent_timeout_cooldown_sec == 120.0
