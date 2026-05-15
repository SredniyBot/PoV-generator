"""Адаптер OpenRouter под :class:`LLMProvider`."""

from __future__ import annotations

import os
from typing import Any

from ....common.errors import ConflictError
from ...openrouter_client import OpenRouterClient, OpenRouterConfig


class OpenRouterProvider:
    """Тонкий адаптер ``OpenRouterClient`` под :class:`LLMProvider`."""

    name = "openrouter"

    def __init__(self, *, model: str | None = None) -> None:
        api_key = os.environ.get("POV_OPENROUTER_API_KEY")
        if not api_key:
            raise ConflictError("Не задан POV_OPENROUTER_API_KEY для провайдера openrouter.")
        resolved_model = model or os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        base_url = os.environ.get("POV_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = resolved_model
        self._client = OpenRouterClient(
            OpenRouterConfig(api_key=api_key, model=resolved_model, base_url=base_url)
        )

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
