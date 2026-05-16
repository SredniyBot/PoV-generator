"""Адаптер Claude через подписку (локальный CLI) под :class:`LLMProvider`."""

from __future__ import annotations

from typing import Any

from ....common.errors import ConflictError
from ....domain.llm_settings import ProviderConnection
from ...claude_subscription_client import (
    ClaudeSubscriptionClient,
    ClaudeSubscriptionConfig,
    model_for_complexity,
)


class ClaudeSubscriptionProvider:
    """Тонкий адаптер ``ClaudeSubscriptionClient`` под :class:`LLMProvider`.

    Модель здесь — опциональна: CLI ``claude`` сам выбирает модель сессии,
    если override не задан. API-key не нужен — авторизация через ``claude login``.
    """

    name = "claude_subscription"

    def __init__(
        self,
        *,
        model: str | None = None,
        max_turns: int = 1,
    ) -> None:
        self.model = model
        # cli_path / load_timeout_ms резолвятся внутри
        # ``ClaudeSubscriptionClient.__init__`` через _resolve_* helpers —
        # см. docstring claude_subscription_client.py.
        self._client = ClaudeSubscriptionClient(
            ClaudeSubscriptionConfig(model=model, max_turns=max_turns)
        )

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> "ClaudeSubscriptionProvider":
        resolved_model = model or model_for_complexity(complexity)
        # ClaudeSubscriptionClient.from_env читает POV_CLAUDE_MAX_TURNS и пр.,
        # но конструктор-логику оставим единой через прямой конструктор.
        import os as _os

        max_turns_raw = _os.environ.get("POV_CLAUDE_MAX_TURNS", "1")
        try:
            max_turns = int(max_turns_raw)
        except (TypeError, ValueError):
            max_turns = 1
        return cls(model=resolved_model, max_turns=max_turns)

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
        resolved_model = model or model_for_complexity(complexity)
        max_turns_raw = connection.extras.get("max_turns", "1")
        try:
            max_turns = int(max_turns_raw)
        except (TypeError, ValueError):
            max_turns = 1
        return cls(model=resolved_model, max_turns=max_turns)

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        return self._client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
