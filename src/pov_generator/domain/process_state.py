"""Слой B — состояние процесса работы над проектом (Process State).

Содержит динамику работы: пробелы, готовность, активные доменные и
методологические паки, режим вовлечённости. Не содержит знаний о
проекте — за это отвечает :mod:`project_knowledge` (Layer A).

Изменения проходят через :data:`ProcessPatch` и
:func:`apply_process_patch`. Прямая мутация запрещена.

Алинейка engagement-видимости (roadmap, Этап 3) живёт здесь как
:func:`should_ask_user_for`: для заданного режима возвращает множество
уровней видимости, при которых положение проактивно выносится на
пользователя. Право оспорить любой уровень — универсально и не зависит
от engagement-режима.

См. roadmap, Этапы 0.1, 0.2 и 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..common.errors import ConflictError, NotFoundError
from ..common.serialization import utc_now_iso
from .clarifications import ClarificationMode
from .positions import VisibilityLevel

# --- значения статусов -------------------------------------------------------


ReadinessStatus = Literal["missing", "partial", "ready", "waived"]
GapSeverity = Literal["low", "medium", "high", "critical"]
DomainPackStatus = Literal["candidate", "active", "disabled"]
DomainPackSource = Literal["llm_detector", "operator", "artifact", "system", "bootstrap"]
MethodologyPackStatus = Literal["active", "disabled"]
MethodologyPackSource = Literal["operator", "objective_default", "system", "bootstrap"]


# --- записи слоя -------------------------------------------------------------


@dataclass(frozen=True)
class GapRecord:
    """Открытый пробел в проекте — недостающее знание/решение/входные данные."""

    identifier: str
    title: str
    description: str
    severity: GapSeverity
    blocking: bool
    opened_at: str
    closed_at: str | None = None


@dataclass(frozen=True)
class ReadinessRecord:
    """Готовность к следующим фазам по конкретному измерению."""

    dimension: str
    status: ReadinessStatus
    blocking: bool
    confidence: float
    evidence: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class DomainSignalRecord:
    """Сигнал, по которому система рассматривает активацию доменного пака."""

    domain: str
    signal: str
    source: str
    confidence: float
    detected_at: str


@dataclass(frozen=True)
class ActiveDomainPackRecord:
    """Активный (или кандидатный/отключённый) доменный пак проекта."""

    ref: str
    domain: str
    status: DomainPackStatus
    source: DomainPackSource
    rationale: str
    confidence: float
    activated_at: str


@dataclass(frozen=True)
class ActiveMethodologyPackRecord:
    """Активный (или отключённый) методологический пак проекта."""

    ref: str
    status: MethodologyPackStatus
    source: MethodologyPackSource
    rationale: str
    activated_at: str


# --- алинейка engagement ↔ видимости ----------------------------------------


# Таблица: какие уровни видимости проактивно выходят на пользователя
# в данном режиме. Право оспорить любой уровень — универсально и не
# зависит от engagement-режима.
#
# autopilot — спрашиваем только principal-вопросы;
# balanced  — principal и architectural;
# control   — то же что balanced, политика confidence/важности толкает
#             ближе к principal в спорных случаях;
# expert    — все три уровня, включая technical.
_PROACTIVE_ASK_LEVELS: dict[ClarificationMode, frozenset[VisibilityLevel]] = {
    "autopilot": frozenset({"principal"}),
    "balanced": frozenset({"principal", "architectural"}),
    "control": frozenset({"principal", "architectural"}),
    "expert": frozenset({"principal", "architectural", "technical"}),
}


def proactive_ask_levels(mode: ClarificationMode) -> frozenset[VisibilityLevel]:
    """Уровни видимости, проактивно выносимые на пользователя в данном режиме."""
    return _PROACTIVE_ASK_LEVELS[mode]


# --- состояние слоя ----------------------------------------------------------


@dataclass(frozen=True)
class ProcessState:
    """Слой B — динамическое состояние работы над проектом.

    Не хранит знаний о проекте (это :class:`ProjectKnowledge`).
    Хранит: что отсутствует (gaps), что готово (readiness), какие паки
    активны, в каком режиме работает менеджер.

    Жизненный цикл — событийный через :data:`ProcessPatch`.
    """

    root_task_id: str | None = None
    active_gaps: dict[str, GapRecord] = field(default_factory=dict)
    readiness: dict[str, ReadinessRecord] = field(default_factory=dict)
    domain_signals: dict[str, DomainSignalRecord] = field(default_factory=dict)
    active_domain_packs: dict[str, ActiveDomainPackRecord] = field(default_factory=dict)
    active_methodology_packs: dict[str, ActiveMethodologyPackRecord] = field(
        default_factory=dict
    )
    clarification_mode: ClarificationMode = "balanced"
    version: int = 0
    updated_at: str = field(default_factory=utc_now_iso)

    # --- проекции (read-only) ------------------------------------------------

    @property
    def active_domain_pack_records(self) -> dict[str, ActiveDomainPackRecord]:
        """Только активные доменные паки (исключая candidate/disabled)."""
        return {
            ref: record
            for ref, record in self.active_domain_packs.items()
            if record.status == "active"
        }

    @property
    def active_methodology_pack_records(self) -> dict[str, ActiveMethodologyPackRecord]:
        """Только активные методологические паки (исключая disabled)."""
        return {
            ref: record
            for ref, record in self.active_methodology_packs.items()
            if record.status == "active"
        }

    @property
    def blocking_gaps(self) -> dict[str, GapRecord]:
        """Активные пробелы, помеченные ``blocking=True``."""
        return {gid: gap for gid, gap in self.active_gaps.items() if gap.blocking}

    def should_ask_user_for(self, visibility: VisibilityLevel) -> bool:
        """Выводится ли положение этого уровня видимости проактивно на пользователя.

        При ``False`` система имеет право принять решение автономно
        (с фиксацией в Layer A как положение с соответствующим источником).
        При ``True`` — должна породить уточнение и дождаться ответа.

        Право оспорить любое положение остаётся у пользователя
        независимо от этого ответа (см. roadmap, Этап 3.2).
        """
        return visibility in proactive_ask_levels(self.clarification_mode)


# --- патчи -------------------------------------------------------------------


@dataclass(frozen=True)
class SetRootTaskPatch:
    """Зафиксировать корневую задачу проекта."""

    task_id: str


@dataclass(frozen=True)
class UpsertGapPatch:
    """Открыть пробел или обновить его описание."""

    gap_id: str
    title: str
    description: str
    severity: GapSeverity = "medium"
    blocking: bool = True


@dataclass(frozen=True)
class CloseGapPatch:
    """Закрыть открытый пробел."""

    gap_id: str


@dataclass(frozen=True)
class UpsertReadinessPatch:
    """Обновить готовность по измерению."""

    dimension: str
    status: ReadinessStatus
    blocking: bool
    confidence: float = 1.0
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetectDomainSignalPatch:
    """Зафиксировать обнаруженный доменный сигнал."""

    domain: str
    signal: str
    source: str
    confidence: float


@dataclass(frozen=True)
class ActivateDomainPackPatch:
    """Активировать доменный пак."""

    pack_ref: str
    domain: str
    source: DomainPackSource = "operator"
    rationale: str = ""
    confidence: float = 1.0


@dataclass(frozen=True)
class DisableDomainPackPatch:
    """Отключить ранее активированный доменный пак."""

    pack_ref: str


@dataclass(frozen=True)
class ActivateMethodologyPackPatch:
    """Активировать методологический пак."""

    pack_ref: str
    source: MethodologyPackSource = "operator"
    rationale: str = ""


@dataclass(frozen=True)
class DisableMethodologyPackPatch:
    """Отключить ранее активированный методологический пак."""

    pack_ref: str


@dataclass(frozen=True)
class SetClarificationModePatch:
    """Сменить режим вовлечённости пользователя."""

    mode: ClarificationMode


ProcessPatch = (
    SetRootTaskPatch
    | UpsertGapPatch
    | CloseGapPatch
    | UpsertReadinessPatch
    | DetectDomainSignalPatch
    | ActivateDomainPackPatch
    | DisableDomainPackPatch
    | ActivateMethodologyPackPatch
    | DisableMethodologyPackPatch
    | SetClarificationModePatch
)


# --- применение --------------------------------------------------------------


def apply_process_patch(state: ProcessState, patch: ProcessPatch) -> ProcessState:
    """Применить патч к состоянию процесса; возвращает новый снимок.

    Гарантии:
        * результат — новый объект;
        * ``version`` инкрементируется;
        * ``updated_at`` обновляется.

    Ошибки:
        * :class:`NotFoundError` — патч ссылается на несуществующую запись;
        * :class:`ConflictError` — нарушение инварианта (например,
          ``confidence`` вне [0, 1]).
    """
    now = utc_now_iso()

    if isinstance(patch, SetRootTaskPatch):
        return _bump(state, root_task_id=patch.task_id, updated_at=now)

    if isinstance(patch, UpsertGapPatch):
        gaps = dict(state.active_gaps)
        gaps[patch.gap_id] = GapRecord(
            identifier=patch.gap_id,
            title=patch.title,
            description=patch.description,
            severity=patch.severity,
            blocking=patch.blocking,
            opened_at=now,
        )
        return _bump(state, active_gaps=gaps, updated_at=now)

    if isinstance(patch, CloseGapPatch):
        if patch.gap_id not in state.active_gaps:
            raise NotFoundError(f"Gap not found: {patch.gap_id}")
        gaps = dict(state.active_gaps)
        gaps.pop(patch.gap_id)
        return _bump(state, active_gaps=gaps, updated_at=now)

    if isinstance(patch, UpsertReadinessPatch):
        if not 0.0 <= patch.confidence <= 1.0:
            raise ConflictError("Readiness confidence must be in [0.0, 1.0].")
        readiness = dict(state.readiness)
        readiness[patch.dimension] = ReadinessRecord(
            dimension=patch.dimension,
            status=patch.status,
            blocking=patch.blocking,
            confidence=patch.confidence,
            evidence=patch.evidence,
            updated_at=now,
        )
        return _bump(state, readiness=readiness, updated_at=now)

    if isinstance(patch, DetectDomainSignalPatch):
        signals = dict(state.domain_signals)
        key = f"{patch.domain}:{patch.signal}"
        signals[key] = DomainSignalRecord(
            domain=patch.domain,
            signal=patch.signal,
            source=patch.source,
            confidence=patch.confidence,
            detected_at=now,
        )
        return _bump(state, domain_signals=signals, updated_at=now)

    if isinstance(patch, ActivateDomainPackPatch):
        packs = dict(state.active_domain_packs)
        packs[patch.pack_ref] = ActiveDomainPackRecord(
            ref=patch.pack_ref,
            domain=patch.domain,
            status="active",
            source=patch.source,
            rationale=patch.rationale,
            confidence=patch.confidence,
            activated_at=now,
        )
        return _bump(state, active_domain_packs=packs, updated_at=now)

    if isinstance(patch, DisableDomainPackPatch):
        current = state.active_domain_packs.get(patch.pack_ref)
        if current is None:
            raise NotFoundError(f"Domain pack not found: {patch.pack_ref}")
        packs = dict(state.active_domain_packs)
        packs[patch.pack_ref] = ActiveDomainPackRecord(
            ref=current.ref,
            domain=current.domain,
            status="disabled",
            source=current.source,
            rationale=current.rationale,
            confidence=current.confidence,
            activated_at=current.activated_at,
        )
        return _bump(state, active_domain_packs=packs, updated_at=now)

    if isinstance(patch, ActivateMethodologyPackPatch):
        packs = dict(state.active_methodology_packs)
        packs[patch.pack_ref] = ActiveMethodologyPackRecord(
            ref=patch.pack_ref,
            status="active",
            source=patch.source,
            rationale=patch.rationale,
            activated_at=now,
        )
        return _bump(state, active_methodology_packs=packs, updated_at=now)

    if isinstance(patch, DisableMethodologyPackPatch):
        current = state.active_methodology_packs.get(patch.pack_ref)
        if current is None:
            raise NotFoundError(f"Methodology pack not found: {patch.pack_ref}")
        packs = dict(state.active_methodology_packs)
        packs[patch.pack_ref] = ActiveMethodologyPackRecord(
            ref=current.ref,
            status="disabled",
            source=current.source,
            rationale=current.rationale,
            activated_at=current.activated_at,
        )
        return _bump(state, active_methodology_packs=packs, updated_at=now)

    if isinstance(patch, SetClarificationModePatch):
        return _bump(state, clarification_mode=patch.mode, updated_at=now)

    raise TypeError(f"Unsupported process patch: {type(patch)!r}")


# --- внутренние помощники ----------------------------------------------------


def _bump(state: ProcessState, **changes) -> ProcessState:
    """Собрать новый снимок ProcessState с инкрементом версии.

    Принимает только те ключи, которые отличаются от текущего состояния.
    Поля, не упомянутые в ``changes``, переносятся из ``state``.
    """
    base = {
        "root_task_id": state.root_task_id,
        "active_gaps": dict(state.active_gaps),
        "readiness": dict(state.readiness),
        "domain_signals": dict(state.domain_signals),
        "active_domain_packs": dict(state.active_domain_packs),
        "active_methodology_packs": dict(state.active_methodology_packs),
        "clarification_mode": state.clarification_mode,
        "version": state.version + 1,
        "updated_at": utc_now_iso(),
    }
    base.update(changes)
    return ProcessState(**base)
