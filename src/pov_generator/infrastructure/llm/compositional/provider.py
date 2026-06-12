"""Декоратор :class:`LLMProvider`, собирающий сложные структуры по частям.

Прозрачен для вызывающих (тот же контракт ``chat_json`` → ``LLMResult``):
оборачивает любой провайдер и для каждого запроса решает,

* **проактивно** (схема сложная по :func:`should_decompose`) — сразу собирать по
  частям, не тратя заведомо провальный strict-проход;
* **реактивно** (один проход не уложился в схему) — вместо «съезда» на сырой
  ответ собрать по частям, СОХРАНЯЯ контракт.

Простые схемы идут одним проходом, как раньше — накладных расходов нет.

Размещение в цепочке: оборачивает уже залогированный провайдер
(``CompositionalLLMProvider(LoggingLLMProvider(real))``), поэтому каждый
фрагмент виден в логах как отдельный вызов, а наружу возвращается ОДИН
``LLMResult`` с агрегированным usage — вызывающий учитывает токены как обычно.
"""

from __future__ import annotations

from typing import Any

from ....common.llm_modes import plain_json_preferred
from ....common.logging import get_logger
from ..protocol import LLMProvider, LLMResult, LLMUsage
from .assembler import StructuredAssembler
from .complexity import should_decompose
from .decomposer import DecompositionStrategy, SchemaTreeDecomposer
from .prompt_builder import PartPromptBuilder
from .schema_utils import is_object_schema
from .validation import matches_schema

_logger = get_logger("llm")


class CompositionalLLMProvider:
    """Прозрачная обёртка: один проход для простых схем, сборка для сложных."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        decomposer: DecompositionStrategy | None = None,
        prompt_builder: PartPromptBuilder | None = None,
    ) -> None:
        self._inner = inner
        self._assembler = StructuredAssembler(
            inner,
            decomposer=decomposer or SchemaTreeDecomposer(),
            prompt_builder=prompt_builder,
        )

    @property
    def inner(self) -> LLMProvider:
        """Обёрнутый провайдер — для интроспекции (тесты, отладка)."""
        return self._inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str | None:
        return self._inner.model

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> LLMResult:
        # Plain-режим (ambient): вызывающий просит один проход без декомпозиции
        # (форму добьёт нормализация). Композиция применима только к объектным
        # схемам. В обоих случаях — делегируем как есть.
        if plain_json_preferred() or not is_object_schema(schema):
            return self._inner.chat_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema
            )

        if should_decompose(schema):
            _logger.info("compositional: сложная схема → сборка по частям", mode="proactive")
            return self._assemble(system_prompt, user_prompt, schema)

        result = self._inner.chat_json(
            system_prompt=system_prompt, user_prompt=user_prompt, schema=schema
        )
        if matches_schema(result.payload, schema):
            return result
        _logger.info(
            "compositional: один проход не уложился в схему → сборка по частям", mode="reactive"
        )
        return self._assemble(system_prompt, user_prompt, schema)

    def _assemble(self, system_prompt: str, user_prompt: str, schema: dict[str, Any]) -> LLMResult:
        value, usages = self._assembler.assemble(
            schema, base_system=system_prompt, base_user=user_prompt
        )
        payload = value if isinstance(value, dict) else {}
        return LLMResult(payload=payload, usage=_aggregate_usage(usages))


def _aggregate_usage(usages: list[LLMUsage | None]) -> LLMUsage | None:
    """Свернуть usage всех фрагментов в один (как будто один вызов).

    None, если ни один фрагмент не дал фактических данных."""
    real = [u for u in usages if u is not None]
    if not real:
        return None
    inp = sum(u.input_tokens for u in real)
    out = sum(u.output_tokens for u in real)
    cache = sum(u.cache_tokens or 0 for u in real)
    reasoning = sum(u.reasoning_tokens or 0 for u in real)
    cost = sum(u.cost_usd or 0.0 for u in real)
    return LLMUsage(
        input_tokens=inp,
        output_tokens=out,
        total_tokens=inp + out,
        source="actual" if any(u.source == "actual" for u in real) else "estimated",
        cache_tokens=cache or None,
        cost_usd=cost or None,
        reasoning_tokens=reasoning or None,
        # Сборка по частям: запусков/повторов суммируем по всем фрагментам —
        # наружу отдаём как один вызов с полным числом подзадач.
        call_count=sum(u.call_count for u in real),
        retry_count=sum(u.retry_count for u in real),
    )
