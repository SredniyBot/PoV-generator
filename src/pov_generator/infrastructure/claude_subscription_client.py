"""Клиент Claude через подписку Claude Code.

Использует библиотеку `claude-agent-sdk`, которая запускает локальный CLI
`claude` (он должен быть установлен и залогинен через `claude login`).
Авторизация происходит по сессии CLI, отдельный API-key не нужен.

Ограничение: structured output через tool-use здесь не используется
(это сложно реализовать через агентскую SDK). Вместо этого мы просим
модель вернуть строго JSON и затем извлекаем его из ответа.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
import re
from typing import Any

from ..common.errors import ConflictError


@dataclass(frozen=True)
class ClaudeSubscriptionConfig:
    model: str | None
    max_turns: int = 1


def model_for_complexity(complexity: str | None) -> str | None:
    """Маппинг сложности на модель. None означает «модель по умолчанию CLI/подписки»."""
    overrides = {
        "trivial": os.environ.get("POV_CLAUDE_MODEL_TRIVIAL"),
        "standard": os.environ.get("POV_CLAUDE_MODEL_STANDARD"),
        "complex": os.environ.get("POV_CLAUDE_MODEL_COMPLEX"),
    }
    return overrides.get(complexity or "standard")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ClaudeSubscriptionClient:
    """Тонкая обёртка над claude-agent-sdk."""

    def __init__(self, config: ClaudeSubscriptionConfig) -> None:
        self._config = config
        try:
            import claude_agent_sdk  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ConflictError(
                "Не установлен пакет 'claude-agent-sdk'. Поставьте его (pip install -e .[dev]) "
                "и убедитесь, что CLI 'claude' установлен и выполнен 'claude login'."
            ) from exc
        self._sdk = claude_agent_sdk

    @classmethod
    def from_env(cls, *, model: str | None = None) -> "ClaudeSubscriptionClient":
        active_model = model or os.environ.get("POV_CLAUDE_MODEL") or None
        max_turns_raw = os.environ.get("POV_CLAUDE_MAX_TURNS", "1")
        try:
            max_turns = int(max_turns_raw)
        except ValueError as exc:
            raise ConflictError(
                f"POV_CLAUDE_MAX_TURNS должно быть целым числом, получено: {max_turns_raw}"
            ) from exc
        return cls(ClaudeSubscriptionConfig(model=active_model, max_turns=max_turns))

    @property
    def model(self) -> str:
        return self._config.model or "claude-code-default"

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        full_prompt = (
            user_prompt
            + "\n\n---\n"
            + "Верни ответ строго в виде одного JSON-объекта, соответствующего этой JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
            + "\n\nНе добавляй пояснений вне JSON. Не используй markdown-обёртку."
        )
        text = asyncio.run(self._collect(system_prompt, full_prompt))
        return self._extract_json(text)

    async def _collect(self, system_prompt: str, user_prompt: str) -> str:
        # ClaudeAgentOptions может не иметь поля `model` в старых версиях SDK.
        options_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "max_turns": self._config.max_turns,
            "permission_mode": "bypassPermissions",
        }
        if self._config.model:
            options_kwargs["model"] = self._config.model
        try:
            options = self._sdk.ClaudeAgentOptions(**options_kwargs)
        except TypeError:
            # Если model не поддерживается, повторим без него.
            options_kwargs.pop("model", None)
            options = self._sdk.ClaudeAgentOptions(**options_kwargs)

        chunks: list[str] = []
        try:
            async for message in self._sdk.query(prompt=user_prompt, options=options):
                content = getattr(message, "content", None)
                if not content:
                    continue
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
        except Exception as exc:  # pragma: no cover
            raise ConflictError(f"Ошибка при обращении к Claude через подписку: {exc}") from exc
        return "".join(chunks)

    def _extract_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ConflictError("Claude вернул пустой ответ.")
        match = _JSON_FENCE_RE.search(text)
        candidate: str | None = None
        if match:
            candidate = match.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                candidate = text[start : end + 1]
        if candidate is None:
            raise ConflictError(f"Не удалось извлечь JSON из ответа: {text!r}")
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ConflictError(f"Невалидный JSON в ответе Claude: {candidate!r}") from exc
        if not isinstance(payload, dict):
            raise ConflictError(f"Ожидался JSON-объект, получено: {payload!r}")
        return payload
