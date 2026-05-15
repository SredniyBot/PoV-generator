"""Адаптер Anthropic Claude SDK под :class:`LLMProvider`."""

from __future__ import annotations

from typing import Any

from ...claude_sdk_client import ClaudeSdkClient, model_for_complexity


class ClaudeSdkProvider:
    """Тонкий адаптер ``ClaudeSdkClient`` под :class:`LLMProvider`."""

    name = "claude_sdk"

    def __init__(
        self,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> None:
        resolved_model = model or model_for_complexity(complexity)
        self.model = resolved_model
        # ClaudeSdkClient.from_env сам валидирует ключ и поднимает
        # ConflictError, если он не задан.
        self._client = ClaudeSdkClient.from_env(model=resolved_model)

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
