"""Структурный интерфейс LLM-провайдера.

PEP 544 Protocol с ``runtime_checkable``: любой объект с подходящей
сигнатурой ``chat_json`` автоматически считается ``LLMProvider``, без
обязательного наследования. Это удобно для тестов (мок-классы) и для
адаптации внешних SDK без обёрток над обёртками.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Структурный контракт «отправить промпт — получить JSON по схеме».

    Реализации обязаны:

    * Иметь атрибут ``name`` (str) — каноническое имя провайдера для
      логирования / отображения в UI / трассировки.
    * Иметь атрибут ``model`` (str | None) — текущая модель (для
      провайдеров, где модель определяет CLI/подписка, может быть None).
    * Реализовать ``chat_json(system_prompt, user_prompt, schema) -> dict``.

    Implementations НЕ должны:

    * Читать env-переменные. Конфигурация — задача
      :class:`LLMProviderRegistry` и его билдеров.
    * Делать switch по другим провайдерам. Один экземпляр — один провайдер.
    """

    name: str
    model: str | None

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
