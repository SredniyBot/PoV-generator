"""Кодек патчей состояния + реконструкция (Ф3a ролбека).

Патчи в ``state_events`` хранятся как (``patch_type``, ``payload``). Для
ролбека их нужно десериализовать обратно в объекты-патчи и переиграть поверх
базового состояния (чекпоинта), пропуская патчи откаченных шагов.

Десериализация явная (без магии): неизвестный тип патча → ошибка, чтобы откат
не «тихо» потерял изменение, а упал в транзакции.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import fields, is_dataclass

from ..domain.positions import position_from_primitive
from ..domain.process_state import (
    ActivateDomainPackPatch,
    ActivateMethodologyPackPatch,
    CloseGapPatch,
    DetectDomainSignalPatch,
    DisableDomainPackPatch,
    DisableMethodologyPackPatch,
    ProcessState,
    SetClarificationModePatch,
    SetRootTaskPatch,
    UpsertGapPatch,
    UpsertReadinessPatch,
    apply_process_patch,
)
from ..domain.project_knowledge import (
    ElevateVisibilityPatch,
    ProjectKnowledge,
    RejectPositionPatch,
    SupersedePositionPatch,
    UpsertPositionPatch,
    apply_knowledge_patch,
)
from ..domain.project_state import StateEvent

_PROCESS_PATCHES = {
    cls.__name__: cls
    for cls in (
        SetRootTaskPatch,
        UpsertGapPatch,
        CloseGapPatch,
        UpsertReadinessPatch,
        DetectDomainSignalPatch,
        ActivateDomainPackPatch,
        DisableDomainPackPatch,
        ActivateMethodologyPackPatch,
        DisableMethodologyPackPatch,
        SetClarificationModePatch,
    )
}


def _build_dataclass(cls, payload: dict):
    """Собрать плоский патч-датакласс из payload, списки → кортежи (для tuple-полей)."""
    field_names = {f.name for f in fields(cls)} if is_dataclass(cls) else set()
    kwargs = {}
    for key, value in payload.items():
        if key not in field_names:
            continue
        kwargs[key] = tuple(value) if isinstance(value, list) else value
    return cls(**kwargs)


def decode_patch(layer: str, patch_type: str, payload: dict):
    """Десериализовать патч из (слой, тип, payload)."""
    if layer == "knowledge":
        if patch_type == "UpsertPositionPatch":
            return UpsertPositionPatch(position=position_from_primitive(payload["position"]))
        if patch_type == "SupersedePositionPatch":
            return SupersedePositionPatch(
                old_position_id=payload["old_position_id"],
                new_position=position_from_primitive(payload["new_position"]),
            )
        if patch_type == "RejectPositionPatch":
            return RejectPositionPatch(
                position_id=payload["position_id"], reason=payload["reason"]
            )
        if patch_type == "ElevateVisibilityPatch":
            return ElevateVisibilityPatch(
                position_id=payload["position_id"], new_level=payload["new_level"]
            )
        raise ValueError(f"Неизвестный knowledge-патч: {patch_type}")
    if layer == "process":
        cls = _PROCESS_PATCHES.get(patch_type)
        if cls is None:
            raise ValueError(f"Неизвестный process-патч: {patch_type}")
        return _build_dataclass(cls, payload)
    raise ValueError(f"Неизвестный слой состояния: {layer}")


def reconstruct_layers(
    base_knowledge: ProjectKnowledge,
    base_process: ProcessState,
    events: Iterable[StateEvent],
) -> tuple[ProjectKnowledge, ProcessState]:
    """Переиграть события поверх базового состояния (чистая функция).

    ``events`` — упорядоченные по ``seq`` патчи, которые НУЖНО применить
    (вызывающий код уже отфильтровал откаченные). Декодирует каждый патч и
    применяет чистой доменной функцией.
    """
    knowledge = base_knowledge
    process = base_process
    for event in events:
        patch = decode_patch(event.layer, event.patch_type, event.payload)
        if event.layer == "knowledge":
            knowledge = apply_knowledge_patch(knowledge, patch)
        else:
            process = apply_process_patch(process, patch)
    return knowledge, process
