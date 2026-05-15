"""Адаптер Claude через подписку (локальный CLI) под :class:`LLMProvider`."""

from __future__ import annotations

from typing import Any

from ...claude_subscription_client import ClaudeSubscriptionClient, model_for_complexity


class ClaudeSubscriptionProvider:
    """Тонкий адаптер ``ClaudeSubscriptionClient`` под :class:`LLMProvider`.

    Модель здесь — опциональна: CLI ``claude`` сам выбирает модель сессии,
    если override не задан.
    """

    name = "claude_subscription"

    def __init__(
        self,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> None:
        resolved_model = model or model_for_complexity(complexity)
        self.model = resolved_model
        # ClaudeSubscriptionClient.from_env допускает model=None и в этом
        # случае модель определит CLI/подписка.
        self._client = ClaudeSubscriptionClient.from_env(model=resolved_model)

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
