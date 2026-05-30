"""Адаптер Anthropic Claude SDK под :class:`LLMProvider`."""

from __future__ import annotations

import os
from typing import Any

from ....common.errors import ConflictError
from ....domain.llm_settings import ProviderConnection
from ...claude_sdk_client import ClaudeSdkClient, ClaudeSdkConfig, model_for_complexity
from ..protocol import LLMUsage


class ClaudeSdkProvider:
    """Тонкий адаптер ``ClaudeSdkClient`` под :class:`LLMProvider`.

    Поддерживает три способа конструирования (см. OpenRouterProvider): прямой
    конструктор / :meth:`from_env` / :meth:`from_connection`.
    """

    name = "claude_sdk"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int = 32768,
    ) -> None:
        if not api_key:
            raise ConflictError("Не задан API key для провайдера claude_sdk.")
        self.model = model
        self._client = ClaudeSdkClient(
            ClaudeSdkConfig(api_key=api_key, model=model, max_tokens=max_tokens)
        )
        # v3.5: token usage последнего вызова (см. LLMProvider protocol)
        self.last_usage: LLMUsage = LLMUsage.empty()

    @classmethod
    def from_env(
        cls,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> "ClaudeSdkProvider":
        api_key = os.environ.get("POV_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ConflictError(
                "Не задан POV_ANTHROPIC_API_KEY (или ANTHROPIC_API_KEY) для провайдера claude_sdk."
            )
        resolved_model = model or os.environ.get("POV_CLAUDE_MODEL") or model_for_complexity(complexity)
        max_tokens = int(os.environ.get("POV_CLAUDE_MAX_TOKENS", "32768"))
        return cls(api_key=api_key, model=resolved_model, max_tokens=max_tokens)

    @classmethod
    def from_connection(
        cls,
        connection: ProviderConnection,
        *,
        model: str | None = None,
        complexity: str | None = None,
    ) -> "ClaudeSdkProvider":
        if connection.provider_type != "anthropic":
            raise ConflictError(
                f"ClaudeSdkProvider требует connection типа 'anthropic', получен '{connection.provider_type}'."
            )
        api_key = (connection.credentials.api_key or "").strip()
        if not api_key:
            raise ConflictError(
                f"У connection '{connection.display_name}' пустой API key — добавьте в Settings."
            )
        resolved_model = model or model_for_complexity(complexity)
        max_tokens_raw = connection.extras.get("max_tokens", "32768")
        try:
            max_tokens = int(max_tokens_raw)
        except (TypeError, ValueError):
            max_tokens = 32768
        return cls(api_key=api_key, model=resolved_model, max_tokens=max_tokens)

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
        # v3.5: проброс usage из вложенного клиента
        self.last_usage = self._client.last_usage
        return result
