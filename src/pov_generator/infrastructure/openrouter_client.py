from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib import error, request

from ..common.errors import ConflictError
from .llm.protocol import LLMResult, LLMUsage


def _usage_from_openrouter(usage: object) -> LLMUsage | None:
    """Нормализует поле ``usage`` ответа OpenRouter в :class:`LLMUsage`.

    OpenRouter возвращает ``{prompt_tokens, completion_tokens, total_tokens}``.
    Если поля нет — None (n/a), без выдуманных чисел.
    """
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
    cost = usage.get("cost")
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        source="actual",
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    model: str
    base_url: str = "https://openrouter.ai/api/v1"
    app_name: str = "pov-generator"


class OpenRouterClient:
    def __init__(self, config: OpenRouterConfig) -> None:
        self._config = config

    @classmethod
    def from_env(cls) -> "OpenRouterClient":
        api_key = os.environ.get("POV_OPENROUTER_API_KEY")
        if not api_key:
            raise ConflictError("Не задан POV_OPENROUTER_API_KEY.")
        model = os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        base_url = os.environ.get("POV_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        return cls(OpenRouterConfig(api_key=api_key, model=model, base_url=base_url))

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> LLMResult:
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "artifact_output",
                    "strict": True,
                    "schema": schema,
                },
            },
            "plugins": [{"id": "response-healing"}],
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://local.pov-generator",
            "X-Title": self._config.app_name,
        }
        http_request = request.Request(
            url=f"{self._config.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        # 1 час на ответ модели. На complex-задачах opus может думать
        # 5-15 мин; 120s было слишком жёстко и валило финальный synthesis.
        # Override через POV_OPENROUTER_TIMEOUT_SEC если нужно меньше/больше.
        timeout_sec = int(os.environ.get("POV_OPENROUTER_TIMEOUT_SEC", "3600"))
        try:
            with request.urlopen(http_request, timeout=timeout_sec) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")
            raise ConflictError(f"OpenRouter HTTP {exc.code}: {raw_error}") from exc
        except error.URLError as exc:
            raise ConflictError(f"Ошибка соединения с OpenRouter: {exc}") from exc

        parsed = json.loads(raw)
        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise ConflictError(f"Неожиданный ответ OpenRouter: {parsed}") from exc
        if not isinstance(content, str):
            raise ConflictError(f"OpenRouter вернул неожиданный content: {content!r}")
        try:
            parsed_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConflictError(f"OpenRouter вернул невалидный JSON: {content}") from exc
        return LLMResult(payload=parsed_payload, usage=_usage_from_openrouter(parsed.get("usage")))
