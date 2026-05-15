"""Резолвер LLM-провайдеров.

Единственное место в кодовой базе, где живёт switch по типу провайдера.
Сервисам выдаёт готовый :class:`LLMProvider`-инстанс.

Два режима резолва:

1. :meth:`get` — низкоуровневый, по точному типу провайдера и (опционально)
   модели. Используется в complexity_selector_service и в тестах. Кредиты
   читаются из env (legacy путь).

2. :meth:`resolve_for_purpose` — высокоуровневый, через settings-store:
   ``purpose → ModelAssignment → ModelRouting → ProviderConnection``.
   Это **основной путь** для всех application-сервисов. Сервис говорит
   «дай модель для clarification_ce11», resolver сам находит, через
   какой connection её достать.

Адаптеры (``providers/*.py``) умеют конструироваться тремя способами:

* напрямую: ``OpenRouterProvider(api_key=..., model=...)``.
* из env: ``OpenRouterProvider.from_env(model=...)`` — используется методом
  :meth:`get`.
* из connection: ``OpenRouterProvider.from_connection(conn, model=...)``
  — используется :meth:`resolve_for_purpose`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from ...common.errors import ConflictError
from ...domain.llm_settings import (
    PURPOSE_EXECUTION_COMPLEX,
    PURPOSE_EXECUTION_STANDARD,
    PURPOSE_EXECUTION_TRIVIAL,
    ProviderConnection,
)
from .protocol import LLMProvider
from .providers.claude_sdk import ClaudeSdkProvider
from .providers.claude_subscription import ClaudeSubscriptionProvider
from .providers.openrouter import OpenRouterProvider


@dataclass(frozen=True)
class LLMResolution:
    """Результат резолва env → провайдер: что в итоге выбрано.

    Используется в логах / UI для отладки «какая модель реально пошла».
    """

    provider_name: str
    model: str | None


# Билдеры провайдеров из env. Каждый билдер — изолирован в своём адаптере.
ProviderBuilder = Callable[[str | None, str | None], LLMProvider]


def _build_openrouter_from_env(model: str | None, complexity: str | None) -> LLMProvider:
    del complexity  # OpenRouter не маппит complexity на модель
    return OpenRouterProvider.from_env(model=model)


def _build_claude_sdk_from_env(model: str | None, complexity: str | None) -> LLMProvider:
    return ClaudeSdkProvider.from_env(model=model, complexity=complexity)


def _build_claude_subscription_from_env(model: str | None, complexity: str | None) -> LLMProvider:
    return ClaudeSubscriptionProvider.from_env(model=model, complexity=complexity)


_ENV_BUILDERS: dict[str, ProviderBuilder] = {
    "openrouter": _build_openrouter_from_env,
    "claude_sdk": _build_claude_sdk_from_env,
    "claude_subscription": _build_claude_subscription_from_env,
}


# Соответствие provider_type (в ProviderConnection) → имени провайдера (как
# его используют сервисы / получают из env). Anthropic API через
# ``claude_sdk``, локальный CLI через ``claude_subscription``.
_PROVIDER_TYPE_TO_NAME: dict[str, str] = {
    "openrouter": "openrouter",
    "anthropic": "claude_sdk",
    "claude_cli": "claude_subscription",
}


class _SettingsStoreProtocol:
    """Минимальный интерфейс, который registry ожидает от store.

    Полное API живёт в :class:`SqliteSettingsStore`; сюда вынесено только то,
    что нужно registry — чтобы он мог быть протестирован с fake store без
    тащения sqlite-зависимостей в тестовый модуль.
    """

    def get_assignment(self, purpose: str): ...
    def list_routings_for_model(self, model_name: str): ...
    def get_connection(self, connection_id: str): ...


class LLMProviderRegistry:
    """Резолвит и собирает :class:`LLMProvider` по имени или из purpose.

    Args:
        settings_store: опциональный persistence-слой настроек. Если задан —
            :meth:`resolve_for_purpose` работает; без него — только :meth:`get`
            из env. UI-настройки (Stage 5+) всегда передают store.
    """

    def __init__(self, settings_store: _SettingsStoreProtocol | None = None) -> None:
        self._store = settings_store

    @property
    def supported_providers(self) -> tuple[str, ...]:
        return tuple(sorted(_ENV_BUILDERS.keys()))

    # --- Low-level: get by explicit provider name (legacy + tests) -----------

    def get(
        self,
        *,
        provider: str,
        model: str | None = None,
        complexity: str | None = None,
    ) -> LLMProvider:
        """Собрать провайдер по точному имени.

        Используется complexity_selector_service и в тестах. Для основного
        application-кода предпочитается :meth:`resolve_for_purpose`.
        """
        builder = _ENV_BUILDERS.get(provider)
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
        """Авто-резолв провайдера из env с возможностью override."""
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
        if os.environ.get("POV_OPENROUTER_API_KEY"):
            return "openrouter"
        return None

    # --- High-level: resolve by purpose (через store) ------------------------

    def resolve_for_purpose(
        self,
        purpose: str,
        *,
        complexity: str | None = None,
        override_model: str | None = None,
    ) -> LLMProvider:
        """Собрать провайдер для сценария ``purpose``.

        Алгоритм:

        1. Если задан ``override_model`` (например, через CLI ``--model X``),
           используем его — обходя assignment.
        2. Иначе берём ``ModelAssignment[purpose+complexity]``.
        3. Находим все enabled routings модели, отсортированные по priority desc.
        4. По первому рабочему routing берём connection и строим адаптер
           через ``from_connection``.
        5. Если все routings упали (ConflictError) — поднимаем последнюю
           ошибку (соответствует Q1: «fail loudly»; auto-fallback по разным
           моделям тут НЕ делается, остаётся на уровне settings приоритетов).

        Raises:
            ConflictError: если store не задан / нет назначения / нет routings /
                connection недоступен.
        """
        if self._store is None:
            # Backward-compat: пытаемся жить через env. Используется в тестах
            # complexity_selector_service до полного перехода на DI store.
            return self.from_env(override_model=override_model, complexity=complexity)

        # 1. Определяем имя модели.
        model_name = override_model
        if model_name is None:
            purpose_key = _resolve_purpose_key(purpose, complexity)
            assignment = self._store.get_assignment(purpose_key)
            if assignment is None:
                raise ConflictError(
                    f"Не назначена модель для сценария '{purpose_key}'. "
                    "Откройте Settings → Default Models и выберите модель."
                )
            model_name = assignment.model_name

        # 2. Routings.
        routings = list(self._store.list_routings_for_model(model_name))
        if not routings:
            raise ConflictError(
                f"У модели '{model_name}' нет рабочих маршрутов. "
                "Откройте Settings → Models и подключите хотя бы один провайдер для этой модели."
            )

        # 3. Перебор по приоритету.
        last_error: Exception | None = None
        for routing in routings:
            connection = self._store.get_connection(routing.connection_id)
            if connection is None:
                last_error = ConflictError(
                    f"Routing '{routing.routing_id}' ссылается на удалённый connection."
                )
                continue
            try:
                return self._build_from_connection(connection, model=model_name, complexity=complexity)
            except ConflictError as exc:
                last_error = exc
                continue

        # Если все routings упали — поднимаем последнюю ошибку.
        assert last_error is not None
        raise last_error

    def _build_from_connection(
        self,
        connection: ProviderConnection,
        *,
        model: str | None,
        complexity: str | None,
    ) -> LLMProvider:
        """Построить адаптер по connection. Switch по provider_type."""
        if connection.provider_type == "openrouter":
            return OpenRouterProvider.from_connection(connection, model=model)
        if connection.provider_type == "anthropic":
            return ClaudeSdkProvider.from_connection(connection, model=model, complexity=complexity)
        if connection.provider_type == "claude_cli":
            return ClaudeSubscriptionProvider.from_connection(connection, model=model, complexity=complexity)
        raise ConflictError(
            f"Неизвестный provider_type '{connection.provider_type}' "
            f"у connection '{connection.display_name}'."
        )


def _resolve_purpose_key(purpose: str, complexity: str | None) -> str:
    """Преобразовать ``("execution", "trivial")`` → ``"execution.trivial"``.

    Для purposes без complexity-вариантов (clarification_ce11 и пр.)
    возвращает purpose как есть.
    """
    if purpose == "execution":
        if complexity == "trivial":
            return PURPOSE_EXECUTION_TRIVIAL
        if complexity == "complex":
            return PURPOSE_EXECUTION_COMPLEX
        return PURPOSE_EXECUTION_STANDARD
    return purpose
