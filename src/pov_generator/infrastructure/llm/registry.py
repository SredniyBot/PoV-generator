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
import time
from dataclasses import dataclass
from typing import Any, Callable

from ...common.errors import ConflictError
from ...common.logging import get_logger
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

_llm_logger = get_logger("llm")


class LoggingLLMProvider:
    """Декоратор :class:`LLMProvider`: единая точка наблюдения за LLM-трафиком.

    Через registry проходят ВСЕ провайдеры, а значит — все реальные вызовы
    chat_json (execution, decision_identification/extraction, complexity,
    domain_pack_selection). Логируем каждый: provider/model/purpose/токены/
    длительность (INFO) или ошибку (ERROR). Проксирует контракт Protocol
    (name/model/last_usage) на обёрнутый провайдер.
    """

    def __init__(self, inner: LLMProvider, *, purpose: str | None = None) -> None:
        self._inner = inner
        self._purpose = purpose

    @property
    def inner(self) -> LLMProvider:
        """Обёрнутый провайдер — для интроспекции (тесты, отладка)."""
        return self._inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str | None:
        return self._inner.model

    @property
    def last_usage(self):  # type: ignore[no-untyped-def]
        return self._inner.last_usage

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            result = self._inner.chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema
            )
        except BaseException as exc:  # noqa: BLE001 — логируем и пробрасываем
            dur = round((time.perf_counter() - start) * 1000)
            _llm_logger.error(
                "LLM-вызов: ошибка",
                purpose=self._purpose,
                provider=self._inner.name,
                model=self._inner.model,
                error=str(exc).strip() or type(exc).__name__,
                duration_ms=dur,
                exc_info=False,
            )
            raise
        dur = round((time.perf_counter() - start) * 1000)
        usage = getattr(self._inner, "last_usage", None)
        _llm_logger.info(
            "LLM-вызов",
            purpose=self._purpose,
            provider=self._inner.name,
            model=self._inner.model,
            in_tokens=getattr(usage, "input_tokens", None) or None,
            out_tokens=getattr(usage, "output_tokens", None) or None,
            total_tokens=getattr(usage, "total_tokens", None) or None,
            duration_ms=dur,
        )
        return result


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
        purpose: str | None = None,
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
        return LoggingLLMProvider(builder(model, complexity), purpose=purpose)

    def from_env(
        self,
        *,
        override_provider: str | None = None,
        override_model: str | None = None,
        env_provider_var: str = "POV_EXECUTION_PROVIDER",
        env_model_var: str | None = None,
        complexity: str | None = None,
        purpose: str | None = None,
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
        return self.get(
            provider=provider_name, model=model_name, complexity=complexity, purpose=purpose
        )

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
            return self.from_env(
                override_model=override_model, complexity=complexity, purpose=purpose
            )

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
                return self._build_from_connection(
                    connection, model=model_name, complexity=complexity, purpose=purpose
                )
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
        purpose: str | None = None,
    ) -> LLMProvider:
        """Построить адаптер по connection. Switch по provider_type."""
        if connection.provider_type == "openrouter":
            inner: LLMProvider = OpenRouterProvider.from_connection(connection, model=model)
        elif connection.provider_type == "anthropic":
            inner = ClaudeSdkProvider.from_connection(connection, model=model, complexity=complexity)
        elif connection.provider_type == "claude_cli":
            inner = ClaudeSubscriptionProvider.from_connection(
                connection, model=model, complexity=complexity
            )
        else:
            raise ConflictError(
                f"Неизвестный provider_type '{connection.provider_type}' "
                f"у connection '{connection.display_name}'."
            )
        return LoggingLLMProvider(inner, purpose=purpose)


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
