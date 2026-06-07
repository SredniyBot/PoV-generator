"""Граф зависимостей шагов для ролбека (Ф2).

Зависимость **звуковая**, а не эвристика: ребро X → Y, если шаг Y прочитал/
потребовал то, что записал шаг X, и X выполнен раньше Y. На основе:
- write-set(X): артефакты (``created_by_task_id``) + изменения состояния из
  ``state_events`` с ``task_id == X`` (позиции/readiness/gaps/паки);
- read-set(Y): фактические чтения из ``context_manifests`` (входные артефакты +
  использованные позиции) ∪ контрактные требования шаблона (readiness/gaps/паки).

Откатываемое множество — транзитивное замыкание от целевого шага. При сомнении
множество расширяется (консервативно), а не сужается.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Ключ единицы состояния: (вид, идентификатор). Виды: artifact / position /
# readiness / gap / domain_pack.
StateKey = tuple[str, str]


@dataclass(frozen=True)
class StepFootprint:
    """След шага: что записал (writes) и что прочитал/потребовал (reads)."""

    task_id: str
    seq: int
    writes: frozenset[StateKey]
    reads: frozenset[StateKey]


def compute_rollback_set(target_task_id: str, footprints: list[StepFootprint]) -> set[str]:
    """Транзитивное замыкание: целевой шаг + все, кто (транзитивно) зависят.

    Ребро X → Y: ``writes(X) ∩ reads(Y) ≠ ∅`` и ``seq(X) < seq(Y)``. Чистая
    функция — легко тестируется на любой топологии.
    """
    by_id = {footprint.task_id: footprint for footprint in footprints}
    reverted = {target_task_id}
    if target_task_id not in by_id:
        return reverted
    changed = True
    while changed:
        changed = False
        for candidate in footprints:
            if candidate.task_id in reverted:
                continue
            for reverted_id in tuple(reverted):
                source = by_id.get(reverted_id)
                if source is None:
                    continue
                if source.seq < candidate.seq and (source.writes & candidate.reads):
                    reverted.add(candidate.task_id)
                    changed = True
                    break
    return reverted


# --- ридер следов из рантайма (импурный) ------------------------------------


def _patch_write_keys(patch_type: str, payload: dict) -> set[StateKey]:
    """Какую единицу состояния записывает патч (для write-set)."""
    if patch_type == "UpsertPositionPatch":
        pid = (payload.get("position") or {}).get("identifier")
        return {("position", pid)} if pid else set()
    if patch_type == "SupersedePositionPatch":
        pid = (payload.get("new_position") or {}).get("identifier")
        return {("position", pid)} if pid else set()
    if patch_type in ("RejectPositionPatch", "ElevateVisibilityPatch"):
        pid = payload.get("position_id")
        return {("position", pid)} if pid else set()
    if patch_type == "UpsertReadinessPatch":
        dimension = payload.get("dimension")
        return {("readiness", dimension)} if dimension else set()
    if patch_type in ("UpsertGapPatch", "CloseGapPatch"):
        gap_id = payload.get("gap_id")
        return {("gap", gap_id)} if gap_id else set()
    if patch_type in ("ActivateDomainPackPatch", "DisableDomainPackPatch"):
        pack_ref = payload.get("pack_ref")
        return {("domain_pack", pack_ref)} if pack_ref else set()
    return set()


def _patch_read_keys(patch_type: str, payload: dict) -> set[StateKey]:
    """Что патч читает из состояния (например, заменяемую позицию)."""
    if patch_type == "SupersedePositionPatch":
        old = payload.get("old_position_id")
        return {("position", old)} if old else set()
    return set()


def collect_step_footprints(runtime, workspace: Path, snapshot) -> list[StepFootprint]:
    """Собрать следы выполненных листовых шагов (у которых есть чекпоинт).

    Порядок шага — `seq` последнего чекпоинта (последняя попытка исполнения).
    """
    seq_by_task: dict[str, int] = {}
    for checkpoint in runtime.list_step_checkpoints(workspace):
        prev = seq_by_task.get(checkpoint.task_id)
        seq_by_task[checkpoint.task_id] = checkpoint.seq if prev is None else max(prev, checkpoint.seq)

    writes: dict[str, set[StateKey]] = {tid: set() for tid in seq_by_task}
    reads: dict[str, set[StateKey]] = {tid: set() for tid in seq_by_task}

    # write-set: артефакты шага.
    for artifact in runtime.list_artifacts(workspace):
        owner = artifact.created_by_task_id
        if owner in writes:
            writes[owner].add(("artifact", artifact.artifact_id))

    # write-/read-set: изменения состояния, тегированные шагом.
    for event in runtime.list_state_events(workspace):
        tid = event.task_id
        if tid not in writes:
            continue
        writes[tid].update(_patch_write_keys(event.patch_type, event.payload))
        reads[tid].update(_patch_read_keys(event.patch_type, event.payload))

    # read-set: фактические чтения из контекста (артефакты + позиции).
    for manifest in runtime.list_context_manifests(workspace):
        tid = manifest.task_id
        if tid not in reads:
            continue
        for item in manifest.items:
            ref = item.source_ref or ""
            if ref.startswith("artifact:"):
                reads[tid].add(("artifact", ref.split(":", 1)[1]))
        for position_id in manifest.used_position_ids:
            reads[tid].add(("position", position_id))

    # read-set: контрактные требования шаблона (readiness/gaps/паки).
    tasks_by_id = {task.task_id: task for task in runtime.list_tasks(workspace)}
    for tid in seq_by_task:
        task = tasks_by_id.get(tid)
        if task is None:
            continue
        try:
            template = snapshot.resolve_template(task.template_ref)
        except Exception:
            continue
        for dimension in template.inputs.required_readiness:
            reads[tid].add(("readiness", dimension))
        for gap_id in template.inputs.forbidden_open_gaps:
            reads[tid].add(("gap", gap_id))
        for pack_ref in template.inputs.required_domain_packs:
            reads[tid].add(("domain_pack", pack_ref))

    return [
        StepFootprint(
            task_id=tid,
            seq=seq_by_task[tid],
            writes=frozenset(writes[tid]),
            reads=frozenset(reads[tid]),
        )
        for tid in seq_by_task
    ]
