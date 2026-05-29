"""Сервис инициализации и операций уровня проекта.

Создаёт workspace, инициализирует двухслойное состояние (knowledge + process)
и предоставляет операторам высокоуровневые команды: задать цель, открыть/закрыть
пробел, изменить готовность, переключить методологию, активировать домен,
сменить engagement-режим.

Цель проекта (``project.goal``) живёт в Layer A как положение с
``visibility='principal'`` и ``scope='global'`` (см. roadmap, Этап 0).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from ..common.serialization import utc_now_iso
from ..domain.positions import (
    Position,
    PositionSource,
    PositionType,
    VisibilityLevel,
)
from ..domain.process_state import (
    ActivateDomainPackPatch,
    ActivateMethodologyPackPatch,
    CloseGapPatch,
    DisableMethodologyPackPatch,
    ProcessState,
    SetClarificationModePatch,
    UpsertGapPatch,
    UpsertReadinessPatch,
)
from ..domain.project_knowledge import (
    GOAL_POSITION_ID,
    ProjectKnowledge,
    UpsertPositionPatch,
    apply_knowledge_patch,
)
from ..domain.project_state import ProjectManifest, ProjectState, StateEvent
from ..domain.registry import DomainPackSpec, ObjectRef
from ..infrastructure.sqlite_runtime import SqliteRuntime

_INITIAL_REQUEST_POSITION_ID = "project.business_request"


@dataclass(frozen=True)
class ProjectBootstrap:
    """Результат инициализации проекта — манифест + начальное состояние."""

    manifest: ProjectManifest
    state: ProjectState


class ProjectService:
    """Команды уровня проекта: создание, операторские правки положений/состояния."""

    def __init__(self, runtime: SqliteRuntime) -> None:
        self._runtime = runtime

    # --- инициализация ------------------------------------------------------

    def init_project(
        self,
        workspace: Path,
        name: str,
        objective_ref: ObjectRef,
        request_text: str,
        domain_packs: tuple[DomainPackSpec, ...] = (),
        default_methodology_pack_ref: str | None = None,
    ) -> ProjectBootstrap:
        """Создать новый проект и записать его начальное состояние.

        ``default_methodology_pack_ref`` ожидается от вызывающего слоя — он
        обычно подставляет ``ObjectiveSpec.default_methodology_pack_ref`` из
        реестра (см. ``workspace_command_service.create_project`` и CLI
        ``project init``). Если поле в YAML objective не задано и caller
        ничего не передал — используем глобальный fallback
        ``process.lean_jtbd@1.0.0``.
        """
        if default_methodology_pack_ref is None:
            default_methodology_pack_ref = "process.lean_jtbd@1.0.0"
        project_id = str(uuid.uuid4())
        created_at = utc_now_iso()
        manifest = ProjectManifest(
            project_id=project_id,
            name=name,
            objective_ref=objective_ref.as_string(),
            business_request=request_text.strip(),
            created_at=created_at,
        )

        # Layer A: исходный запрос фиксируем как principal-fact.
        knowledge = ProjectKnowledge()
        knowledge = apply_knowledge_patch(
            knowledge,
            UpsertPositionPatch(
                Position(
                    identifier=_INITIAL_REQUEST_POSITION_ID,
                    type="fact",
                    statement=request_text.strip(),
                    visibility="principal",
                    scope="global",
                    source="input",
                    taken_by="system",
                    taken_at=created_at,
                    tags=("project", "input"),
                )
            ),
        )

        # Layer B: активируем доменные паки и дефолтную методологию.
        process = ProcessState()
        for pack in domain_packs:
            from ..domain.process_state import apply_process_patch

            process = apply_process_patch(
                process,
                ActivateDomainPackPatch(
                    pack_ref=pack.ref.as_string(),
                    domain=pack.domain,
                    source="bootstrap",
                    rationale="Domain pack chosen at project creation.",
                    confidence=1.0,
                ),
            )
        if default_methodology_pack_ref:
            from ..domain.process_state import apply_process_patch

            process = apply_process_patch(
                process,
                ActivateMethodologyPackPatch(
                    pack_ref=default_methodology_pack_ref,
                    source="bootstrap",
                    rationale="Default project methodology.",
                ),
            )

        state = ProjectState(manifest=manifest, knowledge=knowledge, process=process)

        bootstrap_events = (
            StateEvent(
                layer="knowledge",
                version=knowledge.version,
                patch_type="bootstrap",
                payload={"positions": list(knowledge.positions.keys())},
                actor="system",
                reason="project initialization",
                created_at=created_at,
            ),
            StateEvent(
                layer="process",
                version=process.version,
                patch_type="bootstrap",
                payload={
                    "active_domain_packs": list(process.active_domain_packs.keys()),
                    "active_methodology_packs": list(process.active_methodology_packs.keys()),
                },
                actor="system",
                reason="project initialization",
                created_at=created_at,
            ),
        )
        self._runtime.create_workspace(workspace, manifest, state, bootstrap_events)
        return ProjectBootstrap(manifest=manifest, state=state)

    # --- чтение -------------------------------------------------------------

    def load_manifest(self, workspace: Path) -> ProjectManifest:
        return self._runtime.load_manifest(workspace)

    def load_project_state(self, workspace: Path) -> ProjectState:
        return self._runtime.load_project_state(workspace)

    def state_history(self, workspace: Path) -> list[StateEvent]:
        return self._runtime.list_state_events(workspace)

    # --- операторские команды (Layer A — знания) ----------------------------

    def set_goal(
        self,
        workspace: Path,
        text: str,
        *,
        actor: str = "operator",
        reason: str = "manual goal update",
    ) -> ProjectKnowledge:
        """Зафиксировать или обновить цель проекта.

        Цель — положение типа ``fact`` с уровнем ``principal`` и
        ``scope='global'``. Стабильный id — :data:`GOAL_POSITION_ID`.
        """
        return self._upsert_position(
            workspace,
            identifier=GOAL_POSITION_ID,
            position_type="fact",
            statement=text.strip(),
            visibility="principal",
            scope="global",
            source="user" if actor == "operator" else "system",
            taken_by=actor,
            tags=("project", "goal"),
            actor=actor,
            reason=reason,
        )

    def add_fact(
        self,
        workspace: Path,
        identifier: str,
        statement: str,
        *,
        source: PositionSource = "user",
        visibility: VisibilityLevel = "architectural",
        actor: str = "operator",
        taken_by_label: str | None = None,
        reason: str = "manual fact registration",
        tags: tuple[str, ...] = (),
    ) -> ProjectKnowledge:
        """Зафиксировать факт о проекте как положение Layer A.

        ``source`` — категория источника (user/system/...).
        ``taken_by_label`` — свободная метка актора (например, имя CLI-команды
        или selector'а), сохраняется в Position.taken_by. Если не задан —
        используется ``actor``.
        """
        return self._upsert_position(
            workspace,
            identifier=identifier,
            position_type="fact",
            statement=statement,
            visibility=visibility,
            scope="global",
            source=source,
            taken_by=taken_by_label or actor,
            tags=tags,
            actor=actor,
            reason=reason,
        )

    def _upsert_position(
        self,
        workspace: Path,
        *,
        identifier: str,
        position_type: PositionType,
        statement: str,
        visibility: VisibilityLevel,
        scope: str,
        source: PositionSource,
        taken_by: str,
        tags: tuple[str, ...] = (),
        actor: str,
        reason: str,
    ) -> ProjectKnowledge:
        now = utc_now_iso()
        position = Position(
            identifier=identifier,
            type=position_type,
            statement=statement,
            visibility=visibility,
            scope=scope,  # type: ignore[arg-type]
            source=source,
            taken_by=taken_by,
            taken_at=now,
            tags=tags,
        )
        return self._runtime.apply_knowledge_patch(
            workspace,
            UpsertPositionPatch(position=position),
            actor=actor,
            reason=reason,
        )

    # --- операторские команды (Layer B — процесс) ---------------------------

    def add_gap(
        self,
        workspace: Path,
        gap_id: str,
        title: str,
        description: str,
        severity: str = "medium",
        blocking: bool = True,
        *,
        actor: str = "operator",
        reason: str = "manual gap registration",
    ) -> ProcessState:
        return self._runtime.apply_process_patch(
            workspace,
            UpsertGapPatch(
                gap_id=gap_id,
                title=title,
                description=description,
                severity=severity,  # type: ignore[arg-type]
                blocking=blocking,
            ),
            actor=actor,
            reason=reason,
        )

    def close_gap(
        self,
        workspace: Path,
        gap_id: str,
        *,
        actor: str = "operator",
        reason: str = "manual gap close",
    ) -> ProcessState:
        return self._runtime.apply_process_patch(
            workspace,
            CloseGapPatch(gap_id=gap_id),
            actor=actor,
            reason=reason,
        )

    def set_readiness(
        self,
        workspace: Path,
        dimension: str,
        status: str,
        blocking: bool,
        confidence: float,
        *,
        actor: str = "operator",
        reason: str = "manual readiness update",
    ) -> ProcessState:
        return self._runtime.apply_process_patch(
            workspace,
            UpsertReadinessPatch(
                dimension=dimension,
                status=status,  # type: ignore[arg-type]
                blocking=blocking,
                confidence=confidence,
            ),
            actor=actor,
            reason=reason,
        )

    def set_clarification_mode(self, workspace: Path, mode: str) -> ProcessState:
        return self._runtime.apply_process_patch(
            workspace,
            SetClarificationModePatch(mode=mode),  # type: ignore[arg-type]
            actor="operator",
            reason="clarification mode changed",
        )

    def enable_domain_pack(
        self,
        workspace: Path,
        pack: DomainPackSpec,
        *,
        actor: str = "operator",
        reason: str = "manual domain activation",
    ) -> ProcessState:
        return self._runtime.apply_process_patch(
            workspace,
            ActivateDomainPackPatch(
                pack_ref=pack.ref.as_string(),
                domain=pack.domain,
                source="operator" if actor == "operator" else "system",
                rationale=reason,
                confidence=1.0,
            ),
            actor=actor,
            reason=reason,
        )

    def set_methodology(
        self,
        workspace: Path,
        pack_ref: str,
        *,
        actor: str = "operator",
        reason: str = "manual methodology activation",
    ) -> ProcessState:
        """Сменить активный methodology pack.

        В MVP активна не более одной методологии (PS10) — текущие активные
        отключаются перед активацией новой.
        """
        process = self._runtime.load_process_state(workspace)
        for ref, record in process.active_methodology_packs.items():
            if record.status == "active" and ref != pack_ref:
                self._runtime.apply_process_patch(
                    workspace,
                    DisableMethodologyPackPatch(pack_ref=ref),
                    actor=actor,
                    reason=f"replaced by {pack_ref}",
                )
        return self._runtime.apply_process_patch(
            workspace,
            ActivateMethodologyPackPatch(
                pack_ref=pack_ref,
                source="operator" if actor == "operator" else "system",
                rationale=reason,
            ),
            actor=actor,
            reason=reason,
        )

    # --- цепочка objective'ов -----------------------------------------------

    def activate_next_objective(
        self,
        workspace: Path,
        new_objective_ref: ObjectRef,
        *,
        default_methodology_pack_ref: str | None = None,
        actor: str = "operator",
        reason: str = "objective activation",
    ) -> ProjectManifest:
        """Активировать следующий objective в рамках того же workspace.

        Прошлый ``objective_ref`` уходит в конец ``objective_history``;
        ``objective_ref`` становится ``new_objective_ref``.
        ``knowledge`` и ``process`` состояние не сбрасываются — артефакты,
        gaps, readiness, активные доменные паки продолжают жить.

        Если ``default_methodology_pack_ref`` передан и отличается от
        текущей активной методологии — переключаем через ``set_methodology``.
        Если совпадает (или ``None``) — методология не меняется.

        Caller (CLI / API / workspace_command_service) после активации
        обязан вызвать ``planning_service.expand_graph`` — новый root task
        будет создан под новый objective автоматически (stable key по
        ``project_id + root_task_ref``).
        """
        manifest = self._runtime.load_manifest(workspace)
        new_ref_str = new_objective_ref.as_string()
        if new_ref_str == manifest.objective_ref:
            raise ValueError(
                f"Objective '{new_ref_str}' уже является активным; "
                f"нечего активировать."
            )

        updated_manifest = ProjectManifest(
            project_id=manifest.project_id,
            name=manifest.name,
            objective_ref=new_ref_str,
            business_request=manifest.business_request,
            created_at=manifest.created_at,
            objective_history=manifest.objective_history + (manifest.objective_ref,),
        )
        self._runtime.update_manifest(workspace, updated_manifest)

        if default_methodology_pack_ref is not None:
            process = self._runtime.load_process_state(workspace)
            already_active = any(
                ref == default_methodology_pack_ref and record.status == "active"
                for ref, record in process.active_methodology_packs.items()
            )
            if not already_active:
                self.set_methodology(
                    workspace,
                    default_methodology_pack_ref,
                    actor=actor,
                    reason=f"activated for {new_ref_str}",
                )

        return updated_manifest
