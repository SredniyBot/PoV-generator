"""Стратегии объединения артефактов merge-задачами (Этап 5 roadmap).

Этап 5 вводит merge как отдельный класс leaf-задач:

* **structural** — детерминированное объединение N артефактов в один.
  Без LLM. Подходит для непересекающихся доменов и fan-out результатов.
* **synthetic** — интеграция через LLM (тот же путь, что обычная LLM
  leaf-задача). Подходит для нарративной склейки пересекающихся
  артефактов (финальный requirements_spec и т.п.).
* **hybrid** — комбинация (зарезервировано на будущее).

Этот модуль содержит **только** структурную стратегию: она чистая
функция от inputs + policy → output. Synthetic-стратегия проходит
обычным путём исполнения (LLM с merge-aware промптом) и описана в
``execution_service``.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from typing import Any

from ..common.errors import ConflictError
from ..domain.registry import ConflictPolicy


def structural_merge(
    inputs: Sequence[dict[str, Any]],
    *,
    conflict_policy: ConflictPolicy = "union",
) -> dict[str, Any]:
    """Структурно объединить N входных артефактов в один словарь.

    Алгоритм — рекурсивный deep-merge inputs слева направо. Каждое
    последующее значение объединяется с накопленным результатом по
    правилам типа поля:

    * **dict** — рекурсивный merge по ключам;
    * **list** — поведение зависит от ``conflict_policy``:

      - ``union`` — конкатенация с дедупликацией хэшируемых элементов
        (порядок первого появления сохраняется);
      - ``first_wins`` — берётся список из накопленного результата;
      - ``last_wins`` — берётся список из нового входа;
      - ``fail_on_conflict`` — :class:`ConflictError`, если списки
        не идентичны.

    * **scalar** (str/int/float/bool/None) — ``first_wins`` по
      умолчанию для всех политик кроме явных ``last_wins``/``fail_on_conflict``;
      ``union`` для скаляров эквивалентен ``first_wins``.

    Гарантии:

    * Чистая функция: входы не мутируются.
    * Детерминированна при одинаковых входах и политике.
    * Пустой ``inputs`` → пустой словарь ``{}``.

    Параметры:
        inputs: упорядоченный список словарей (содержимое артефактов).
            Несловарные элементы пропускаются.
        conflict_policy: см. :class:`ConflictPolicy`.

    Возвращает:
        Объединённый словарь. Должен быть отдельно провалидирован против
        контракта выходного артефакта.
    """
    result: dict[str, Any] = {}
    for payload in inputs:
        if not isinstance(payload, dict):
            continue
        result = _merge_dicts(result, payload, conflict_policy)
    return result


# --- internal helpers -------------------------------------------------------


def _merge_dicts(
    base: dict[str, Any],
    other: dict[str, Any],
    policy: ConflictPolicy,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in other.items():
        if key in merged:
            merged[key] = _merge_values(merged[key], value, policy)
        else:
            merged[key] = _clone(value)
    return merged


def _merge_values(left: Any, right: Any, policy: ConflictPolicy) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        return _merge_dicts(left, right, policy)
    if isinstance(left, list) and isinstance(right, list):
        return _merge_lists(left, right, policy)
    # Scalar conflict (или mixed-type) — решается политикой.
    if policy == "last_wins":
        return _clone(right)
    if policy == "fail_on_conflict" and left != right:
        raise ConflictError(
            f"Structural merge conflict: '{left!r}' vs '{right!r}' (policy=fail_on_conflict)"
        )
    # union/first_wins/совпадающие значения — берём left.
    return left


def _merge_lists(
    left: list[Any], right: list[Any], policy: ConflictPolicy
) -> list[Any]:
    if policy == "first_wins":
        return list(left)
    if policy == "last_wins":
        return list(right)
    if policy == "fail_on_conflict":
        if left != right:
            raise ConflictError(
                f"Structural merge conflict: list mismatch (policy=fail_on_conflict)"
            )
        return list(left)
    # union: дедуп для хэшируемых, иначе сохраняем порядок появления.
    return _dedup_preserving_order(left + right)


def _dedup_preserving_order(items: Iterable[Any]) -> list[Any]:
    """Дедуп с сохранением порядка первого появления.

    Хэшируемые элементы сравниваются через set. Нехэшируемые (dict, list)
    — через линейный поиск (O(n²), но в практических merge'ах размер
    мал и приемлем).
    """
    result: list[Any] = []
    hashable_seen: set[Hashable] = set()
    for item in items:
        try:
            if item in hashable_seen:
                continue
            hashable_seen.add(item)
            result.append(_clone(item))
        except TypeError:
            # Нехэшируемый — fallback на линейный поиск.
            if any(existing == item for existing in result):
                continue
            result.append(_clone(item))
    return result


def _clone(value: Any) -> Any:
    """Поверхностный клон контейнеров (dict/list) — защита от мутаций
    входов из вне. Скаляры immutable, возвращаем как есть."""
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value
