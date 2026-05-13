"""Слой A — знания о проекте (Project Knowledge).

Однородная коллекция :class:`Position` — фактов, допущений, решений,
ограничений и рисков. Артефакты опираются на положения этого слоя.

Изменения проходят только через :data:`KnowledgePatch` и
:func:`apply_knowledge_patch`. Прямая мутация запрещена: применение
патча возвращает новое состояние с инкрементом версии.

См. roadmap, Этап 0.1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from ..common.errors import ConflictError, NotFoundError
from ..common.serialization import utc_now_iso
from .positions import (
    Position,
    PositionScope,
    PositionType,
    VisibilityLevel,
    visibility_rank,
)


# --- зарезервированные идентификаторы ---------------------------------------


GOAL_POSITION_ID = "project.goal"
"""Идентификатор положения, описывающего цель проекта.

Цель проекта живёт в Layer A как положение типа ``fact`` с уровнем
видимости ``principal`` и scope ``global``. Использование стабильного
идентификатора позволяет приложению быстро получить цель без обхода
коллекции.
"""


# --- состояние слоя ----------------------------------------------------------


@dataclass(frozen=True)
class ProjectKnowledge:
    """Слой A — однородная коллекция положений проекта.

    Положения хранятся в одном dict по идентификатору. Группировки
    по типу/видимости/области — это проекции (методы запроса),
    не отдельные хранилища.

    Активные, superseded и rejected положения сосуществуют — это
    нужно для аудита и оспаривания. Проекции по умолчанию возвращают
    только активные.
    """

    positions: dict[str, Position] = field(default_factory=dict)
    version: int = 0
    updated_at: str = field(default_factory=utc_now_iso)

    # --- проекции (read-only) ------------------------------------------------

    def active(self) -> Iterator[Position]:
        """Активные положения (status='active')."""
        return (p for p in self.positions.values() if p.status == "active")

    def by_type(self, position_type: PositionType) -> Iterator[Position]:
        """Активные положения данного типа."""
        return (p for p in self.active() if p.type == position_type)

    def by_visibility(self, level: VisibilityLevel) -> Iterator[Position]:
        """Активные положения данного уровня видимости."""
        return (p for p in self.active() if p.visibility == level)

    def by_scope(self, scope: PositionScope) -> Iterator[Position]:
        """Активные положения данной области распространения."""
        return (p for p in self.active() if p.scope == scope)

    def by_tag(self, tag: str) -> Iterator[Position]:
        """Активные положения, помеченные данным тегом."""
        return (p for p in self.active() if tag in p.tags)

    def get(self, identifier: str) -> Position | None:
        """Положение по идентификатору без учёта статуса; None если нет."""
        return self.positions.get(identifier)

    def must_get(self, identifier: str) -> Position:
        """Положение по идентификатору; :class:`NotFoundError` если нет."""
        position = self.positions.get(identifier)
        if position is None:
            raise NotFoundError(f"Position not found: {identifier}")
        return position

    def goal(self) -> Position | None:
        """Положение-цель проекта (``GOAL_POSITION_ID``), если задана."""
        position = self.positions.get(GOAL_POSITION_ID)
        if position is None or position.status != "active":
            return None
        return position

    def goal_statement(self) -> str | None:
        """Формулировка цели проекта на естественном языке, если задана."""
        goal = self.goal()
        return goal.statement if goal is not None else None


# --- патчи -------------------------------------------------------------------


@dataclass(frozen=True)
class UpsertPositionPatch:
    """Добавить новое положение или заменить существующее по идентификатору.

    Безусловная замена: используется при первой записи положения или при
    «обновлении на месте» (например, корректировка confidence). Для
    versioning'а с историей использовать :class:`SupersedePositionPatch`.
    """

    position: Position


@dataclass(frozen=True)
class SupersedePositionPatch:
    """Заменить активное положение новой версией с историей.

    Старое положение получает ``status='superseded'`` и ``superseded_at``;
    новое становится активным. Поле ``new_position.supersedes`` будет
    автоматически выставлено в ``old_position_id`` при применении.

    Используется когда положение **переосмысливается** (а не просто
    корректируется): например, после ответа пользователя, опровергнувшего
    автоматическое допущение.
    """

    old_position_id: str
    new_position: Position


@dataclass(frozen=True)
class RejectPositionPatch:
    """Явно отвергнуть положение без замены.

    Положение получает ``status='rejected'`` и ``rejection_reason``.
    Используется при оспаривании пользователем, когда новой альтернативы
    нет — например, пользователь говорит «нет, это не так» без выбора
    другого варианта.
    """

    position_id: str
    reason: str


@dataclass(frozen=True)
class ElevateVisibilityPatch:
    """Повысить уровень видимости положения.

    Используется когда пользователь оспаривал положение — значит, оно
    для него важнее, чем казалось системе. Допустимо только повышение
    (``visibility_rank(new) > visibility_rank(current)``); понижение —
    ``ConflictError``.
    """

    position_id: str
    new_level: VisibilityLevel


KnowledgePatch = (
    UpsertPositionPatch
    | SupersedePositionPatch
    | RejectPositionPatch
    | ElevateVisibilityPatch
)


# --- применение --------------------------------------------------------------


def apply_knowledge_patch(
    knowledge: ProjectKnowledge,
    patch: KnowledgePatch,
) -> ProjectKnowledge:
    """Применить патч к слою знаний; возвращает новый снимок.

    Гарантии:
        * результат — новый объект (исходный не мутируется);
        * ``version`` инкрементируется на 1;
        * ``updated_at`` обновляется до now (UTC).

    Ошибки:
        * :class:`NotFoundError` — патч ссылается на несуществующее положение;
        * :class:`ConflictError` — нарушение инварианта (например, понижение
          видимости через :class:`ElevateVisibilityPatch`).
    """
    if isinstance(patch, UpsertPositionPatch):
        return _upsert(knowledge, patch.position)

    if isinstance(patch, SupersedePositionPatch):
        return _supersede(knowledge, patch.old_position_id, patch.new_position)

    if isinstance(patch, RejectPositionPatch):
        return _reject(knowledge, patch.position_id, patch.reason)

    if isinstance(patch, ElevateVisibilityPatch):
        return _elevate(knowledge, patch.position_id, patch.new_level)

    raise TypeError(f"Unsupported knowledge patch: {type(patch)!r}")


# --- внутренние операции -----------------------------------------------------


def _upsert(knowledge: ProjectKnowledge, position: Position) -> ProjectKnowledge:
    positions = dict(knowledge.positions)
    positions[position.identifier] = position
    return _bump(knowledge, positions)


def _supersede(
    knowledge: ProjectKnowledge,
    old_id: str,
    new_position: Position,
) -> ProjectKnowledge:
    old = knowledge.positions.get(old_id)
    if old is None:
        raise NotFoundError(f"Cannot supersede: position not found: {old_id}")
    if old.status != "active":
        raise ConflictError(
            f"Cannot supersede non-active position: {old_id!r} (status={old.status!r})"
        )

    now = utc_now_iso()
    superseded_old = replace(old, status="superseded", superseded_at=now)
    linked_new = replace(new_position, supersedes=old_id)

    positions = dict(knowledge.positions)
    positions[superseded_old.identifier] = superseded_old
    positions[linked_new.identifier] = linked_new
    return _bump(knowledge, positions, updated_at=now)


def _reject(
    knowledge: ProjectKnowledge, position_id: str, reason: str
) -> ProjectKnowledge:
    target = knowledge.positions.get(position_id)
    if target is None:
        raise NotFoundError(f"Cannot reject: position not found: {position_id}")
    if target.status == "rejected":
        # Идемпотентность: повторный reject с тем же reason — не ошибка.
        if target.rejection_reason == reason:
            return knowledge
        raise ConflictError(
            f"Position already rejected with different reason: {position_id!r}"
        )

    rejected = replace(target, status="rejected", rejection_reason=reason)
    positions = dict(knowledge.positions)
    positions[rejected.identifier] = rejected
    return _bump(knowledge, positions)


def _elevate(
    knowledge: ProjectKnowledge,
    position_id: str,
    new_level: VisibilityLevel,
) -> ProjectKnowledge:
    target = knowledge.positions.get(position_id)
    if target is None:
        raise NotFoundError(
            f"Cannot elevate visibility: position not found: {position_id}"
        )
    if visibility_rank(new_level) <= visibility_rank(target.visibility):
        raise ConflictError(
            f"Visibility elevation must raise the rank: "
            f"{target.visibility!r} → {new_level!r} is not an elevation"
        )

    elevated = replace(target, visibility=new_level)
    positions = dict(knowledge.positions)
    positions[elevated.identifier] = elevated
    return _bump(knowledge, positions)


def _bump(
    knowledge: ProjectKnowledge,
    positions: dict[str, Position],
    *,
    updated_at: str | None = None,
) -> ProjectKnowledge:
    """Собрать новый снимок с инкрементом версии."""
    return ProjectKnowledge(
        positions=positions,
        version=knowledge.version + 1,
        updated_at=updated_at or utc_now_iso(),
    )
