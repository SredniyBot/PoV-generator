from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from ..common.errors import ConflictError
from ..common.logging import get_logger
from .llm.protocol import LLMResult, LLMUsage
from .llm.structured_output import strip_nulls, to_strict_schema

logger = get_logger("llm")

# ```json ... ``` обёртка вокруг ответа: появляется только в режиме без
# response_format (модель сама решает форматирование) — срезаем терпимо.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


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

    # --- режимы structured output -------------------------------------------

    @staticmethod
    def _response_format_attempts(schema: dict[str, object]) -> list[dict[str, object] | None]:
        """Цепочка режимов соблюдения схемы — от сильного к слабому.

        1. ``json_schema (strict)`` — настоящий constrained decoding. Требует
           strict-подмножества схемы → :func:`to_strict_schema` (lossless).
           Для «unstructured» контрактов (``additionalProperties: true``)
           подмножество не существует — шаг пропускается.
        2. ``json_object`` — гарантирован только валидный JSON, без схемы.
        3. ``None`` — без response_format: схема живёт в промпте (инструкция
           добавляется в chat_json), парсинг терпим к ```json``` обёрткам.

        Деградация по цепочке происходит ТОЛЬКО на HTTP 400 (модель/провайдер
        не поддерживает параметр); сетевые и auth-ошибки поднимаются сразу.
        """
        attempts: list[dict[str, object] | None] = []
        strict_schema = to_strict_schema(schema)  # type: ignore[arg-type]
        if strict_schema is not None:
            attempts.append(
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "artifact_output",
                        "strict": True,
                        "schema": strict_schema,
                    },
                }
            )
        attempts.append({"type": "json_object"})
        attempts.append(None)
        return attempts

    def chat_json(self, *, system_prompt: str, user_prompt: str, schema: dict[str, object]) -> LLMResult:
        # Некоторые провайдеры (Alibaba/Qwen через OpenRouter) при заданном
        # response_format требуют, чтобы слово "json" встречалось в сообщениях,
        # иначе возвращают HTTP 400 (InvalidParameter). Гарантируем это
        # безвредной инструкцией — для остальных моделей она ничего не меняет.
        if "json" not in f"{system_prompt}\n{user_prompt}".lower():
            system_prompt = (
                f"{system_prompt.rstrip()}\n\nВерни ответ одним валидным JSON-объектом по схеме."
            ).strip()

        attempts = self._response_format_attempts(schema)
        last_error: ConflictError | None = None
        for index, response_format in enumerate(attempts):
            try:
                return self._request_once(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_format=response_format,
                )
            except ConflictError as exc:
                # Деградируем только на отказ параметра (HTTP 400) и только
                # если остался более слабый режим.
                if "HTTP 400" not in str(exc) or index == len(attempts) - 1:
                    raise
                last_error = exc
                next_mode = attempts[index + 1]
                logger.warning(
                    "openrouter: модель отвергла response_format — деградация",
                    model=self._config.model,
                    from_mode=(response_format or {}).get("type", "none"),
                    to_mode=(next_mode or {}).get("type", "none"),
                )
        assert last_error is not None  # цепочка непуста; сюда не доходим
        raise last_error

    def _request_once(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_format: dict[str, object] | None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "plugins": [{"id": "response-healing"}],
        }
        if response_format is not None:
            payload["response_format"] = response_format
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
        # В режиме без response_format модель может обернуть JSON в ```json```.
        fence = _FENCE_RE.match(content.strip())
        if fence:
            content = fence.group(1)
        try:
            parsed_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConflictError(f"OpenRouter вернул невалидный JSON: {content}") from exc
        # null в strict-режиме означает «поле не заполнено» (см. structured_output)
        # — приводим к канонической форме «ключ отсутствует».
        return LLMResult(
            payload=strip_nulls(parsed_payload),
            usage=_usage_from_openrouter(parsed.get("usage")),
        )
