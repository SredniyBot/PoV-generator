"""Общий разбор облегчённого ответа решений (для обоих источников).

``decision_identification`` (pre-flight) и ``decision_extraction`` (emergent)
теперь используют ЕДИНУЮ облегчённую схему (см.
:func:`domain.decisions.light_decision_item_schema`). Здесь — общий маппинг
«сырых» элементов в богатый domain, чтобы решения в реестре были однородны
независимо от источника:

* ``alternatives`` = ``{label, description}`` → :class:`DecisionAlternative` с
  детерминированным ``option_id = opt-N`` (по индексу; без машинных id от модели
  и кросс-ссылок), пустыми ``pros``/``cons`` и ``confidence=None``;
* рекомендация приходит по ``label`` → находим её ``option_id``; при промахе —
  первая альтернатива (best-effort, без хрупкости).

Чистые функции, без I/O.
"""

from __future__ import annotations

from typing import Any

from ..domain.decisions import DecisionAlternative


def light_alternatives(raw_alternatives: Any) -> tuple[DecisionAlternative, ...]:
    """Облегчённые ``{label, description}`` → кортеж :class:`DecisionAlternative`."""
    items = raw_alternatives if isinstance(raw_alternatives, list) else []
    return tuple(
        DecisionAlternative(
            option_id=f"opt-{index + 1}",
            label=str(alt.get("label", "")).strip(),
            description=str(alt.get("description", "")),
            pros=(),
            cons=(),
            confidence=None,
        )
        for index, alt in enumerate(items)
        if isinstance(alt, dict) and str(alt.get("label", "")).strip()
    )


def resolve_recommended_option_id(
    alternatives: tuple[DecisionAlternative, ...], recommended_label: Any
) -> str:
    """option_id альтернативы с данным ``label``; иначе первая (best-effort)."""
    label = str(recommended_label or "").strip()
    match = next((alt.option_id for alt in alternatives if alt.label == label), None)
    if match is not None:
        return match
    return alternatives[0].option_id if alternatives else ""
