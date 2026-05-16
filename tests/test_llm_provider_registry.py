"""Тесты для `infrastructure.llm.LLMProviderRegistry`.

Реестр — единственная точка switch'а по имени LLM-провайдера в коде.
Эти тесты фиксируют контракт, на который опираются ExecutionService,
DomainPackSelectionService, ClarificationService и ComplexitySelectorService.
"""

from __future__ import annotations

import pytest

from pov_generator.common.errors import ConflictError
from pov_generator.infrastructure.llm import LLMProvider, LLMProviderRegistry
from pov_generator.infrastructure.llm.providers.claude_sdk import ClaudeSdkProvider
from pov_generator.infrastructure.llm.providers.claude_subscription import (
    ClaudeSubscriptionProvider,
)
from pov_generator.infrastructure.llm.providers.openrouter import OpenRouterProvider


def test_registry_lists_all_supported_providers() -> None:
    """Контракт реестра: явно перечислены три LLM-провайдера. ``stub``
    сюда не входит — это другой паттерн исполнения (fixture-замена),
    а не LLM-вызов."""
    registry = LLMProviderRegistry()
    assert set(registry.supported_providers) == {
        "openrouter",
        "claude_sdk",
        "claude_subscription",
    }


def test_registry_get_unknown_provider_raises(monkeypatch) -> None:
    registry = LLMProviderRegistry()
    monkeypatch.delenv("POV_OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConflictError, match="Неподдерживаемый LLM-провайдер"):
        registry.get(provider="some_made_up_provider")


def test_registry_get_openrouter_returns_adapter(monkeypatch) -> None:
    monkeypatch.setenv("POV_OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    registry = LLMProviderRegistry()

    provider = registry.get(provider="openrouter")

    assert isinstance(provider, OpenRouterProvider)
    assert provider.name == "openrouter"
    assert provider.model == "openai/gpt-4.1-mini"
    # Structural-typing protocol: адаптер удовлетворяет LLMProvider.
    assert isinstance(provider, LLMProvider)


def test_registry_get_claude_sdk_uses_complexity_to_pick_model(monkeypatch) -> None:
    monkeypatch.setenv("POV_ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("POV_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("POV_CLAUDE_MODEL_TRIVIAL", raising=False)
    monkeypatch.delenv("POV_CLAUDE_MODEL_STANDARD", raising=False)
    monkeypatch.delenv("POV_CLAUDE_MODEL_COMPLEX", raising=False)
    registry = LLMProviderRegistry()

    trivial = registry.get(provider="claude_sdk", complexity="trivial")
    complex_ = registry.get(provider="claude_sdk", complexity="complex")

    assert isinstance(trivial, ClaudeSdkProvider)
    assert isinstance(complex_, ClaudeSdkProvider)
    # Разные complexity дают разные модели.
    assert trivial.model != complex_.model
    assert "haiku" in (trivial.model or "")
    assert "opus" in (complex_.model or "")


def test_registry_get_claude_subscription_model_may_be_none(monkeypatch) -> None:
    """ClaudeSubscriptionProvider допускает model=None — тогда модель
    определяется CLI/сессией."""
    for env_var in (
        "POV_CLAUDE_MODEL",
        "POV_CLAUDE_MODEL_TRIVIAL",
        "POV_CLAUDE_MODEL_STANDARD",
        "POV_CLAUDE_MODEL_COMPLEX",
    ):
        monkeypatch.delenv(env_var, raising=False)
    registry = LLMProviderRegistry()

    provider = registry.get(provider="claude_subscription")

    assert isinstance(provider, ClaudeSubscriptionProvider)
    assert provider.name == "claude_subscription"
    # model может быть None (CLI определит сам); важно, что не ошибка.
    assert provider.model is None or isinstance(provider.model, str)


def test_registry_from_env_respects_override_provider(monkeypatch) -> None:
    monkeypatch.setenv("POV_OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("POV_EXECUTION_PROVIDER", "claude_sdk")
    monkeypatch.setenv("POV_ANTHROPIC_API_KEY", "sk-ant-test")
    registry = LLMProviderRegistry()

    # override побеждает env-переменную
    provider = registry.from_env(override_provider="openrouter")
    assert provider.name == "openrouter"


def test_registry_from_env_uses_custom_env_var_for_provider(monkeypatch) -> None:
    """Сервисы могут указывать собственный env-key (например,
    POV_DOMAIN_PACK_SELECTION_PROVIDER). Реестр это уважает."""
    monkeypatch.setenv("POV_OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("POV_EXECUTION_PROVIDER", raising=False)
    monkeypatch.setenv("POV_DOMAIN_PACK_SELECTION_PROVIDER", "openrouter")
    registry = LLMProviderRegistry()

    provider = registry.from_env(env_provider_var="POV_DOMAIN_PACK_SELECTION_PROVIDER")
    assert provider.name == "openrouter"


def test_registry_from_env_fallbacks_to_openrouter_if_key_present(monkeypatch) -> None:
    """Если ни override, ни env-переменная не заданы, реестр падает на
    OpenRouter (если есть ключ). Claude НЕ выбирается автоматически:
    его активация требует осознанного действия."""
    monkeypatch.delenv("POV_EXECUTION_PROVIDER", raising=False)
    monkeypatch.delenv("POV_DOMAIN_PACK_SELECTION_PROVIDER", raising=False)
    monkeypatch.setenv("POV_OPENROUTER_API_KEY", "sk-test")
    registry = LLMProviderRegistry()

    provider = registry.from_env()
    assert provider.name == "openrouter"


def test_registry_from_env_raises_if_nothing_configured(monkeypatch) -> None:
    for env_var in (
        "POV_EXECUTION_PROVIDER",
        "POV_DOMAIN_PACK_SELECTION_PROVIDER",
        "POV_OPENROUTER_API_KEY",
        "POV_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
    registry = LLMProviderRegistry()

    with pytest.raises(ConflictError, match="Не задан LLM-провайдер"):
        registry.from_env()
