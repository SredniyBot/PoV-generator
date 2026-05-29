"""Структурный интерфейс LLM-провайдера.

PEP 544 Protocol с ``runtime_checkable``: любой объект с подходящей
сигнатурой ``chat_json`` автоматически считается ``LLMProvider``, без
обязательного наследования. Это удобно для тестов (мок-классы) и для
адаптации внешних SDK без обёрток над обёртками.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMUsage:
    """Учёт токенов одного chat_json-вызова (v3.5).

    Поля нормализованы под общий API:
    - ``input_tokens``: вход (system + user prompts + tool/schema overhead).
    - ``output_tokens``: ответ модели.
    - ``cache_read_tokens`` / ``cache_write_tokens``: для провайдеров с
      prompt cache (Claude). 0 если провайдер не сообщает.
    - ``total_tokens``: сумма input+output (без удвоения по кэшу).
    - ``provider`` / ``model``: для traceability в UI/логах.

    Сделано frozen — usage иммутабелен, агрегаты считаются вручную.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    provider: str = ""
    model: str = ""

    @classmethod
    def empty(cls) -> "LLMUsage":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "provider": self.provider,
            "model": self.model,
        }


@runtime_checkable
class LLMProvider(Protocol):
    """Структурный контракт «отправить промпт — получить JSON по схеме».

    Реализации обязаны:

    * Иметь атрибут ``name`` (str) — каноническое имя провайдера для
      логирования / отображения в UI / трассировки.
    * Иметь атрибут ``model`` (str | None) — текущая модель (для
      провайдеров, где модель определяет CLI/подписка, может быть None).
    * Реализовать ``chat_json(system_prompt, user_prompt, schema) -> dict``.
    * v3.5: после каждого ``chat_json`` обновлять атрибут ``last_usage``
      (см. :class:`LLMUsage`). Если данные о usage недоступны (провайдер
      не возвращает) — ``LLMUsage.empty()``.

    Implementations НЕ должны:

    * Читать env-переменные. Конфигурация — задача
      :class:`LLMProviderRegistry` и его билдеров.
    * Делать switch по другим провайдерам. Один экземпляр — один провайдер.
    """

    name: str
    model: str | None
    # v3.5: последний usage от провайдера. После chat_json — данные текущего
    # вызова; до первого вызова — LLMUsage.empty().
    last_usage: LLMUsage

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Отправить промпт и получить структурированный JSON-ответ.

        Args:
            system_prompt: системный промпт (роль, ограничения, стиль).
            user_prompt: пользовательский промпт (контекст и задача).
            schema: JSON-schema ожидаемого ответа. Провайдер сам решает,
                как добиться структурированного вывода (response_format,
                tool-use, json mode и т.п.).

        Returns:
            Распарсенный dict, соответствующий ``schema``. Конкретный
            формат не нормализуется — он определяется задачей.

        Raises:
            ConflictError: если ответ не валиден / провайдер не настроен /
                сетевая/SDK-ошибка.
        """
        ...
