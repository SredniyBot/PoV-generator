"""Реестр LLM-провайдеров.

Единственное место в кодовой базе, где живёт switch по имени провайдера.
Все application-сервисы (execution / domain pack selection / clarification /
complexity selector) принимают экземпляр :class:`LLMProviderRegistry`
через конструктор и обращаются к нему через
``registry.from_env(...).chat_json(...)``.

Добавление нового провайдера = новый адаптер в ``providers/`` + одна
строка в ``_BUILDERS``. Сервисы не меняются.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from ...common.errors import ConflictError
from .protocol import LLMProvider
from .providers.claude_sdk import ClaudeSdkProvider
from .providers.claude_subscription import ClaudeSubscriptionProvider
from .providers.openrouter import OpenRouterProvider


@dataclass(frozen=True)
class LLMResolution:
    """Результат резолва env → провайдер: что в итоге выбрано."""

    provider_name: str
    model: str | None


# Билдеры провайдеров: имя → callable, который собирает экземпляр
# с учётом model + complexity. Каждый билдер изолирован в своём
# адаптере; здесь только маршрутизация.
ProviderBuilder = Callable[[str | None, str | None], LLMProvider]


def _build_openrouter(model: str | None, complexity: str | None) -> LLMProvider:
    # OpenRouter не маппит complexity на модель — модель явно задаётся
    # POV_OPENROUTER_MODEL или флагом команды.
    del complexity
    return OpenRouterProvider(model=model)


def _build_claude_sdk(model: str | None, complexity: str | None) -> LLMProvider:
    return ClaudeSdkProvider(model=model, complexity=complexity)


def _build_claude_subscription(model: str | None, complexity: str | None) -> LLMProvider:
    return ClaudeSubscriptionProvider(model=model, complexity=complexity)


_BUILDERS: dict[str, ProviderBuilder] = {
    "openrouter": _build_openrouter,
    "claude_sdk": _build_claude_sdk,
    "claude_subscription": _build_claude_subscription,
}


class LLMProviderRegistry:
    """Резолвит и собирает :class:`LLMProvider` по имени или из env."""

    @property
    def supported_providers(self) -> tuple[str, ...]:
        return tuple(sorted(_BUILDERS.keys()))

    def get(
        self,
        *,
        provider: str,
        model: str | None = None,
        complexity: str | None = None,
    ) -> LLMProvider:
        """Собрать провайдер по точному имени.

        Args:
            provider: каноническое имя (``openrouter`` / ``claude_sdk`` /
                ``claude_subscription``).
            model: явный override модели. Если ``None`` — берётся из env
                и/или из complexity-маппинга соответствующего адаптера.
            complexity: ``trivial`` / ``standard`` / ``complex``. Релевантно
                только для Claude-провайдеров (там есть маппинг на модель).

        Raises:
            ConflictError: если имя провайдера не зарегистрировано.
        """
        builder = _BUILDERS.get(provider)
        if builder is None:
            raise ConflictError(
                f"Неподдерживаемый LLM-провайдер: '{provider}'. "
                f"Поддерживаются: {', '.join(self.supported_providers)}."
            )
        return builder(model, complexity)

    def from_env(
        self,
        *,
        override_provider: str | None = None,
        override_model: str | None = None,
        env_provider_var: str = "POV_EXECUTION_PROVIDER",
        env_model_var: str | None = None,
        complexity: str | None = None,
    ) -> LLMProvider:
        """Авто-резолв провайдера из env с возможностью override.

        Порядок:

        1. ``override_provider`` (например, из CLI/команды) — побеждает всё.
        2. ``env_provider_var`` (по умолчанию ``POV_EXECUTION_PROVIDER``)
           — основной канал конфигурации. Для отдельных сервисов можно
           указать свой env-var (например,
           ``POV_DOMAIN_PACK_SELECTION_PROVIDER``).
        3. ``"openrouter"``, если задан ``POV_OPENROUTER_API_KEY``, иначе
           провайдер не может быть выбран — ``ConflictError``.

        Модель резолвится по той же логике:
        ``override_model`` → ``env_model_var`` → дефолт адаптера.
        """
        provider_name = (
            override_provider
            or os.environ.get(env_provider_var)
            or self._fallback_provider()
        )
        if provider_name is None:
            raise ConflictError(
                f"Не задан LLM-провайдер. Установите {env_provider_var} "
                f"или передайте override. Поддерживаются: "
                f"{', '.join(self.supported_providers)}."
            )

        model_name = override_model
        if model_name is None and env_model_var is not None:
            model_name = os.environ.get(env_model_var)

        return self.get(provider=provider_name, model=model_name, complexity=complexity)

    def _fallback_provider(self) -> str | None:
        """Если env-переменная провайдера пуста — мягкий fallback.

        Не выбираем CLI-варианты (``claude_*``) автоматически: их активация
        требует осознанного действия (CLI установлен и залогинен / ключ
        задан). Поэтому единственный auto-fallback — OpenRouter при
        наличии его ключа.
        """
        if os.environ.get("POV_OPENROUTER_API_KEY"):
            return "openrouter"
        return None
