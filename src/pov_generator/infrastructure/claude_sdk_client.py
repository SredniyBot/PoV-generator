from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..common.errors import ConflictError


@dataclass(frozen=True)
class ClaudeSdkConfig:
    api_key: str
    model: str
    max_tokens: int = 32768
    temperature: float = 0.2


def model_for_complexity(complexity: str | None) -> str:
    """Маппинг сложности задачи на модель Claude. Можно переопределять через env."""
    overrides = {
        "trivial": os.environ.get("POV_CLAUDE_MODEL_TRIVIAL", "claude-haiku-4-5-20251001"),
        "standard": os.environ.get("POV_CLAUDE_MODEL_STANDARD", "claude-sonnet-4-6"),
        # Opus 4.7 — текущий флагман на сложных задачах синтеза (финальное ТЗ,
        # сложные analysis-задачи в complex-режиме).
        "complex": os.environ.get("POV_CLAUDE_MODEL_COMPLEX", "claude-opus-4-7"),
    }
    return overrides.get(complexity or "standard", overrides["standard"])


class ClaudeSdkClient:
    """Тонкая обёртка над Anthropic Python SDK с tool-use для structured output."""

    def __init__(self, config: ClaudeSdkConfig) -> None:
        self._config = config
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ConflictError(
                "Не установлен пакет 'anthropic'. Добавьте его в зависимости проекта (.[dev] переустановит)."
            ) from exc
        # 1 час HTTP-таймаут — opus на complex думает 5-15 мин; 60s
        # дефолт Anthropic SDK слишком жёсткий для финального synthesis.
        # Override через POV_ANTHROPIC_TIMEOUT_SEC.
        timeout_sec = int(os.environ.get("POV_ANTHROPIC_TIMEOUT_SEC", "3600"))
        self._client = anthropic.Anthropic(api_key=config.api_key, timeout=float(timeout_sec))

    @classmethod
    def from_env(cls, *, model: str | None = None) -> "ClaudeSdkClient":
        api_key = os.environ.get("POV_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ConflictError(
                "Не задан POV_ANTHROPIC_API_KEY (или ANTHROPIC_API_KEY) для провайдера claude_sdk."
            )
        active_model = model or os.environ.get("POV_CLAUDE_MODEL", "claude-sonnet-4-6")
        max_tokens_raw = os.environ.get("POV_CLAUDE_MAX_TOKENS", "32768")
        try:
            max_tokens = int(max_tokens_raw)
        except ValueError as exc:
            raise ConflictError(f"POV_CLAUDE_MAX_TOKENS должно быть числом, получено: {max_tokens_raw}") from exc
        return cls(ClaudeSdkConfig(api_key=api_key, model=active_model, max_tokens=max_tokens))

    @property
    def model(self) -> str:
        return self._config.model

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        tool_name: str = "produce_artifact",
        tool_description: str = "Produce the leaf-task output following the strict schema.",
    ) -> dict[str, Any]:
        """Запрашивает у Claude структурированный JSON через tool use."""
        try:
            response = self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                system=system_prompt,
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description,
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:  # pragma: no cover
            raise ConflictError(f"Ошибка запроса к Claude SDK: {exc}") from exc

        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use":
                payload = getattr(block, "input", None)
                if not isinstance(payload, dict):
                    raise ConflictError(f"Claude вернул tool_use без dict-ввода: {payload!r}")
                return payload
        raise ConflictError(f"Claude не вернул tool_use блок. Ответ: {response.content!r}")
