"""Адаптер OpenRouter под :class:`LLMProvider`."""

from __future__ import annotations

import os
from typing import Any

from ....common.errors import ConflictError
from ....domain.llm_settings import ProviderConnection
from ..protocol import LLMResult

# Плоский клиент импортируется лениво в __init__ — иначе цикл импорта
# flat-client → пакет ``.llm`` → ``registry`` → этот адаптер → flat-client
# (см. подробный комментарий в providers/claude_subscription.py).

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openai/gpt-4.1-mini"


class OpenRouterProvider:
    """Тонкий адаптер ``OpenRouterClient`` под :class:`LLMProvider`.

    Кредиты можно передать тремя способами:

    * Явно через :meth:`from_connection` (рекомендуется — Stage 2+).
    * Через env (``POV_OPENROUTER_API_KEY``) — legacy путь, используется
      :class:`LLMProviderRegistry` когда settings-store пуст.
    * Прямой конструктор с api_key + model — для тестов.
    """

    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        from ...openrouter_client import OpenRouterClient, OpenRouterConfig

        if not api_key:
            raise ConflictError("Не задан API key для провайдера openrouter.")
        self.model = model
        self._client = OpenRouterClient(
            OpenRouterConfig(api_key=api_key, model=model, base_url=base_url)
        )

    # --- Builders ------------------------------------------------------------

    @classmethod
    def from_env(cls, *, model: str | None = None) -> "OpenRouterProvider":
        api_key = os.environ.get("POV_OPENROUTER_API_KEY")
        if not api_key:
            raise ConflictError("Не задан POV_OPENROUTER_API_KEY для провайдера openrouter.")
        resolved_model = model or os.environ.get("POV_OPENROUTER_MODEL", _DEFAULT_MODEL)
        base_url = os.environ.get("POV_OPENROUTER_BASE_URL", _DEFAULT_BASE_URL)
        return cls(api_key=api_key, model=resolved_model, base_url=base_url)

    @classmethod
    def from_connection(
        cls,
        connection: ProviderConnection,
        *,
        model: str | None = None,
    ) -> "OpenRouterProvider":
        if connection.provider_type != "openrouter":
            raise ConflictError(
                f"OpenRouterProvider не может работать с connection типа '{connection.provider_type}'."
            )
        api_key = (connection.credentials.api_key or "").strip()
        if not api_key:
            raise ConflictError(
                f"У connection '{connection.display_name}' пустой API key — добавьте в Settings."
            )
        resolved_model = model or _DEFAULT_MODEL
        base_url = connection.extras.get("base_url", _DEFAULT_BASE_URL)
        return cls(api_key=api_key, model=resolved_model, base_url=base_url)

    # --- LLMProvider contract ------------------------------------------------

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
