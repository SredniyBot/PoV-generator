from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib import error, request

from ..common.errors import ConflictError


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
        try:
            with request.urlopen(http_request, timeout=120) as response:
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
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConflictError(f"OpenRouter вернул невалидный JSON: {content}") from exc
