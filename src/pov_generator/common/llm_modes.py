"""Ambient-режимы structured-вывода LLM.

По образцу :mod:`cancellation`: ambient-скоуп на ``ContextVar``, чтобы верхний
слой (сервис) мог попросить особый режим, **не меняя сигнатуру**
``LLMProvider.chat_json`` (ISP сохраняется, провайдеры подключают поведение
точечно — OCP).

``plain_json_scope`` просит «плоский» режим вместо строгого structured-вывода:
один проход с schema-в-промпте и tolerant-разбором, БЕЗ многоходовой
strict-coercion (``--json-schema``) и БЕЗ compositional-декомпозиции. Нужен для
запросов, где важнее один быстрый ход, а форму добьёт нормализация — например,
выявление решений (best-effort, дешёвая модель, тяжёлая схема: strict-coercion
там давал штормы ретраев и десятки минут).

Кто honor'ит режим:
* :class:`CompositionalLLMProvider` — пропускает декомпозицию (один проход);
* провайдеры с плоским путём (claude_subscription) — не передают
  ``--json-schema``, полагаясь на schema-в-промпте + нормализацию.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_prefer_plain_json: ContextVar[bool] = ContextVar("prefer_plain_json", default=False)


def plain_json_preferred() -> bool:
    """Активен ли в текущем скоупе «плоский» режим structured-вывода."""
    return _prefer_plain_json.get()


@contextmanager
def plain_json_scope() -> Iterator[None]:
    """Включить «плоский» режим structured-вывода на время блока.

    Вложенность безопасна; выход из блока восстанавливает прежнее значение.
    """
    reset_token = _prefer_plain_json.set(True)
    try:
        yield
    finally:
        _prefer_plain_json.reset(reset_token)
