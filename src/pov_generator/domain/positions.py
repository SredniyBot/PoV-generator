"""Положения проекта (positions) — единая форма знания о проекте.

Положение — это любая единица знания, которое система держит о проекте:
факт из входа, допущение, принятое решение, ограничение или риск. Все
типы имеют одинаковую операционную форму: формулировку простым языком,
источник появления, уверенность системы, видимость и связи с другими
положениями.

Этот модуль определяет только саму форму положения. Хранение, патчи,
проекции и интеграция с артефактами лежат в `project_knowledge.py`.

Архитектурно: положения образуют Layer A в разделении состояния проекта
(см. roadmap, Этап 0.1). Layer A — знание о проекте; Layer B — состояние
работы (process state).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PositionType = Literal["fact", "assumption", "decision", "constraint", "risk"]
"""Роль положения в понимании проекта.

- ``fact`` — что-то истинное (извлечено из входа или подтверждено).
- ``assumption`` — выведено системой, не подтверждено пользователем.
- ``decision`` — выбрано между альтернативами.
- ``constraint`` — жёсткая граница (бюджет, срок, регуляторика).
- ``risk`` — известная опасность, требующая внимания.

Тип — это роль, а не подкласс: операционная форма одна на все типы.
Это даёт однородную коллекцию и одинаковые проекции.
"""

VisibilityLevel = Literal["principal", "architectural", "technical"]
"""Уровень видимости положения.

- ``principal`` — бизнес-цель, главное ограничение, целевой пользователь.
- ``architectural`` — выбор подхода, контур решения, способ интеграции.
- ``technical`` — деталь схемы данных, библиотека, тонкости поведения.

Алинейка с engagement-режимом (см. roadmap, Этап 3): чем ниже уровень
видимости, тем выше требуемый engagement, чтобы вопрос вышел пользователю
проактивно. Менеджер всегда может оспорить положение любого уровня
вручную независимо от engagement-режима.
"""

PositionScope = Literal["global", "domain", "local"]
"""Область распространения положения.

- ``global`` — видно во всех контекстах задач проекта.
- ``domain`` — видно только когда активен соответствующий домен.
- ``local`` — видно только в узкой ветке проекта (потомках одного артефакта).

Опора для дешёвой выборки положений в контекст задачи (roadmap, Этап 2).
"""

PositionSource = Literal["input", "user", "system", "clarification", "artifact"]
"""Источник появления положения.

- ``input`` — извлечено из исходного материала (брифа, документов).
- ``user`` — введено пользователем явно.
- ``system`` — выведено системой автоматически.
- ``clarification`` — получено через уточнение у пользователя.
- ``artifact`` — следует из ранее созданного артефакта.
"""

PositionStatus = Literal["active", "superseded", "rejected"]
"""Жизненный цикл положения.

- ``active`` — текущее активное положение, на него опираются артефакты.
- ``superseded`` — заменено новой версией; хранится для аудита.
- ``rejected`` — явно отвергнуто, не используется в выборках по умолчанию.
"""


# Числовой ранг уровней видимости — для сравнения и оспаривания.
# Внутренний; внешние потребители используют ``visibility_rank``.
_VISIBILITY_RANK: dict[VisibilityLevel, int] = {
    "technical": 1,
    "architectural": 2,
    "principal": 3,
}


def visibility_rank(level: VisibilityLevel) -> int:
    """Числовой ранг уровня видимости (больше = виднее/важнее).

    ``principal`` (3) > ``architectural`` (2) > ``technical`` (1).

    Используется для сравнения уровней и для подъёма видимости при
    оспаривании: повышение разрешено только в сторону большего ранга.
    """
    return _VISIBILITY_RANK[level]


@dataclass(frozen=True)
class PositionAlternative:
    """Альтернатива, рассматривавшаяся при принятии решения.

    Используется в первую очередь для типа ``decision``, чтобы зафиксировать
    из чего система выбирала и почему отказалась от других вариантов.
    Альтернатива — иммутабельная запись, ссылок на положения не содержит
    (если нужна связь — она выражается через ``Position.related_position_ids``).
    """

    label: str
    rationale: str
    rejected_reason: str


@dataclass(frozen=True)
class Position:
    """Единая форма положения проекта.

    Положения хранятся в :class:`ProjectKnowledge` однородной коллекцией.
    Поле ``type`` определяет роль положения; структура одинаковая для всех.

    Инварианты:
        * ``identifier`` стабилен и уникален в пределах проекта.
        * ``confidence`` ∈ [0.0, 1.0].
        * Положение со ``status='superseded'`` имеет ``superseded_at``.
        * Положение со ``status='rejected'`` имеет ``rejection_reason``.
        * Если ``supersedes`` задан, он указывает на ``identifier``
          ранее существовавшего положения.

    Жизненный цикл — иммутабельный. Изменения создают новые положения
    или меняют статус через патчи (см. ``project_knowledge``).
    """

    identifier: str
    type: PositionType
    statement: str
    visibility: VisibilityLevel
    scope: PositionScope
    source: PositionSource
    taken_by: str
    taken_at: str
    confidence: float = 1.0
    tags: tuple[str, ...] = ()
    alternatives: tuple[PositionAlternative, ...] = ()
    related_position_ids: tuple[str, ...] = ()
    status: PositionStatus = "active"
    supersedes: str | None = None
    superseded_at: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Position confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )
        if self.status == "superseded" and self.superseded_at is None:
            raise ValueError(
                f"Superseded position {self.identifier!r} must have superseded_at"
            )
        if self.status == "rejected" and self.rejection_reason is None:
            raise ValueError(
                f"Rejected position {self.identifier!r} must have rejection_reason"
            )


def position_from_primitive(payload: dict) -> Position:
    """Восстановить :class:`Position` из примитива (``to_primitive``).

    Единая точка реконструкции — используется и инфраструктурой (снимки), и
    кодеком патчей состояния (реплей при ролбеке).
    """
    alternatives = tuple(
        PositionAlternative(**alt) for alt in payload.get("alternatives", ())
    )
    return Position(
        identifier=payload["identifier"],
        type=payload["type"],
        statement=payload["statement"],
        visibility=payload["visibility"],
        scope=payload["scope"],
        source=payload["source"],
        taken_by=payload["taken_by"],
        taken_at=payload["taken_at"],
        confidence=float(payload.get("confidence", 1.0)),
        tags=tuple(payload.get("tags", ())),
        alternatives=alternatives,
        related_position_ids=tuple(payload.get("related_position_ids", ())),
        status=payload.get("status", "active"),
        supersedes=payload.get("supersedes"),
        superseded_at=payload.get("superseded_at"),
        rejection_reason=payload.get("rejection_reason"),
    )
