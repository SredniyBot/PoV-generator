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

Помимо явного скоупа, :class:`CompositionalLLMProvider` АВТОМАТИЧЕСКИ ведёт себя
как plain для провайдеров с лимитом окна (атрибут ``token_window_limited`` —
напр. ``claude_subscription``): для них caching не помогает лимиту (окно считает
ОБЪЁМ cache-read = вызовы × префикс), поэтому проактивная декомпозиция на N
фрагментов взрывала бы 5-часовое окно. Их основной путь — один плоский проход +
нормализация; сборка по частям остаётся РЕАКТИВНОЙ крайней мерой
(``force_compositional_scope``), только при реальном провале формы.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_prefer_plain_json: ContextVar[bool] = ContextVar("prefer_plain_json", default=False)
# Принудительная compositional-сборка: перебивает «один проход» провайдера с
# лимитом окна (token_window_limited). Включается ТОЛЬКО как реактивная
# крайняя мера, когда плоский проход + нормализация + self-repair не уложились
# в схему (execution_service). Так дорогая сборка по частям не платится на
# каждой задаче, а только при реальном провале формы.
_force_compositional: ContextVar[bool] = ContextVar("force_compositional", default=False)
# Метка текущего фрагмента сборки (compositional) — чтобы провайдер мог написать
# в лог, КАКОЙ именно фрагмент пришлось деградировать/ретраить. Наблюдаемость
# «что именно ретраится», без протаскивания метки через сигнатуру chat_json.
_fragment_label: ContextVar[str | None] = ContextVar("fragment_label", default=None)


def plain_json_preferred() -> bool:
    """Активен ли в текущем скоупе «плоский» режим structured-вывода."""
    return _prefer_plain_json.get()


def force_compositional_requested() -> bool:
    """Запрошена ли принудительная сборка по частям (реактивная крайняя мера)."""
    return _force_compositional.get()


def current_fragment_label() -> str | None:
    """Метка текущего фрагмента сборки (для диагностических логов)."""
    return _fragment_label.get()


@contextmanager
def fragment_label_scope(label: str) -> Iterator[None]:
    """Пометить текущий фрагмент сборки на время блока (для логов провайдера)."""
    reset_token = _fragment_label.set(label)
    try:
        yield
    finally:
        _fragment_label.reset(reset_token)


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


@contextmanager
def force_compositional_scope() -> Iterator[None]:
    """Включить принудительную compositional-сборку на время блока.

    Крайняя мера: верхний слой (execution_service) включает её для ПОВТОРНОГО
    прохода, если плоский проход не уложился в схему. Перебивает «один проход»
    провайдера с лимитом окна. Вложенность безопасна.
    """
    reset_token = _force_compositional.set(True)
    try:
        yield
    finally:
        _force_compositional.reset(reset_token)
