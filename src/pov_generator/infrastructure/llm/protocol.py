"""Структурный интерфейс LLM-провайдера.

PEP 544 Protocol с ``runtime_checkable``: любой объект с подходящей
сигнатурой ``chat_json`` автоматически считается ``LLMProvider``, без
обязательного наследования. Это удобно для тестов (мок-классы) и для
адаптации внешних SDK без обёрток над обёртками.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

UsageSource = Literal["actual", "estimated"]
"""Источник чисел usage: ``actual`` — провайдер реально вернул; ``estimated`` —
оценка длины (fallback). Где провайдер не дал данных вовсе — usage = None (n/a),
выдуманные числа не подставляем."""


def estimate_token_count(text: str) -> int:
    """Грубая оценка токенов по длине (≈4 символа на токен).

    Тот же принцип, что у ``ContextBudget`` (см. ``context_service``):
    используется как fallback, когда провайдер не возвращает фактический usage.
    """
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class LLMUsage:
    """Нормализованный расход токенов на один LLM-вызов."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    source: UsageSource
    cache_tokens: int | None = None
    cost_usd: float | None = None
    # Токены «расширенного мышления» (extended thinking). У Claude thinking
    # ВХОДИТ в output_tokens — это его подмножество, а не отдельная статья.
    # Выделяем явно, чтобы было видно накладные расходы на размышление (именно
    # они, а не сам ответ, определяют время генерации). None = провайдер не
    # умеет/не различает thinking.
    reasoning_tokens: int | None = None

    @classmethod
    def estimated(cls, *, input_text: str, output_text: str) -> "LLMUsage":
        """Оценка usage по длине промпта/ответа (source=estimated)."""
        input_tokens = estimate_token_count(input_text)
        output_tokens = estimate_token_count(output_text)
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            source="estimated",
        )


@dataclass(frozen=True)
class LLMResult:
    """Результат одного ``chat_json``: распарсенный payload + usage.

    ``usage = None`` означает «провайдер не дал данных» (n/a) — UI показывает
    «n/a», а не выдуманные числа.
    """

    payload: dict[str, Any]
    usage: LLMUsage | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Структурный контракт «отправить промпт — получить JSON по схеме».

    Реализации обязаны:

    * Иметь атрибут ``name`` (str) — каноническое имя провайдера для
      логирования / отображения в UI / трассировки.
    * Иметь атрибут ``model`` (str | None) — текущая модель (для
      провайдеров, где модель определяет CLI/подписка, может быть None).
    * Реализовать ``chat_json(system_prompt, user_prompt, schema) -> LLMResult``
      (``payload`` по схеме + ``usage`` — расход токенов; см. :class:`LLMResult`).

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
    ) -> LLMResult:
        """Отправить промпт и получить структурированный JSON-ответ + usage.

        Args:
            system_prompt: системный промпт (роль, ограничения, стиль).
            user_prompt: пользовательский промпт (контекст и задача).
            schema: JSON-schema ожидаемого ответа. Провайдер сам решает,
                как добиться структурированного вывода (response_format,
                tool-use, json mode и т.п.).

        Returns:
            :class:`LLMResult` — распарсенный ``payload`` (dict по ``schema``) +
            ``usage`` (расход токенов; None если провайдер не дал данных).

        Raises:
            ConflictError: если ответ не валиден / провайдер не настроен /
                сетевая/SDK-ошибка.
        """
        ...
