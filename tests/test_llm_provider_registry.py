"""Тесты для `infrastructure.llm.LLMProviderRegistry`.

Реестр — единственная точка switch'а по имени LLM-провайдера в коде.
Эти тесты фиксируют контракт, на который опираются ExecutionService,
DomainPackSelectionService, ClarificationService и ComplexitySelectorService.
"""

from __future__ import annotations

import pytest

from pov_generator.common.errors import ConflictError
from pov_generator.common.llm_observation import llm_observation_scope
from pov_generator.infrastructure.llm import LLMProvider, LLMProviderRegistry
from pov_generator.infrastructure.llm.protocol import LLMResult, LLMUsage
from pov_generator.infrastructure.llm.providers.claude_sdk import ClaudeSdkProvider
from pov_generator.infrastructure.llm.providers.claude_subscription import (
    ClaudeSubscriptionProvider,
)
from pov_generator.infrastructure.llm.providers.openrouter import OpenRouterProvider
from pov_generator.infrastructure.llm.registry import LoggingLLMProvider
from pov_generator.infrastructure.observability import NullSink, reset_llm_sink, set_llm_sink
from pov_generator.infrastructure.observability.langfuse_sink import get_llm_sink


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

    # registry оборачивает провайдер двумя декораторами: CompositionalLLMProvider
    # (сборка сложных схем по частям) снаружи → LoggingLLMProvider (логирование)
    # → конкретный адаптер. Адаптер — под .inner.inner.
    assert isinstance(provider.inner.inner, OpenRouterProvider)
    assert provider.name == "openrouter"
    assert provider.model == "openai/gpt-4.1-mini"
    # Structural-typing protocol: обёртка тоже удовлетворяет LLMProvider.
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

    assert isinstance(trivial.inner.inner, ClaudeSdkProvider)
    assert isinstance(complex_.inner.inner, ClaudeSdkProvider)
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

    assert isinstance(provider.inner.inner, ClaudeSubscriptionProvider)
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


# ---------------------------------------------------------------------------
# v3.11 — наблюдаемость через LoggingLLMProvider (ships dark)
# ---------------------------------------------------------------------------


class _FakeInner:
    name = "fake"
    model = "fake-model"

    def __init__(self, result: LLMResult) -> None:
        self._result = result

    def chat_json(self, *, system_prompt, user_prompt, schema) -> LLMResult:  # noqa: ANN001
        del system_prompt, user_prompt, schema
        return self._result


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def emit(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_logging_provider_emits_observation_with_bound_context() -> None:
    """Чокпоинт эмитит в синк provider/model/usage/prompt/response + ambient
    контекст (purpose/task_id из llm_observation_scope)."""
    usage = LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15, source="actual")
    provider = LoggingLLMProvider(
        _FakeInner(LLMResult(payload={"ok": True}, usage=usage)),
        purpose="decision_identification",
    )
    sink = _RecordingSink()
    set_llm_sink(sink)
    try:
        with llm_observation_scope(purpose="decision_identification", task_id="t-1", project_id="p-1"):
            result = provider.chat_json(system_prompt="SYS", user_prompt="USR", schema={})
    finally:
        reset_llm_sink()

    assert result.payload == {"ok": True}
    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["purpose"] == "decision_identification"
    assert call["provider"] == "fake"
    assert call["model"] == "fake-model"
    assert call["usage"] is usage
    assert call["system_prompt"] == "SYS"
    assert call["response"] == {"ok": True}
    assert call["context"]["task_id"] == "t-1"
    assert call["context"]["project_id"] == "p-1"


def test_logging_provider_emits_observation_on_error() -> None:
    class _Boom:
        name = "boom"
        model = "m"

        def chat_json(self, **_kwargs):
            raise RuntimeError("down")

    provider = LoggingLLMProvider(_Boom(), purpose="primary_generation")
    sink = _RecordingSink()
    set_llm_sink(sink)
    try:
        with pytest.raises(RuntimeError):
            provider.chat_json(system_prompt="s", user_prompt="u", schema={})
    finally:
        reset_llm_sink()

    assert len(sink.calls) == 1
    assert sink.calls[0]["error"] == "down"
    assert sink.calls[0]["response"] is None


def test_default_sink_is_nullsink_without_env(monkeypatch) -> None:
    """Без Langfuse-env синк — NullSink (no-op), а chat_json работает как раньше."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    reset_llm_sink()
    try:
        assert isinstance(get_llm_sink(), NullSink)
        provider = LoggingLLMProvider(_FakeInner(LLMResult(payload={"x": 1}, usage=None)))
        result = provider.chat_json(system_prompt="s", user_prompt="u", schema={})
        assert result.payload == {"x": 1}
    finally:
        reset_llm_sink()
