"""Эфемерные креды агента из LLM-подключения проекта.

Маппинг ProviderConnection → env песочницы + имя модели для адаптера. Ключ
никуда не персистится — только в env прогона. Чистый маппинг, тестируется в
изоляции.
"""

from __future__ import annotations

from pov_generator.domain.llm_settings import ProviderConnection, ProviderCredentials
from pov_generator.infrastructure.harness.credentials import (
    HarnessCredentials,
    credentials_from_connection,
)


def _conn(provider_type: str, *, api_key: str | None = "secret-key", **extras: str) -> ProviderConnection:
    return ProviderConnection(
        connection_id="c1",
        provider_type=provider_type,  # type: ignore[arg-type]
        display_name=f"{provider_type} conn",
        credentials=ProviderCredentials(api_key=api_key),
        extras=dict(extras),
    )


def test_openrouter_creds_and_litellm_prefix_for_aider() -> None:
    creds = credentials_from_connection(
        _conn("openrouter", base_url="https://openrouter.ai/api/v1"),
        model="anthropic/claude-3.5-sonnet",
        adapter="aider",
    )
    assert creds.env["OPENROUTER_API_KEY"] == "secret-key"
    assert creds.env["OPENAI_API_KEY"] == "secret-key"
    assert creds.env["OPENAI_API_BASE"] == "https://openrouter.ai/api/v1"
    # Уже с префиксом ('/') — не трогаем.
    assert creds.model == "anthropic/claude-3.5-sonnet"


def test_openrouter_prefixes_bare_model_for_aider() -> None:
    creds = credentials_from_connection(_conn("openrouter"), model="gpt-4o", adapter="aider")
    assert creds.model == "openrouter/gpt-4o"


def test_anthropic_creds_for_claude_code_no_prefix() -> None:
    creds = credentials_from_connection(
        _conn("anthropic"), model="claude-sonnet-4-5", adapter="claude_code"
    )
    assert creds.env["ANTHROPIC_API_KEY"] == "secret-key"
    assert "OPENAI_API_KEY" not in creds.env
    # claude_code (CLI) принимает чистое имя модели.
    assert creds.model == "claude-sonnet-4-5"


def test_anthropic_prefixes_model_for_aider() -> None:
    creds = credentials_from_connection(
        _conn("anthropic"), model="claude-sonnet-4-5", adapter="aider"
    )
    assert creds.model == "anthropic/claude-sonnet-4-5"


def test_claude_cli_has_no_injected_key() -> None:
    # Локальная сессия CLI — ключа нет; в docker инъецировать нечего.
    creds = credentials_from_connection(_conn("claude_cli", api_key=None), model="x", adapter="claude_code")
    assert creds.env == {}
    assert creds.model == "x"


def test_none_connection_is_empty() -> None:
    creds = credentials_from_connection(None, model="x", adapter="aider")
    assert creds == HarnessCredentials()
