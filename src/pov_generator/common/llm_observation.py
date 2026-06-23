"""Ambient-контекст наблюдения за LLM-вызовами (v3.11).

По образцу :mod:`llm_modes`: верхний слой (сервис) помечает текущий вызов
метаданными (``purpose``, ``project_id``, ``task_id``), НЕ меняя сигнатуру
``LLMProvider.chat_json``. Единый чокпоинт (``LoggingLLMProvider``) читает этот
контекст и прикладывает его как метаданные спана трассировки (Langfuse).

Вложенные скоупы НАКАПЛИВАЮТСЯ (merge): внешний может задать ``project_id``,
внутренний — ``purpose``; провайдер увидит объединение. Значения ``None``
игнорируются, чтобы не затирать уже выставленный контекст.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_observation: ContextVar[dict[str, Any]] = ContextVar("llm_observation", default={})


def current_llm_observation() -> dict[str, Any]:
    """Снимок текущего контекста наблюдения (копия — менять безопасно)."""
    return dict(_observation.get())


@contextmanager
def llm_observation_scope(**fields: Any) -> Iterator[None]:
    """Добавить метаданные наблюдения на время блока (merge с текущими).

    Вложенность безопасна; выход восстанавливает прежнее значение. ``None``-поля
    отбрасываются, чтобы не затирать унаследованный контекст.
    """
    merged = {**_observation.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _observation.set(merged)
    try:
        yield
    finally:
        _observation.reset(token)
