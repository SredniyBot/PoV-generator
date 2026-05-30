from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib import error, request

from ..common.errors import ConflictError
from .llm.protocol import LLMUsage


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    model: str
    base_url: str = "https://openrouter.ai/api/v1"
    app_name: str = "pov-generator"


class OpenRouterClient:
    def __init__(self, config: OpenRouterConfig) -> None:
        self._config = config
        # v3.5: usage последнего вызова — обновляется в chat_json.
        self.last_usage: LLMUsage = LLMUsage.empty()

    @classmethod
    def from_env(cls) -> "OpenRouterClient":
        api_key = os.environ.get("POV_OPENROUTER_API_KEY")
        if not api_key:
            raise ConflictError("Не задан POV_OPENROUTER_API_KEY.")
        model = os.environ.get("POV_OPENROUTER_MODEL", "openai/gpt-4.1-mini")
        base_url = os.environ.get("POV_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        return cls(OpenRouterConfig(api_key=api_key, model=model, base_url=base_url))

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> dict[str, object]:
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
        # v3.5: usage — OpenRouter возвращает {"usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}.
        usage_raw = parsed.get("usage") or {}
        try:
            input_tok = int(usage_raw.get("prompt_tokens") or 0)
            output_tok = int(usage_raw.get("completion_tokens") or 0)
            total_tok = int(usage_raw.get("total_tokens") or (input_tok + output_tok))
        except (TypeError, ValueError):
            input_tok = output_tok = total_tok = 0
        self.last_usage = LLMUsage(
            input_tokens=input_tok,
            output_tokens=output_tok,
            total_tokens=total_tok,
            provider="openrouter",
            model=self._config.model,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConflictError(f"OpenRouter вернул невалидный JSON: {content}") from exc
