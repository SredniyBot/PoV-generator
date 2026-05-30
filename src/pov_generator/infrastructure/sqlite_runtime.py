from __future__ import annotations

import functools
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TypeVar

from ..common.errors import NotFoundError
from ..common.serialization import json_dumps, json_loads, to_primitive, utc_now_iso
from ..domain.artifacts import (
    ArtifactMetadata,
    ArtifactRecord,
    ArtifactRelations,
    ContextBudget,
    ContextItem,
    ContextManifest,
)
from ..domain.checkpoints import CheckpointSession, CheckpointStatus
from ..domain.decisions import (
    Decision,
    DecisionAlternative,
    DecisionLevel,
    DecisionSource,
    DecisionStatus,
)
from ..domain.execution import ExecutionRequest, ExecutionResult, ExecutionTrace
from ..domain.planning import AdmissionCheck, CandidateEvaluation, PlanningDecision
from ..domain.positions import Position, PositionAlternative
from ..domain.process_state import (
    ActiveDomainPackRecord,
    ActiveMethodologyPackRecord,
    DomainSignalRecord,
    GapRecord,
    ProcessPatch,
    ProcessState,
    ReadinessRecord,
    apply_process_patch,
)
from ..domain.project_knowledge import (
    KnowledgePatch,
    ProjectKnowledge,
    apply_knowledge_patch,
)
from ..domain.project_state import ProjectManifest, ProjectState, StateEvent, StateLayer
from ..domain.tasks import TaskEvent, TaskRecord, apply_task_command
from ..domain.validation import EscalationTicket, ValidationFinding, ValidationRun
from ..domain.workflow_runs import WorkflowRunRecord, WorkflowRunStatus, WorkflowStepRecord

# --- сериализация Layer A (знания) -------------------------------------------


# --- сериализация CheckpointSession (v3.0) ------------------------------------


def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointSession:
    decision_ids = tuple(
        json_loads(row["decision_ids_json"]) if row["decision_ids_json"] else []
    )
    return CheckpointSession(
        session_id=row["session_id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        task_title=row["task_title"],
        artifact_role=row["artifact_role"],
        status=row["status"],
        decision_ids=decision_ids,
        created_at=row["created_at"],
        finalized_at=row["finalized_at"],
        finalized_by=row["finalized_by"],
    )


# --- сериализация Decision (v3.0 — реестр решений) ---------------------------


def _decision_to_row(decision: Decision, *, created_at: str, updated_at: str) -> dict[str, object]:
    """Превратить Decision в dict для bind в SQL.

    Tuple-коллекции (alternatives, affected_artifact_ids, depends_on_decision_ids)
    сериализуются в JSON-строки. Это упрощение оправдано на v3.0 — см.
    комментарий к схеме в ``_ensure_schema``.
    """
    return {
        "decision_id": decision.decision_id,
        "project_id": decision.project_id,
        "title": decision.title,
        "description": decision.description_without_category,
        "chosen_option_id": decision.chosen_option_id,
        "alternatives_json": json_dumps([to_primitive(alt) for alt in decision.alternatives]),
        "rationale": decision.rationale,
        "level": decision.level,
        "level_rationale": decision.level_rationale,
        "confidence": float(decision.confidence),
        "status": decision.status,
        "source": decision.source,
        "source_task_id": decision.source_task_id,
        "affected_artifact_ids_json": json_dumps(list(decision.affected_artifact_ids)),
        "depends_on_decision_ids_json": json_dumps(list(decision.depends_on_decision_ids)),
        "user_action": decision.user_action,
        "original_chosen_option_id": decision.original_chosen_option_id,
        "user_free_text_answer": decision.user_free_text_answer,
        "free_form_level_override": decision.free_form_level_override,
        "created_at": created_at,
        "updated_at": updated_at,
        "category": decision.normalized_category,
        "answer_mode": decision.answer_mode,
        "chosen_option_ids_json": json_dumps(list(decision.chosen_option_ids)),
        "user_verified": 1 if decision.user_verified else 0,
        "user_verified_at": decision.user_verified_at,
    }


def _decision_from_row(row: sqlite3.Row) -> Decision:
    """Десериализовать строку из таблицы decisions.

    Защитное чтение коллекций: пустая JSON-строка или null → пустой tuple.
    Это спасает от мусора в существующих базах при миграции.
    """
    raw_alts = json_loads(row["alternatives_json"]) if row["alternatives_json"] else []
    alternatives = tuple(
        DecisionAlternative(
            option_id=str(item["option_id"]),
            label=str(item.get("label", "")),
            description=str(item.get("description", "")),
            pros=tuple(item.get("pros", ()) or ()),
            cons=tuple(item.get("cons", ()) or ()),
            confidence=(
                float(item["confidence"])
                if item.get("confidence") is not None
                else None
            ),
        )
        for item in raw_alts
    )
    affected = tuple(
        json_loads(row["affected_artifact_ids_json"])
        if row["affected_artifact_ids_json"]
        else []
    )
    depends_on = tuple(
        json_loads(row["depends_on_decision_ids_json"])
        if row["depends_on_decision_ids_json"]
        else []
    )
    # v3.1 поля. Защитный access — БД могла быть создана до миграции.
    try:
        answer_mode = row["answer_mode"] or "single"
    except (KeyError, IndexError):
        answer_mode = "single"
    try:
        chosen_ids_raw = row["chosen_option_ids_json"]
        chosen_option_ids = tuple(json_loads(chosen_ids_raw)) if chosen_ids_raw else ()
    except (KeyError, IndexError):
        chosen_option_ids = ()
    # v3.4 — user_verified метка. Защитный read для legacy баз.
    try:
        user_verified = bool(row["user_verified"])
    except (KeyError, IndexError):
        user_verified = False
    try:
        user_verified_at = row["user_verified_at"]
    except (KeyError, IndexError):
        user_verified_at = None
    try:
        category = str(row["category"] or "")
    except (KeyError, IndexError):
        category = ""
    return Decision(
        decision_id=row["decision_id"],
        project_id=row["project_id"],
        title=row["title"],
        description=row["description"],
        chosen_option_id=row["chosen_option_id"],
        alternatives=alternatives,
        rationale=row["rationale"],
        level=row["level"],
        level_rationale=row["level_rationale"],
        confidence=float(row["confidence"]),
        status=row["status"],
        source=row["source"],
        source_task_id=row["source_task_id"],
        affected_artifact_ids=affected,
        depends_on_decision_ids=depends_on,
        user_action=row["user_action"],
        original_chosen_option_id=row["original_chosen_option_id"],
        user_free_text_answer=row["user_free_text_answer"],
        free_form_level_override=row["free_form_level_override"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        category=category,
        answer_mode=answer_mode,
        chosen_option_ids=chosen_option_ids,
        user_verified=user_verified,
        user_verified_at=user_verified_at,
    )


def _position_to_dict(position: Position) -> dict[str, object]:
    return to_primitive(position)


def _position_from_dict(payload: dict) -> Position:
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


def _knowledge_to_dict(knowledge: ProjectKnowledge) -> dict[str, object]:
    return {
        "positions": {
            identifier: _position_to_dict(position)
            for identifier, position in knowledge.positions.items()
        },
        "version": knowledge.version,
        "updated_at": knowledge.updated_at,
    }


def _knowledge_from_dict(payload: dict) -> ProjectKnowledge:
    positions = {
        identifier: _position_from_dict(value)
        for identifier, value in payload.get("positions", {}).items()
    }
    return ProjectKnowledge(
        positions=positions,
        version=int(payload.get("version", 0)),
        updated_at=payload.get("updated_at", utc_now_iso()),
    )


# --- сериализация Layer B (процесс) ------------------------------------------


def _process_to_dict(state: ProcessState) -> dict[str, object]:
    return to_primitive(state)


def _process_from_dict(payload: dict) -> ProcessState:
    return ProcessState(
        root_task_id=payload.get("root_task_id"),
        active_gaps={
            key: GapRecord(**value)
            for key, value in payload.get("active_gaps", {}).items()
        },
        readiness={
            key: ReadinessRecord(**value)
            for key, value in payload.get("readiness", {}).items()
        },
        domain_signals={
            key: DomainSignalRecord(**value)
            for key, value in payload.get("domain_signals", {}).items()
        },
        active_domain_packs={
            key: ActiveDomainPackRecord(**value)
            for key, value in payload.get("active_domain_packs", {}).items()
        },
        active_methodology_packs={
            key: ActiveMethodologyPackRecord(**value)
            for key, value in payload.get("active_methodology_packs", {}).items()
        },
        clarification_mode=payload.get("clarification_mode", "balanced"),
        version=int(payload.get("version", 0)),
        updated_at=payload.get("updated_at", utc_now_iso()),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        project_id=row["project_id"],
        objective_ref=row["objective_ref"],
        parent_task_id=row["parent_task_id"],
        template_ref=row["template_ref"],
        template_type=row["template_type"],
        title=row["title"],
        status=row["status"],
        origin_kind=row["origin_kind"],
        origin_ref=row["origin_ref"],
        stable_key=row["stable_key"],
        depth=row["depth"],
        slot_id=row["slot_id"],
        attempt=row["attempt"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _artifact_metadata_from_payload(payload: dict | None) -> ArtifactMetadata:
    """Восстановить :class:`ArtifactMetadata` из JSON-payload'а.

    Совместимо с старыми payload'ами, где metadata был свободным dict'ом —
    неизвестные поля попадают в :attr:`ArtifactMetadata.extras`.
    """
    if not payload:
        return ArtifactMetadata()
    known = {
        "template_ref",
        "provider",
        "model",
        "complexity",
        "methodology_pack_ref",
        "execution_run_id",
        "merge_strategy",
        "reasoning",
        "methodology_trace",
        "overall_confidence",
        "field_confidence",
        "used_position_ids",
        "token_usage",
        "extras",
    }
    extras = {key: value for key, value in payload.items() if key not in known}
    declared_extras = payload.get("extras") or {}
    if isinstance(declared_extras, dict):
        extras = {**declared_extras, **extras}
    # v3.5: token_usage — словарь словарей; защитное чтение для legacy.
    raw_usage = payload.get("token_usage") or {}
    token_usage: dict[str, dict[str, int]] = {}
    if isinstance(raw_usage, dict):
        for stage, vals in raw_usage.items():
            if isinstance(vals, dict):
                token_usage[str(stage)] = {
                    k: int(v) for k, v in vals.items() if isinstance(v, (int, float))
                }
    return ArtifactMetadata(
        template_ref=payload.get("template_ref"),
        provider=payload.get("provider"),
        model=payload.get("model"),
        complexity=payload.get("complexity"),
        methodology_pack_ref=payload.get("methodology_pack_ref"),
        execution_run_id=payload.get("execution_run_id"),
        merge_strategy=payload.get("merge_strategy"),
        reasoning=dict(payload.get("reasoning") or {}),
        methodology_trace=dict(payload.get("methodology_trace") or {}),
        overall_confidence=payload.get("overall_confidence"),
        field_confidence=dict(payload.get("field_confidence") or {}),
        used_position_ids=tuple(payload.get("used_position_ids") or ()),
        token_usage=token_usage,
        extras=extras,
    )


def _artifact_metadata_to_payload(metadata: ArtifactMetadata) -> dict[str, object]:
    """Сериализовать :class:`ArtifactMetadata` в плоский JSON-payload."""
    payload: dict[str, object] = {}
    if metadata.template_ref is not None:
        payload["template_ref"] = metadata.template_ref
    if metadata.provider is not None:
        payload["provider"] = metadata.provider
    if metadata.model is not None:
        payload["model"] = metadata.model
    if metadata.complexity is not None:
        payload["complexity"] = metadata.complexity
    if metadata.methodology_pack_ref is not None:
        payload["methodology_pack_ref"] = metadata.methodology_pack_ref
    if metadata.execution_run_id is not None:
        payload["execution_run_id"] = metadata.execution_run_id
    if metadata.merge_strategy is not None:
        payload["merge_strategy"] = metadata.merge_strategy
    if metadata.reasoning:
        payload["reasoning"] = dict(metadata.reasoning)
    if metadata.methodology_trace:
        payload["methodology_trace"] = dict(metadata.methodology_trace)
    if metadata.overall_confidence is not None:
        payload["overall_confidence"] = metadata.overall_confidence
    if metadata.field_confidence:
        payload["field_confidence"] = dict(metadata.field_confidence)
    if metadata.used_position_ids:
        payload["used_position_ids"] = list(metadata.used_position_ids)
    if metadata.token_usage:
        payload["token_usage"] = {
            stage: dict(vals) for stage, vals in metadata.token_usage.items()
        }
    if metadata.extras:
        payload["extras"] = dict(metadata.extras)
    return payload


def _artifact_relations_from_row(row: sqlite3.Row) -> ArtifactRelations:
    """Восстановить :class:`ArtifactRelations` из строки таблицы artifacts."""
    parent_id = row["parent_artifact_id"]
    try:
        inputs_json = row["input_artifact_ids_json"]
    except (KeyError, IndexError):
        inputs_json = None
    try:
        children_json = row["child_artifact_ids_json"]
    except (KeyError, IndexError):
        children_json = None
    inputs = tuple(json_loads(inputs_json)) if inputs_json else ()
    children = tuple(json_loads(children_json)) if children_json else ()
    return ArtifactRelations(
        parent_artifact_id=parent_id,
        input_artifact_ids=inputs,
        child_artifact_ids=children,
    )


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    try:
        is_superseded_value = row["is_superseded"]
    except (KeyError, IndexError):
        is_superseded_value = 0
    metadata = _artifact_metadata_from_payload(json_loads(row["metadata_json"]))
    relations = _artifact_relations_from_row(row)
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        project_id=row["project_id"],
        artifact_role=row["artifact_role"],
        title=row["title"],
        description=row["description"],
        artifact_format=row["artifact_format"],
        artifact_kind=row["artifact_kind"],
        created_by_task_id=row["created_by_task_id"],
        storage_path=row["storage_path"],
        created_at=row["created_at"],
        relations=relations,
        metadata=metadata,
        is_superseded=bool(is_superseded_value),
    )


def _context_item_from_row(row: sqlite3.Row) -> ContextItem:
    return ContextItem(
        item_id=row["item_id"],
        item_type=row["item_type"],
        source_ref=row["source_ref"],
        title=row["title"],
        content=row["content"],
        token_estimate=row["token_estimate"],
        required=bool(row["required"]),
        priority=row["priority"],
    )


# --- per-workspace write-coordinator -----------------------------------------
#
# Параллельное выполнение шагов workflow означает несколько потоков-воркеров,
# одновременно мутирующих один и тот же workspace (артефакты, состояние,
# решения, статусы задач). SQLite сам по себе допускает только одного писателя,
# а наши read-modify-write мутации (apply_*_patch: load → compute → commit)
# без блокировки дали бы потерянные обновления.
#
# Поэтому ВСЕ мутации сериализуются per-workspace реентрантным локом. LLM-
# вызовы (медленная часть) идут вне лока и реально параллелятся; критическая
# секция записи — микросекунды. Лок процесс-глобальный (ключ — resolved-путь
# workspace), потому что SqliteRuntime инстанцируется в нескольких местах, но
# файл БД у workspace один.
_WORKSPACE_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WORKSPACE_WRITE_LOCKS_GUARD = threading.Lock()


def _workspace_write_lock(workspace: Path) -> threading.RLock:
    key = str(Path(workspace).resolve())
    lock = _WORKSPACE_WRITE_LOCKS.get(key)
    if lock is None:
        with _WORKSPACE_WRITE_LOCKS_GUARD:
            lock = _WORKSPACE_WRITE_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _WORKSPACE_WRITE_LOCKS[key] = lock
    return lock


_RuntimeMethod = TypeVar("_RuntimeMethod", bound=Callable[..., object])


def _serialized_write(method: _RuntimeMethod) -> _RuntimeMethod:
    """Сериализовать мутирующий метод runtime per-workspace.

    Применяется к методам с сигнатурой ``(self, workspace, ...)``. Держит
    per-workspace лок на ВЕСЬ метод — это критично для read-modify-write
    (load внутри тоже под локом → второй писатель видит коммит первого, нет
    потерянных обновлений). Лок реентрантный, поэтому вложенные мутации того
    же потока не дедлочат.
    """

    @functools.wraps(method)
    def wrapper(self, workspace, *args, **kwargs):  # type: ignore[no-untyped-def]
        with _workspace_write_lock(workspace):
            return method(self, workspace, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class SqliteRuntime:
    DB_FILENAME = "runtime.db"
    MANIFEST_FILENAME = "project.json"

    def __init__(self) -> None:
        # Кэш «схема проверена» — schema идемпотентна, повторно проверять
        # на каждом ``_connect`` не нужно. Один раз на workspace.
        self._schema_ensured: set[Path] = set()

    @_serialized_write
    def create_workspace(
        self,
        workspace: Path,
        manifest: ProjectManifest,
        initial_state: ProjectState,
        bootstrap_events: tuple[StateEvent, ...] = (),
    ) -> None:
        """Создать workspace проекта.

        Записывает:
            * ``project.json`` — manifest (читаемый из файловой системы);
            * snapshot слоя знаний — :class:`ProjectKnowledge`;
            * snapshot слоя процесса — :class:`ProcessState`;
            * bootstrap-события (если переданы) — для аудита создания.
        """
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / self.MANIFEST_FILENAME).write_text(json_dumps(manifest), encoding="utf-8")
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into knowledge_snapshots(project_id, state_json, version, updated_at)
                values (?, ?, ?, ?)
                """,
                (
                    manifest.project_id,
                    json_dumps(_knowledge_to_dict(initial_state.knowledge)),
                    initial_state.knowledge.version,
                    initial_state.knowledge.updated_at,
                ),
            )
            connection.execute(
                """
                insert into process_snapshots(project_id, state_json, version, updated_at)
                values (?, ?, ?, ?)
                """,
                (
                    manifest.project_id,
                    json_dumps(_process_to_dict(initial_state.process)),
                    initial_state.process.version,
                    initial_state.process.updated_at,
                ),
            )
            for event in bootstrap_events:
                connection.execute(
                    """
                    insert into state_events(
                      project_id, layer, version, patch_type, payload_json,
                      actor, reason, created_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.project_id,
                        event.layer,
                        event.version,
                        event.patch_type,
                        json_dumps(event.payload),
                        event.actor,
                        event.reason,
                        event.created_at,
                    ),
                )
            connection.commit()

    def load_manifest(self, workspace: Path) -> ProjectManifest:
        manifest_path = workspace / self.MANIFEST_FILENAME
        if not manifest_path.exists():
            raise NotFoundError(f"Workspace manifest not found: {manifest_path}")
        raw = json_loads(manifest_path.read_text(encoding="utf-8"))
        history = raw.get("objective_history", [])
        if not isinstance(history, list):
            raise ValueError(
                f"manifest 'objective_history' must be a list, got {type(history).__name__}"
            )
        return ProjectManifest(
            project_id=raw["project_id"],
            name=raw["name"],
            objective_ref=raw["objective_ref"],
            business_request=raw["business_request"],
            created_at=raw["created_at"],
            objective_history=tuple(str(item) for item in history),
        )

    @_serialized_write
    def update_manifest(self, workspace: Path, manifest: ProjectManifest) -> None:
        """Перезаписать ``project.json`` новым manifest'ом.

        Используется ``project_service.activate_next_objective`` при смене
        активного objective'а. Остальные поля manifest'а (project_id, name,
        business_request, created_at) должны оставаться неизменными — это
        контрактные сигналы для UI и API.
        """
        manifest_path = workspace / self.MANIFEST_FILENAME
        if not manifest_path.exists():
            raise NotFoundError(f"Workspace manifest not found: {manifest_path}")
        manifest_path.write_text(json_dumps(manifest), encoding="utf-8")

    def load_knowledge(self, workspace: Path) -> ProjectKnowledge:
        with self._connect(workspace) as connection:
            row = connection.execute(
                "select state_json from knowledge_snapshots limit 1"
            ).fetchone()
        if row is None:
            raise NotFoundError("Knowledge snapshot not found.")
        return _knowledge_from_dict(json_loads(row["state_json"]))

    def load_process_state(self, workspace: Path) -> ProcessState:
        with self._connect(workspace) as connection:
            row = connection.execute(
                "select state_json from process_snapshots limit 1"
            ).fetchone()
        if row is None:
            raise NotFoundError("Process snapshot not found.")
        return _process_from_dict(json_loads(row["state_json"]))

    def load_project_state(self, workspace: Path) -> ProjectState:
        """Композитный снимок: manifest + knowledge + process."""
        return ProjectState(
            manifest=self.load_manifest(workspace),
            knowledge=self.load_knowledge(workspace),
            process=self.load_process_state(workspace),
        )

    def list_state_events(
        self,
        workspace: Path,
        *,
        layer: StateLayer | None = None,
    ) -> list[StateEvent]:
        """История событий изменения состояния, опционально по слою."""
        query = (
            "select layer, version, patch_type, payload_json, actor, reason, created_at "
            "from state_events"
        )
        params: tuple = ()
        if layer is not None:
            query += " where layer = ?"
            params = (layer,)
        query += " order by id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            StateEvent(
                layer=row["layer"],
                version=row["version"],
                patch_type=row["patch_type"],
                payload=json_loads(row["payload_json"]),
                actor=row["actor"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @_serialized_write
    def apply_knowledge_patch(
        self,
        workspace: Path,
        patch: KnowledgePatch,
        actor: str,
        reason: str,
    ) -> ProjectKnowledge:
        """Применить патч к слою знаний; обновить snapshot и записать событие."""
        knowledge = self.load_knowledge(workspace)
        next_knowledge = apply_knowledge_patch(knowledge, patch)
        manifest = self.load_manifest(workspace)
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update knowledge_snapshots set state_json = ?, version = ?, updated_at = ?
                where project_id = ?
                """,
                (
                    json_dumps(_knowledge_to_dict(next_knowledge)),
                    next_knowledge.version,
                    next_knowledge.updated_at,
                    manifest.project_id,
                ),
            )
            connection.execute(
                """
                insert into state_events(
                  project_id, layer, version, patch_type, payload_json,
                  actor, reason, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.project_id,
                    "knowledge",
                    next_knowledge.version,
                    type(patch).__name__,
                    json_dumps(to_primitive(patch)),
                    actor,
                    reason,
                    next_knowledge.updated_at,
                ),
            )
            connection.commit()
        return next_knowledge

    @_serialized_write
    def apply_process_patch(
        self,
        workspace: Path,
        patch: ProcessPatch,
        actor: str,
        reason: str,
    ) -> ProcessState:
        """Применить патч к слою процесса; обновить snapshot и записать событие."""
        state = self.load_process_state(workspace)
        next_state = apply_process_patch(state, patch)
        manifest = self.load_manifest(workspace)
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update process_snapshots set state_json = ?, version = ?, updated_at = ?
                where project_id = ?
                """,
                (
                    json_dumps(_process_to_dict(next_state)),
                    next_state.version,
                    next_state.updated_at,
                    manifest.project_id,
                ),
            )
            connection.execute(
                """
                insert into state_events(
                  project_id, layer, version, patch_type, payload_json,
                  actor, reason, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.project_id,
                    "process",
                    next_state.version,
                    type(patch).__name__,
                    json_dumps(to_primitive(patch)),
                    actor,
                    reason,
                    next_state.updated_at,
                ),
            )
            connection.commit()
        return next_state

    @_serialized_write
    def create_task(self, workspace: Path, task: TaskRecord) -> TaskRecord:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into tasks(
                  task_id, project_id, objective_ref, parent_task_id, template_ref, template_type, title, status,
                  origin_kind, origin_ref, stable_key, depth, slot_id, attempt, error_message, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.project_id,
                    task.objective_ref,
                    task.parent_task_id,
                    task.template_ref,
                    task.template_type,
                    task.title,
                    task.status,
                    task.origin_kind,
                    task.origin_ref,
                    task.stable_key,
                    task.depth,
                    task.slot_id,
                    task.attempt,
                    task.error_message,
                    task.created_at,
                    task.updated_at,
                ),
            )
            self._insert_task_event(connection, task, "task_created", None, task.status, {"stable_key": task.stable_key})
            connection.commit()
        return task

    def list_tasks(self, workspace: Path) -> list[TaskRecord]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from tasks order by depth, created_at, task_id").fetchall()
        return [_task_from_row(row) for row in rows]

    def get_task(self, workspace: Path, task_id: str) -> TaskRecord:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from tasks where task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return _task_from_row(row)

    def find_task_by_stable_key(self, workspace: Path, stable_key: str) -> TaskRecord | None:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from tasks where stable_key = ?", (stable_key,)).fetchone()
        return None if row is None else _task_from_row(row)

    @_serialized_write
    def transition_task(
        self,
        workspace: Path,
        task_id: str,
        command: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> TaskRecord:
        task = self.get_task(workspace, task_id)
        error_message = None
        if payload:
            raw_error = payload.get("error_message") or payload.get("reason")
            error_message = str(raw_error) if raw_error else None
        next_task = apply_task_command(task, command, error_message=error_message)
        if next_task == task:
            return task
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update tasks set status = ?, attempt = ?, error_message = ?, updated_at = ?
                where task_id = ?
                """,
                (next_task.status, next_task.attempt, next_task.error_message, next_task.updated_at, next_task.task_id),
            )
            self._insert_task_event(connection, next_task, command, task.status, next_task.status, payload or {})
            connection.commit()
        return next_task

    def list_task_events(self, workspace: Path, task_id: str | None = None) -> list[TaskEvent]:
        query = """
            select task_id, event_type, from_status, to_status, payload_json, created_at
            from task_events
        """
        params: tuple[object, ...] = ()
        if task_id is not None:
            query += " where task_id = ?"
            params = (task_id,)
        query += " order by created_at, id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            TaskEvent(
                task_id=row["task_id"],
                event_type=row["event_type"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                payload=json_loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @_serialized_write
    def record_planning_decision(self, workspace: Path, decision: PlanningDecision) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into planning_decisions(project_id, created_at, decision_json)
                values (?, ?, ?)
                """,
                (decision.project_id, decision.created_at or utc_now_iso(), json_dumps(to_primitive(decision))),
            )
            connection.commit()

    def list_planning_decisions(self, workspace: Path) -> list[PlanningDecision]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select decision_json from planning_decisions order by id").fetchall()
        decisions = []
        for row in rows:
            payload = json_loads(row["decision_json"])
            candidates = []
            for candidate_raw in payload.get("candidates", []):
                checks = tuple(AdmissionCheck(**check_raw) for check_raw in candidate_raw.get("checks", []))
                candidates.append(
                    CandidateEvaluation(
                        task_id=candidate_raw["task_id"],
                        task_key=candidate_raw["task_key"],
                        title=candidate_raw["title"],
                        template_ref=candidate_raw["template_ref"],
                        admissible=candidate_raw["admissible"],
                        score=candidate_raw["score"],
                        checks=checks,
                        reasons=tuple(candidate_raw.get("reasons", [])),
                    )
                )
            decisions.append(
                PlanningDecision(
                    decision_id=payload["decision_id"],
                    project_id=payload["project_id"],
                    objective_ref=payload["objective_ref"],
                    mode=payload["mode"],
                    outcome=payload["outcome"],
                    selected_task_id=payload.get("selected_task_id"),
                    selected_task_key=payload.get("selected_task_key"),
                    selected_template_ref=payload.get("selected_template_ref"),
                    admitted_task_ids=tuple(payload.get("admitted_task_ids", [])),
                    blocked_task_summaries=tuple(payload.get("blocked_task_summaries", [])),
                    ranking_strategy=payload.get("ranking_strategy", "deterministic"),
                    candidates=tuple(candidates),
                    reasons=tuple(payload.get("reasons", [])),
                    created_at=payload.get("created_at", ""),
                )
            )
        return decisions

    @_serialized_write
    def store_artifact(self, workspace: Path, *, artifact: ArtifactRecord, content: str) -> ArtifactRecord:
        artifact_path = workspace / artifact.storage_path
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into artifacts(
                  artifact_id, project_id, artifact_role, title, description, artifact_format, artifact_kind,
                  created_by_task_id, parent_artifact_id, input_artifact_ids_json, child_artifact_ids_json,
                  metadata_json, storage_path, created_at, is_superseded
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.project_id,
                    artifact.artifact_role,
                    artifact.title,
                    artifact.description,
                    artifact.artifact_format,
                    artifact.artifact_kind,
                    artifact.created_by_task_id,
                    artifact.relations.parent_artifact_id,
                    json_dumps(list(artifact.relations.input_artifact_ids)),
                    json_dumps(list(artifact.relations.child_artifact_ids)),
                    json_dumps(_artifact_metadata_to_payload(artifact.metadata)),
                    artifact.storage_path,
                    artifact.created_at,
                    1 if artifact.is_superseded else 0,
                ),
            )
            connection.commit()
        return artifact

    @_serialized_write
    def mark_artifact_superseded(self, workspace: Path, artifact_id: str) -> None:
        """B4: помечает артефакт устаревшим (заменён новой версией).
        Используется при retry-task создании новой версии того же role.
        """
        with self._connect(workspace) as connection:
            connection.execute(
                "update artifacts set is_superseded = 1 where artifact_id = ?",
                (artifact_id,),
            )
            connection.commit()

    def latest_active_artifact_by_role_and_task(
        self,
        workspace: Path,
        *,
        artifact_role: str,
        created_by_task_id: str,
    ) -> ArtifactRecord | None:
        """B4: ищет существующий не-superseded artifact того же role,
        созданный той же задачей. Используется при retry чтобы:
        1) указать его id как parent_artifact_id новой версии;
        2) пометить старый как superseded.
        """
        with self._connect(workspace) as connection:
            row = connection.execute(
                """
                select * from artifacts
                where artifact_role = ?
                  and created_by_task_id = ?
                  and (is_superseded is null or is_superseded = 0)
                order by created_at desc, artifact_id desc
                limit 1
                """,
                (artifact_role, created_by_task_id),
            ).fetchone()
        return None if row is None else _artifact_from_row(row)

    def load_artifact(self, workspace: Path, artifact_id: str) -> ArtifactRecord:
        with self._connect(workspace) as connection:
            row = connection.execute("select * from artifacts where artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Artifact not found: {artifact_id}")
        return _artifact_from_row(row)

    def load_artifact_content(self, workspace: Path, artifact_id: str) -> str:
        artifact = self.load_artifact(workspace, artifact_id)
        artifact_path = workspace / artifact.storage_path
        if not artifact_path.exists():
            raise NotFoundError(f"Artifact content not found: {artifact.storage_path}")
        return artifact_path.read_text(encoding="utf-8")

    def list_artifacts(self, workspace: Path, artifact_role: str | None = None) -> list[ArtifactRecord]:
        query = "select * from artifacts"
        params: tuple[object, ...] = ()
        if artifact_role is not None:
            query += " where artifact_role = ?"
            params = (artifact_role,)
        query += " order by created_at, artifact_id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def latest_artifact_by_role(self, workspace: Path, artifact_role: str) -> ArtifactRecord | None:
        with self._connect(workspace) as connection:
            row = connection.execute(
                """
                select * from artifacts
                where artifact_role = ?
                order by created_at desc, artifact_id desc
                limit 1
                """,
                (artifact_role,),
            ).fetchone()
        return None if row is None else _artifact_from_row(row)

    # --- Этап 1.3: graph traversal ------------------------------------------

    def downstream_artifacts(
        self, workspace: Path, artifact_id: str
    ) -> list[ArtifactRecord]:
        """Все артефакты, прямо или транзитивно использовавшие данный.

        Обход вниз по графу через ``input_artifact_ids_json``. Возвращает
        артефакты в порядке обнаружения; цикла быть не должно (artifacts
        immutable + parent/inputs указывают только на ранее созданные),
        но защита от него на всякий случай.
        """
        visited: set[str] = set()
        result: list[ArtifactRecord] = []
        frontier: list[str] = [artifact_id]
        artifacts = self.list_artifacts(workspace)
        index = {a.artifact_id: a for a in artifacts}
        # Обратный индекс: input → набор downstream
        downstream_index: dict[str, list[str]] = {}
        for artifact in artifacts:
            for input_id in artifact.relations.input_artifact_ids:
                downstream_index.setdefault(input_id, []).append(artifact.artifact_id)
        while frontier:
            current = frontier.pop()
            for child_id in downstream_index.get(current, ()):
                if child_id in visited:
                    continue
                visited.add(child_id)
                child = index.get(child_id)
                if child is not None:
                    result.append(child)
                    frontier.append(child_id)
        return result

    def upstream_artifacts(
        self, workspace: Path, artifact_id: str
    ) -> list[ArtifactRecord]:
        """Все артефакты, прямо или транзитивно использованные как входы.

        Обход вверх по графу через ``input_artifact_ids``.
        """
        visited: set[str] = set()
        result: list[ArtifactRecord] = []
        artifacts = self.list_artifacts(workspace)
        index = {a.artifact_id: a for a in artifacts}
        start = index.get(artifact_id)
        if start is None:
            return []
        frontier: list[str] = list(start.relations.input_artifact_ids)
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            artifact = index.get(current)
            if artifact is None:
                continue
            result.append(artifact)
            frontier.extend(artifact.relations.input_artifact_ids)
        return result

    def artifacts_using_position(
        self, workspace: Path, position_id: str
    ) -> list[ArtifactRecord]:
        """Артефакты, явно использовавшие данное положение Layer A.

        Опирается на ``ArtifactMetadata.used_position_ids``. Возвращает
        активные артефакты (не superseded) по умолчанию — это они
        затрагиваются при оспаривании положения.
        """
        return [
            artifact
            for artifact in self.list_artifacts(workspace)
            if position_id in artifact.metadata.used_position_ids
            and not artifact.is_superseded
        ]

    @_serialized_write
    def record_context_manifest(self, workspace: Path, manifest: ContextManifest) -> ContextManifest:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into context_manifests(
                  manifest_id, project_id, task_id, template_ref, problem_state_version, budget_json,
                  excluded_items_json, input_fingerprint, created_at, used_position_ids_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.project_id,
                    manifest.task_id,
                    manifest.template_ref,
                    manifest.problem_state_version,
                    json_dumps(manifest.budget),
                    json_dumps(list(manifest.excluded_items)),
                    manifest.input_fingerprint,
                    manifest.created_at,
                    json_dumps(list(manifest.used_position_ids)),
                ),
            )
            for item in manifest.items:
                connection.execute(
                    """
                    insert into context_manifest_items(
                      item_id, manifest_id, item_type, source_ref, title, content,
                      token_estimate, required, priority
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.item_id,
                        manifest.manifest_id,
                        item.item_type,
                        item.source_ref,
                        item.title,
                        item.content,
                        item.token_estimate,
                        int(item.required),
                        item.priority,
                    ),
                )
            connection.commit()
        return manifest

    def load_context_manifest(self, workspace: Path, manifest_id: str) -> ContextManifest:
        with self._connect(workspace) as connection:
            manifest_row = connection.execute("select * from context_manifests where manifest_id = ?", (manifest_id,)).fetchone()
            if manifest_row is None:
                raise NotFoundError(f"Context manifest not found: {manifest_id}")
            item_rows = connection.execute(
                """
                select * from context_manifest_items
                where manifest_id = ?
                order by required desc, priority desc, item_id
                """,
                (manifest_id,),
            ).fetchall()
        try:
            used_positions_json = manifest_row["used_position_ids_json"]
        except (KeyError, IndexError):
            used_positions_json = None
        used_position_ids = tuple(json_loads(used_positions_json)) if used_positions_json else ()
        return ContextManifest(
            manifest_id=manifest_row["manifest_id"],
            project_id=manifest_row["project_id"],
            task_id=manifest_row["task_id"],
            template_ref=manifest_row["template_ref"],
            problem_state_version=manifest_row["problem_state_version"],
            budget=ContextBudget(**json_loads(manifest_row["budget_json"])),
            items=tuple(_context_item_from_row(row) for row in item_rows),
            excluded_items=tuple(json_loads(manifest_row["excluded_items_json"])),
            input_fingerprint=manifest_row["input_fingerprint"],
            created_at=manifest_row["created_at"],
            used_position_ids=used_position_ids,
        )

    def list_context_manifests(self, workspace: Path) -> list[ContextManifest]:
        with self._connect(workspace) as connection:
            manifest_rows = connection.execute("select * from context_manifests order by created_at, manifest_id").fetchall()
            item_rows = connection.execute("select * from context_manifest_items order by manifest_id, required desc, priority desc, item_id").fetchall()
        items_by_manifest: dict[str, list[ContextItem]] = {}
        for row in item_rows:
            items_by_manifest.setdefault(row["manifest_id"], []).append(_context_item_from_row(row))
        result: list[ContextManifest] = []
        for row in manifest_rows:
            try:
                used_positions_json = row["used_position_ids_json"]
            except (KeyError, IndexError):
                used_positions_json = None
            used_position_ids = (
                tuple(json_loads(used_positions_json)) if used_positions_json else ()
            )
            result.append(
                ContextManifest(
                    manifest_id=row["manifest_id"],
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    template_ref=row["template_ref"],
                    problem_state_version=row["problem_state_version"],
                    budget=ContextBudget(**json_loads(row["budget_json"])),
                    items=tuple(items_by_manifest.get(row["manifest_id"], [])),
                    excluded_items=tuple(json_loads(row["excluded_items_json"])),
                    input_fingerprint=row["input_fingerprint"],
                    created_at=row["created_at"],
                    used_position_ids=used_position_ids,
                )
            )
        return result

    @_serialized_write
    def record_execution_run(
        self,
        workspace: Path,
        *,
        request: ExecutionRequest,
        result: ExecutionResult,
        traces: tuple[ExecutionTrace, ...],
    ) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into execution_runs(
                  execution_run_id, project_id, task_id, template_ref, provider, model, context_manifest_id,
                  actor, status, output_artifact_ids_json, trace_ids_json, failure_code, failure_message, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.execution_run_id,
                    request.project_id,
                    request.task_id,
                    request.template_ref,
                    request.provider,
                    request.model,
                    request.context_manifest_id,
                    request.actor,
                    result.status,
                    json_dumps([output.artifact_id for output in result.outputs]),
                    json_dumps(list(result.trace_ids)),
                    result.failure_code,
                    result.failure_message,
                    utc_now_iso(),
                ),
            )
            for trace in traces:
                connection.execute(
                    """
                    insert into execution_traces(trace_id, execution_run_id, trace_type, title, content, created_at)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (trace.trace_id, request.execution_run_id, trace.trace_type, trace.title, trace.content, utc_now_iso()),
                )
            connection.commit()

    def list_execution_runs(self, workspace: Path) -> list[dict[str, object]]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from execution_runs order by created_at, execution_run_id").fetchall()
        return [dict(row) for row in rows]

    def list_execution_traces(self, workspace: Path, execution_run_id: str | None = None) -> list[dict[str, object]]:
        query = "select * from execution_traces"
        params: tuple[object, ...] = ()
        if execution_run_id is not None:
            query += " where execution_run_id = ?"
            params = (execution_run_id,)
        query += " order by created_at, trace_id"
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    @_serialized_write
    def record_validation_run(self, workspace: Path, run: ValidationRun) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into validation_runs(
                  validation_run_id, project_id, task_id, execution_run_id, status, findings_json,
                  clarification_candidate_ids_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.validation_run_id,
                    run.project_id,
                    run.task_id,
                    run.execution_run_id,
                    run.status,
                    json_dumps(run.findings),
                    json_dumps(run.clarification_candidate_ids),
                    run.created_at,
                ),
            )
            connection.commit()

    def list_validation_runs(self, workspace: Path) -> list[ValidationRun]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from validation_runs order by created_at, validation_run_id").fetchall()
        runs: list[ValidationRun] = []
        for row in rows:
            findings = tuple(ValidationFinding(**finding) for finding in json_loads(row["findings_json"]))
            runs.append(
                ValidationRun(
                    validation_run_id=row["validation_run_id"],
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    execution_run_id=row["execution_run_id"],
                    status=row["status"],
                    findings=findings,
                    clarification_candidate_ids=tuple(json_loads(row["clarification_candidate_ids_json"] or "[]")),
                    created_at=row["created_at"],
                )
            )
        return runs

    @_serialized_write
    def record_escalation_ticket(self, workspace: Path, ticket: EscalationTicket) -> None:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into escalation_tickets(
                  escalation_ticket_id, project_id, task_id, reason_code, severity, blocking, summary,
                  details_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.escalation_ticket_id,
                    ticket.project_id,
                    ticket.task_id,
                    ticket.reason_code,
                    ticket.severity,
                    int(ticket.blocking),
                    ticket.summary,
                    json_dumps(ticket.details),
                    ticket.created_at,
                ),
            )
            connection.commit()

    # ---- workflow runs (W4.1 R1: async run-until-blocked) ----------------

    @_serialized_write
    def create_workflow_run(self, workspace: Path, run: WorkflowRunRecord) -> WorkflowRunRecord:
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into workflow_runs(
                  run_id, project_id, status, provider, model, max_steps,
                  current_step, total_steps_completed,
                  started_at, finished_at, last_step_summary, stop_reason, error_message,
                  cancel_requested, steps_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.project_id,
                    run.status,
                    run.provider,
                    run.model,
                    int(run.max_steps),
                    int(run.current_step),
                    int(run.total_steps_completed),
                    run.started_at,
                    run.finished_at,
                    run.last_step_summary,
                    run.stop_reason,
                    run.error_message,
                    int(run.cancel_requested),
                    json_dumps([self._step_to_dict(s) for s in run.steps]),
                ),
            )
            connection.commit()
        return run

    @_serialized_write
    def update_workflow_run(self, workspace: Path, run: WorkflowRunRecord) -> WorkflowRunRecord:
        # ВАЖНО (параллельный режим): cancel_requested НЕ пишется здесь.
        # Этим флагом владеет только request_workflow_cancel (его дёргает
        # HTTP-поток отмены). Прогресс-обновления ранера делают read-modify-
        # write через get_workflow_run (без лока) + update, и записывать сюда
        # cancel_requested из устаревшего снимка ранера означало бы затереть
        # флаг, поставленный отменой между get и update (lost update). Поэтому
        # колонку cancel_requested оставляем как есть в БД.
        with self._connect(workspace) as connection:
            connection.execute(
                """
                update workflow_runs set
                  status = ?,
                  current_step = ?,
                  total_steps_completed = ?,
                  finished_at = ?,
                  last_step_summary = ?,
                  stop_reason = ?,
                  error_message = ?,
                  steps_json = ?
                where run_id = ?
                """,
                (
                    run.status,
                    int(run.current_step),
                    int(run.total_steps_completed),
                    run.finished_at,
                    run.last_step_summary,
                    run.stop_reason,
                    run.error_message,
                    json_dumps([self._step_to_dict(s) for s in run.steps]),
                    run.run_id,
                ),
            )
            connection.commit()
        return run

    @_serialized_write
    def request_workflow_cancel(self, workspace: Path, run_id: str) -> bool:
        """Идемпотентно ставит cancel_requested=1. Возвращает True, если
        строка с таким run_id существует."""
        with self._connect(workspace) as connection:
            cursor = connection.execute(
                "update workflow_runs set cancel_requested = 1 where run_id = ?",
                (run_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def get_workflow_run(self, workspace: Path, run_id: str) -> WorkflowRunRecord | None:
        with self._connect(workspace) as connection:
            row = connection.execute(
                "select * from workflow_runs where run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return self._workflow_run_from_row(row)

    def list_workflow_runs(
        self,
        workspace: Path,
        *,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[WorkflowRunRecord]:
        query = "select * from workflow_runs"
        params: tuple[object, ...] = ()
        if project_id is not None:
            query += " where project_id = ?"
            params = (project_id,)
        query += " order by started_at desc limit ?"
        params = (*params, int(limit))
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._workflow_run_from_row(row) for row in rows]

    def latest_active_workflow_run(self, workspace: Path, project_id: str) -> WorkflowRunRecord | None:
        with self._connect(workspace) as connection:
            row = connection.execute(
                """
                select * from workflow_runs
                where project_id = ? and status in ('pending', 'running')
                order by started_at desc limit 1
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return self._workflow_run_from_row(row)

    @staticmethod
    def _step_to_dict(step: WorkflowStepRecord) -> dict:
        return {
            "sequence": step.sequence,
            "task_id": step.task_id,
            "task_key": step.task_key,
            "selected_step_id": step.selected_step_id,
            "planning_outcome": step.planning_outcome,
            "validation_status": step.validation_status,
            "execution_run_id": step.execution_run_id,
            "started_at": step.started_at,
            "finished_at": step.finished_at,
            "error_message": step.error_message,
        }

    @staticmethod
    def _workflow_run_from_row(row: sqlite3.Row) -> WorkflowRunRecord:
        from ..common.serialization import json_loads as _json_loads

        steps_raw = _json_loads(row["steps_json"]) if row["steps_json"] else []
        steps = tuple(
            WorkflowStepRecord(
                sequence=int(item.get("sequence", 0)),
                task_id=item.get("task_id"),
                task_key=item.get("task_key"),
                selected_step_id=item.get("selected_step_id"),
                planning_outcome=str(item.get("planning_outcome", "")),
                validation_status=item.get("validation_status"),
                execution_run_id=item.get("execution_run_id"),
                started_at=str(item.get("started_at", "")),
                finished_at=str(item.get("finished_at", "")),
                error_message=item.get("error_message"),
            )
            for item in steps_raw
            if isinstance(item, dict)
        )
        status: WorkflowRunStatus = row["status"]
        return WorkflowRunRecord(
            run_id=row["run_id"],
            project_id=row["project_id"],
            status=status,
            provider=row["provider"],
            model=row["model"],
            max_steps=int(row["max_steps"]),
            current_step=int(row["current_step"]),
            total_steps_completed=int(row["total_steps_completed"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            last_step_summary=row["last_step_summary"] or "",
            stop_reason=row["stop_reason"],
            error_message=row["error_message"],
            cancel_requested=bool(row["cancel_requested"]),
            steps=steps,
        )

    def list_escalations(self, workspace: Path) -> list[EscalationTicket]:
        with self._connect(workspace) as connection:
            rows = connection.execute("select * from escalation_tickets order by created_at, escalation_ticket_id").fetchall()
        return [
            EscalationTicket(
                escalation_ticket_id=row["escalation_ticket_id"],
                project_id=row["project_id"],
                task_id=row["task_id"],
                reason_code=row["reason_code"],
                severity=row["severity"],
                blocking=bool(row["blocking"]),
                summary=row["summary"],
                details=json_loads(row["details_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ---- CheckpointSession (v3.0) -----------------------------------------
    #
    # Сессии чекпоинта — крошечная сущность, в основном связь
    # task ↔ decision_ids ↔ status. Хранится отдельной таблицей, потому что
    # жизненный цикл независим от Decision (статус сессии меняется
    # атомарно, не привязан к статусу отдельных решений).

    @_serialized_write
    def upsert_checkpoint_session(
        self, workspace: Path, session: CheckpointSession
    ) -> CheckpointSession:
        """Создать или обновить сессию. Идемпотентно по session_id."""
        now = utc_now_iso()
        created_at = session.created_at or now
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into checkpoint_sessions (
                    session_id, project_id, task_id, task_title, artifact_role,
                    status, decision_ids_json, created_at, finalized_at, finalized_by
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(session_id) do update set
                    status = excluded.status,
                    decision_ids_json = excluded.decision_ids_json,
                    finalized_at = excluded.finalized_at,
                    finalized_by = excluded.finalized_by
                """,
                (
                    session.session_id,
                    session.project_id,
                    session.task_id,
                    session.task_title,
                    session.artifact_role,
                    session.status,
                    json_dumps(list(session.decision_ids)),
                    created_at,
                    session.finalized_at,
                    session.finalized_by,
                ),
            )
            connection.commit()
        return replace(session, created_at=created_at)

    def get_checkpoint_session(
        self, workspace: Path, session_id: str
    ) -> CheckpointSession:
        with self._connect(workspace) as connection:
            row = connection.execute(
                "select * from checkpoint_sessions where session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"checkpoint session {session_id!r} не найдена")
        return _checkpoint_from_row(row)

    def list_checkpoint_sessions(
        self,
        workspace: Path,
        *,
        project_id: str,
        status: CheckpointStatus | None = None,
    ) -> list[CheckpointSession]:
        """Сессии проекта; по умолчанию все, опционально фильтр по статусу."""
        where = ["project_id = ?"]
        params: list[object] = [project_id]
        if status is not None:
            where.append("status = ?")
            params.append(status)
        query = (
            "select * from checkpoint_sessions where "
            + " and ".join(where)
            + " order by created_at desc, rowid desc"
        )
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [_checkpoint_from_row(row) for row in rows]

    def find_pending_checkpoint_for_task(
        self, workspace: Path, task_id: str
    ) -> CheckpointSession | None:
        """Активная сессия по задаче, если есть.

        Per-task one-pending инвариант: задача не должна одновременно
        иметь две pending-сессии. Если возникнет — сигнал к расследованию,
        здесь возвращаем самую свежую.
        """
        with self._connect(workspace) as connection:
            row = connection.execute(
                """
                select * from checkpoint_sessions
                where task_id = ? and status = 'pending'
                order by created_at desc, rowid desc
                limit 1
                """,
                (task_id,),
            ).fetchone()
        return _checkpoint_from_row(row) if row is not None else None

    # ---- Decision ledger (v3.0) ------------------------------------------
    #
    # CRUD-методы для реестра решений: list/get/upsert + helpers для
    # фильтрации. Хранилище — единственный source of truth, никакого
    # in-memory кэша.

    @_serialized_write
    def upsert_decision(self, workspace: Path, decision: Decision) -> Decision:
        """Создать или обновить запись о решении.

        Идемпотентно по ``decision_id``. Поле ``updated_at`` перезаписывается
        текущим временем; ``created_at`` сохраняется при апдейте (insert
        выставляет, если пусто).
        """
        now = utc_now_iso()
        created_at = decision.created_at or now
        payload = _decision_to_row(decision, created_at=created_at, updated_at=now)
        with self._connect(workspace) as connection:
            connection.execute(
                """
                insert into decisions (
                    decision_id, project_id, title, description, chosen_option_id,
                    alternatives_json, rationale, level, level_rationale, confidence,
                    status, source, source_task_id,
                    affected_artifact_ids_json, depends_on_decision_ids_json,
                    user_action, original_chosen_option_id, user_free_text_answer,
                    free_form_level_override, created_at, updated_at,
                    category, answer_mode, chosen_option_ids_json,
                    user_verified, user_verified_at
                )
                values (
                    :decision_id, :project_id, :title, :description, :chosen_option_id,
                    :alternatives_json, :rationale, :level, :level_rationale, :confidence,
                    :status, :source, :source_task_id,
                    :affected_artifact_ids_json, :depends_on_decision_ids_json,
                    :user_action, :original_chosen_option_id, :user_free_text_answer,
                    :free_form_level_override, :created_at, :updated_at,
                    :category, :answer_mode, :chosen_option_ids_json,
                    :user_verified, :user_verified_at
                )
                on conflict(decision_id) do update set
                    title = excluded.title,
                    description = excluded.description,
                    chosen_option_id = excluded.chosen_option_id,
                    alternatives_json = excluded.alternatives_json,
                    rationale = excluded.rationale,
                    level = excluded.level,
                    level_rationale = excluded.level_rationale,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    source = excluded.source,
                    source_task_id = excluded.source_task_id,
                    affected_artifact_ids_json = excluded.affected_artifact_ids_json,
                    depends_on_decision_ids_json = excluded.depends_on_decision_ids_json,
                    user_action = excluded.user_action,
                    original_chosen_option_id = excluded.original_chosen_option_id,
                    user_free_text_answer = excluded.user_free_text_answer,
                    free_form_level_override = excluded.free_form_level_override,
                    updated_at = excluded.updated_at,
                    category = excluded.category,
                    answer_mode = excluded.answer_mode,
                    chosen_option_ids_json = excluded.chosen_option_ids_json,
                    user_verified = excluded.user_verified,
                    user_verified_at = excluded.user_verified_at
                """,
                payload,
            )
            connection.commit()
        return replace(
            decision,
            description=str(payload["description"]),
            category=str(payload["category"]),
            created_at=created_at,
            updated_at=now,
        )

    def get_decision(self, workspace: Path, decision_id: str) -> Decision:
        """Достать одно решение по id. Бросает NotFoundError при отсутствии."""
        with self._connect(workspace) as connection:
            row = connection.execute(
                "select * from decisions where decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"decision {decision_id!r} не найдено")
        return _decision_from_row(row)

    def list_decisions(
        self,
        workspace: Path,
        *,
        project_id: str,
        level: DecisionLevel | None = None,
        status: DecisionStatus | None = None,
        source: DecisionSource | None = None,
        source_task_id: str | None = None,
    ) -> list[Decision]:
        """Получить отфильтрованный список решений проекта.

        Все фильтры опциональны и AND-комбинируются. Результат отсортирован
        по ``created_at asc`` (стабильный порядок появления), что позволяет
        UI показывать решения в хронологии работы LLM.
        """
        where_clauses = ["project_id = ?"]
        params: list[object] = [project_id]
        if level is not None:
            where_clauses.append("level = ?")
            params.append(level)
        if status is not None:
            where_clauses.append("status = ?")
            params.append(status)
        if source is not None:
            where_clauses.append("source = ?")
            params.append(source)
        if source_task_id is not None:
            where_clauses.append("source_task_id = ?")
            params.append(source_task_id)
        query = (
            "select * from decisions where "
            + " and ".join(where_clauses)
            + " order by created_at asc, rowid asc"
        )
        with self._connect(workspace) as connection:
            rows = connection.execute(query, params).fetchall()
        return [_decision_from_row(row) for row in rows]

    def count_decisions(
        self,
        workspace: Path,
        *,
        project_id: str,
        level: DecisionLevel | None = None,
        status: DecisionStatus | None = None,
    ) -> int:
        """Подсчёт решений с теми же фильтрами что у list_decisions.

        Нужно для UI-badge'ей («N решений на твоём уровне ждут»).
        Реализован отдельным методом, чтобы не тянуть весь датасет в
        память ради счёта.
        """
        where_clauses = ["project_id = ?"]
        params: list[object] = [project_id]
        if level is not None:
            where_clauses.append("level = ?")
            params.append(level)
        if status is not None:
            where_clauses.append("status = ?")
            params.append(status)
        query = "select count(*) as cnt from decisions where " + " and ".join(where_clauses)
        with self._connect(workspace) as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def _insert_task_event(
        self,
        connection: sqlite3.Connection,
        task: TaskRecord,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            """
            insert into task_events(task_id, project_id, event_type, from_status, to_status, payload_json, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (task.task_id, task.project_id, event_type, from_status, to_status, json_dumps(payload), utc_now_iso()),
        )

    @contextmanager
    def _connect(self, workspace: Path):
        workspace.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(workspace / self.DB_FILENAME, timeout=5.0)
        connection.row_factory = sqlite3.Row
        # Pragma'ы под КОНКУРЕНТНЫЙ доступ (параллельные шаги workflow):
        #   - journal_mode=MEMORY + synchronous=OFF: быстрый single-writer
        #     режим (как и было). Корректность при параллельных шагах даёт НЕ
        #     журнал, а write-coordinator: per-workspace лок (@_serialized_write)
        #     гарантирует ровно одного писателя на workspace в любой момент,
        #     а read-modify-write выполняется под этим же локом целиком.
        #     (WAL давал ~3× замедление тест-сьюта на множестве мелких БД и
        #     при наличии лока не нужен.)
        #   - busy_timeout=5000: пока writer держит файловую блокировку,
        #     конкурентные ЧИТАТЕЛИ (WS-поллер, query-service) ждут до 5с
        #     вместо мгновенного "database is locked".
        connection.execute("PRAGMA journal_mode = MEMORY")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            # Schema идемпотентна; проверяем один раз на workspace.
            if workspace not in self._schema_ensured:
                self._ensure_schema(connection)
                self._schema_ensured.add(workspace)
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            create table if not exists knowledge_snapshots (
              project_id text primary key,
              state_json text not null,
              version integer not null,
              updated_at text not null
            );

            create table if not exists process_snapshots (
              project_id text primary key,
              state_json text not null,
              version integer not null,
              updated_at text not null
            );

            create table if not exists state_events (
              id integer primary key autoincrement,
              project_id text not null,
              layer text not null,
              version integer not null,
              patch_type text not null,
              payload_json text not null,
              actor text not null,
              reason text not null,
              created_at text not null
            );

            create table if not exists tasks (
              task_id text primary key,
              project_id text not null,
              objective_ref text not null,
              parent_task_id text,
              template_ref text not null,
              template_type text not null,
              title text not null,
              status text not null,
              origin_kind text not null,
              origin_ref text not null,
              stable_key text not null unique,
              depth integer not null,
              slot_id text,
              attempt integer not null,
              error_message text,
              created_at text not null,
              updated_at text not null
            );

            create table if not exists task_events (
              id integer primary key autoincrement,
              task_id text not null,
              project_id text not null,
              event_type text not null,
              from_status text,
              to_status text,
              payload_json text not null,
              created_at text not null
            );

            create table if not exists planning_decisions (
              id integer primary key autoincrement,
              project_id text not null,
              created_at text not null,
              decision_json text not null
            );

            create table if not exists artifacts (
              artifact_id text primary key,
              project_id text not null,
              artifact_role text not null,
              title text not null,
              description text,
              artifact_format text not null,
              artifact_kind text not null,
              created_by_task_id text,
              parent_artifact_id text,
              input_artifact_ids_json text not null default '[]',
              child_artifact_ids_json text not null default '[]',
              metadata_json text not null,
              storage_path text not null,
              created_at text not null,
              is_superseded integer not null default 0
            );

            create table if not exists context_manifests (
              manifest_id text primary key,
              project_id text not null,
              task_id text not null,
              template_ref text not null,
              problem_state_version integer not null,
              budget_json text not null,
              excluded_items_json text not null,
              input_fingerprint text not null,
              created_at text not null,
              used_position_ids_json text not null default '[]'
            );

            create table if not exists context_manifest_items (
              item_id text primary key,
              manifest_id text not null,
              item_type text not null,
              source_ref text not null,
              title text not null,
              content text not null,
              token_estimate integer not null,
              required integer not null,
              priority integer not null
            );

            create table if not exists execution_runs (
              execution_run_id text primary key,
              project_id text not null,
              task_id text not null,
              template_ref text not null,
              provider text not null,
              model text not null,
              context_manifest_id text not null,
              actor text not null,
              status text not null,
              output_artifact_ids_json text not null,
              trace_ids_json text not null,
              failure_code text,
              failure_message text,
              created_at text not null
            );

            create table if not exists execution_traces (
              trace_id text primary key,
              execution_run_id text not null,
              trace_type text not null,
              title text not null,
              content text not null,
              created_at text not null
            );

            create table if not exists validation_runs (
              validation_run_id text primary key,
              project_id text not null,
              task_id text not null,
              execution_run_id text not null,
              status text not null,
              findings_json text not null,
              clarification_candidate_ids_json text not null default '[]',
              created_at text not null
            );

            create table if not exists escalation_tickets (
              escalation_ticket_id text primary key,
              project_id text not null,
              task_id text,
              reason_code text not null,
              severity text not null,
              blocking integer not null,
              summary text not null,
              details_json text not null,
              created_at text not null
            );

            """
        )
        self._ensure_column(
            connection,
            "validation_runs",
            "clarification_candidate_ids_json",
            "text not null default '[]'",
        )
        # B4: маркер «артефакт заменён более новой версией».
        # При auto-retry задачи новый артефакт записывается, а старый
        # помечается is_superseded=1 чтобы UI L6-1 skeleton показывал
        # только current версию, а artifact_versions строил цепочку через
        # parent_artifact_id.
        self._ensure_column(
            connection,
            "artifacts",
            "is_superseded",
            "integer not null default 0",
        )
        # Этап 1.3: связи артефактов в графе вынесены в отдельные колонки —
        # input_artifact_ids (lineage по контексту), child_artifact_ids
        # (для synthesized композитных). Старые БД мигрируются дефолтом '[]'.
        self._ensure_column(
            connection,
            "artifacts",
            "input_artifact_ids_json",
            "text not null default '[]'",
        )
        self._ensure_column(
            connection,
            "artifacts",
            "child_artifact_ids_json",
            "text not null default '[]'",
        )
        # Этап 1.4: context_manifests хранит идентификаторы положений
        # Layer A, использованных при сборке контекста — это «вход» для
        # ArtifactMetadata.used_position_ids.
        self._ensure_column(
            connection,
            "context_manifests",
            "used_position_ids_json",
            "text not null default '[]'",
        )
        # v3.0 — CheckpointSession: пауза workflow для участия пользователя.
        #
        # Decision_ids хранится JSON-массивом — связь one-to-many от сессии
        # к решениям. Решения сами уже привязаны к session через source_task_id,
        # обратная связь нужна для быстрого UI-чтения «какие решения в этой
        # конкретной сессии». При появлении нагрузки можно вынести в join-таблицу.
        connection.executescript(
            """
            create table if not exists checkpoint_sessions (
              session_id text primary key,
              project_id text not null,
              task_id text not null,
              task_title text not null default '',
              artifact_role text not null default '',
              status text not null,
              decision_ids_json text not null default '[]',
              created_at text not null,
              finalized_at text,
              finalized_by text
            );
            create index if not exists checkpoint_sessions_project_status_idx
                on checkpoint_sessions(project_id, status);
            create index if not exists checkpoint_sessions_task_idx
                on checkpoint_sessions(task_id);
            """
        )

        # v3.0 — Реестр решений (decision ledger).
        #
        # См. specs/12_clarification_escalation.md раздел «v3.0 — реестр
        # решений + checkpoint» и docs/decision_level_criteria.md.
        #
        # Структурный выбор: alternatives, dependencies, affected_artifact_ids
        # храним как JSON-поля в самой строке, не вынося в отдельные таблицы.
        # На v3.0 это упрощение оправдано: реестр читается всегда целиком
        # для проекта (через filter, не через join), таблица растёт в десятки—
        # сотни записей на проект, оптимизация запросов через индексы на
        # выделенные FK не окупает усложнение схемы. При появлении нагрузки
        # на per-alternative выборки — мигрировать в отдельную таблицу.
        connection.executescript(
            """
            create table if not exists decisions (
              decision_id text primary key,
              project_id text not null,
              title text not null,
              description text not null default '',
              chosen_option_id text not null default '',
              alternatives_json text not null default '[]',
              rationale text not null default '',
              level text not null,
              level_rationale text not null default '',
              confidence real not null default 0.0,
              status text not null,
              source text not null,
              source_task_id text,
              affected_artifact_ids_json text not null default '[]',
              depends_on_decision_ids_json text not null default '[]',
              user_action text not null default 'not_shown',
              original_chosen_option_id text,
              user_free_text_answer text,
              free_form_level_override text,
              created_at text not null,
              updated_at text not null,
              category text not null default '',
              answer_mode text not null default 'single',
              chosen_option_ids_json text not null default '[]',
              user_verified integer not null default 0,
              user_verified_at text
            );
            create index if not exists decisions_project_idx
                on decisions(project_id, created_at);
            create index if not exists decisions_project_level_idx
                on decisions(project_id, level);
            create index if not exists decisions_project_status_idx
                on decisions(project_id, status);
            """
        )

        # v3.1: миграция existing БД — добавление answer_mode/chosen_option_ids
        # для legacy decisions, созданных до v3.1 (single-mode по умолчанию).
        self._ensure_column(
            connection, "decisions", "answer_mode", "text not null default 'single'"
        )
        # v3.8: category became explicit; legacy rows may still encode it as
        # "[category]" in description and are normalized lazily on read/upsert.
        self._ensure_column(
            connection, "decisions", "category", "text not null default ''"
        )
        self._ensure_column(
            connection,
            "decisions",
            "chosen_option_ids_json",
            "text not null default '[]'",
        )
        # v3.4: миграция — user_verified для legacy decisions (false).
        self._ensure_column(
            connection, "decisions", "user_verified", "integer not null default 0"
        )
        self._ensure_column(
            connection, "decisions", "user_verified_at", "text"
        )

        # W4.1 (R1): async workflow runs.
        connection.executescript(
            """
            create table if not exists workflow_runs (
              run_id text primary key,
              project_id text not null,
              status text not null,
              provider text,
              model text,
              max_steps integer not null,
              current_step integer not null default 0,
              total_steps_completed integer not null default 0,
              started_at text not null,
              finished_at text,
              last_step_summary text not null default '',
              stop_reason text,
              error_message text,
              cancel_requested integer not null default 0,
              steps_json text not null default '[]'
            );
            create index if not exists workflow_runs_project_idx
                on workflow_runs(project_id, started_at);
            """
        )

    def _ensure_column(self, connection: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        columns = {row["name"] for row in connection.execute(f"pragma table_info({table_name})").fetchall()}
        if column_name not in columns:
            connection.execute(f"alter table {table_name} add column {column_name} {ddl}")
