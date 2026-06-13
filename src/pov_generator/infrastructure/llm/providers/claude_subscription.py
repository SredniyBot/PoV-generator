"""Адаптер Claude-подписки под :class:`LLMProvider` — COMPLETION-роль.

Это «ответь JSON/текстом» поверх локального ``claude`` CLI. Агентская
(многоходовая, с инструментами) роль того же CLI — отдельный harness-провайдер
``infrastructure/harness/providers/claude_code.py``; здесь инструменты всегда
выключены (см. ``ClaudeSubscriptionClient._collect``)."""

from __future__ import annotations

from typing import Any

from ....common.errors import ConflictError
from ....domain.llm_settings import ProviderConnection
from ..protocol import LLMResult

# Плоский клиент импортируется ЛЕНИВО внутри методов (не на уровне модуля),
# чтобы разорвать цикл импорта: flat-client → пакет ``.llm`` (через сабмодуль
# ``.llm.protocol``) → ``registry`` → этот адаптер → flat-client. При ленивом
# импорте к моменту конструирования адаптера пакет ``.llm`` уже инициализирован.


class ClaudeSubscriptionProvider:
    """Тонкий адаптер ``ClaudeSubscriptionClient`` под :class:`LLMProvider`.

    Модель здесь — опциональна: CLI ``claude`` сам выбирает модель сессии,
    если override не задан. API-key не нужен — авторизация через ``claude login``.
    """

    name = "claude_subscription"
    # Лимит подписки — 5-часовое ОКНО, считающее ОБЪЁМ токенов (включая
    # cache-read). Prompt-caching удешевляет деньги, но не окно: N фрагментов ×
    # префикс выжигают окно. Поэтому structured-вывод для подписки идёт ОДНИМ
    # плоским проходом (+ нормализация/self-repair), а compositional-сборка по
    # частям — лишь реактивная крайняя мера. Флаг читают обёртки/execution_service
    # (см. common/llm_modes.py).
    token_window_limited = True

    def __init__(
        self,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        effort: str | None = None,
    ) -> None:
        from ...claude_subscription_client import ClaudeSubscriptionClient, ClaudeSubscriptionConfig

        self.model = model
        # cli_path / load_timeout_ms резолвятся внутри
        # ``ClaudeSubscriptionClient.__init__`` через _resolve_* helpers —
        # см. docstring claude_subscription_client.py.
        # max_turns=None → НЕ передаём поле, чтобы применился дефолт датакласса
        # (_COMPLETION_MAX_TURNS). Источник правды по дефолту — один, в клиенте.
        config_kwargs: dict[str, Any] = {"model": model}
        if max_turns is not None:
            config_kwargs["max_turns"] = max_turns
        # effort (глубина рассуждения) обычно задаётся из уровня сложности задачи
        # (from_env/from_connection → effort_for_complexity). None → клиент
        # возьмёт дефолт уровня standard в момент вызова.
        if effort is not None:
            config_kwargs["effort"] = effort
        self._client = ClaudeSubscriptionClient(ClaudeSubscriptionConfig(**config_kwargs))

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> "ClaudeSubscriptionProvider":
        from ...claude_subscription_client import (
            effort_for_complexity,
            model_for_complexity,
        )

        resolved_model = model or model_for_complexity(complexity)
        # ClaudeSubscriptionClient.from_env читает POV_CLAUDE_MAX_TURNS и пр.,
        # но конструктор-логику оставим единой через прямой конструктор.
        import os as _os

        # Не задано → None: применится единый дефолт completion-роли
        # (_COMPLETION_MAX_TURNS, дефолт датакласса конфига).
        max_turns_raw = _os.environ.get("POV_CLAUDE_MAX_TURNS")
        max_turns: int | None
        try:
            max_turns = int(max_turns_raw) if max_turns_raw is not None else None
        except (TypeError, ValueError):
            max_turns = None
        return cls(
            model=resolved_model,
            max_turns=max_turns,
            effort=effort_for_complexity(complexity),
        )

    @classmethod
    def from_connection(
        cls,
        connection: ProviderConnection,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> "ClaudeSubscriptionProvider":
        if connection.provider_type != "claude_cli":
            raise ConflictError(
                f"ClaudeSubscriptionProvider требует connection типа 'claude_cli', "
                f"получен '{connection.provider_type}'."
            )
        from ...claude_subscription_client import (
            effort_for_complexity,
            model_for_complexity,
        )

        resolved_model = model or model_for_complexity(complexity)
        # Не задано → None: применится единый дефолт completion-роли
        # (_COMPLETION_MAX_TURNS, дефолт датакласса конфига).
        max_turns_raw = connection.extras.get("max_turns")
        max_turns: int | None
        try:
            max_turns = int(max_turns_raw) if max_turns_raw is not None else None
        except (TypeError, ValueError):
            max_turns = None
        # Глубина рассуждения (effort) из уровня сложности.
        return cls(
            model=resolved_model,
            max_turns=max_turns,
            effort=effort_for_complexity(complexity),
        )

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> LLMResult:
        return self._client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
