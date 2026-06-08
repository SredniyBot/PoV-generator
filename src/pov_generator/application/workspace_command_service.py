from __future__ import annotations

import re
import uuid
from pathlib import Path

from ..common.errors import ConflictError
from ..common.logging import get_logger
from ..common.serialization import utc_now_iso
from ..domain.positions import REQUISITE_POSITION_PREFIX, Position
from ..domain.project_knowledge import RejectPositionPatch, UpsertPositionPatch
from ..domain.registry import ObjectRef
from ..domain.workspace_views import CommandResultView, ProjectCreatedView

logger = get_logger("project")
from .checkpoint_service import CheckpointService
from .domain_pack_selection_service import DomainPackSelectionService
from .planning_service import PlanningService
from .project_lock import ensure_project_unlocked
from .project_service import ProjectService
from .registry_service import RegistryService
from .workflow_service import WorkflowService
from .workspace_catalog import WorkspaceCatalog
from .workspace_query_service import blocking_requisites_unprovided, gather_requisites

GRAPH_PROJECTIONS = ("task_graph", "situation", "timeline", "artifacts", "clarifications", "review", "state", "debug")


class WorkspaceCommandService:
    def __init__(
        self,
        catalog: WorkspaceCatalog,
        registry_service: RegistryService,
        project_service: ProjectService,
        planning_service: PlanningService,
        workflow_service: WorkflowService,
        domain_pack_selection_service: DomainPackSelectionService,
        checkpoint_service: CheckpointService,
    ) -> None:
        self._catalog = catalog
        self._registry_service = registry_service
        self._project_service = project_service
        self._planning_service = planning_service
        self._workflow_service = workflow_service
        self._domain_pack_selection_service = domain_pack_selection_service
        self._checkpoint_service = checkpoint_service

    def run_next(self, project_id: str, *, provider: str | None = None, model: str | None = None) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.run_next(workspace_ref.workspace, snapshot, provider=provider, model=model)
        status = "accepted" if result.planning_outcome == "selected" else "blocked"
        summary = (
            f"Запущена задача '{result.selected_step_id}'."
            if result.task_id
            else (result.reasons[0] if result.reasons else "Команда не изменила состояние проекта.")
        )
        return CommandResultView(
            status=status,
            command_name="run-next",
            summary=summary,
            changed_projections=GRAPH_PROJECTIONS,
            resource_id=result.task_id,
        )

    def run_until_blocked(
        self,
        project_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int = 1000,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.run_until_blocked(
            workspace_ref.workspace,
            snapshot,
            provider=provider,
            model=model,
            max_steps=max_steps,
        )
        if result.stopped_reason == "objective_completed":
            status = "accepted"
            summary = f"Цель завершена: выполнено задач {len(result.steps)}."
        elif result.stopped_reason == "validation_failed":
            status = "warning"
            summary = "Процесс остановлен: ревью или валидация требуют внимания."
        elif result.stopped_reason == "planner_blocked":
            status = "blocked"
            summary = "Процесс остановлен: автоматических следующих задач сейчас нет."
        else:
            status = "warning"
            summary = f"Процесс остановлен со статусом '{result.stopped_reason}' после {len(result.steps)} задач."
        return CommandResultView(
            status=status,
            command_name="run-until-blocked",
            summary=summary,
            changed_projections=GRAPH_PROJECTIONS,
        )

    def retry_task(
        self,
        project_id: str,
        *,
        task_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.retry_task(
            workspace_ref.workspace,
            snapshot,
            task_id=task_id,
            provider=provider,
            model=model,
        )
        if result.validation_status == "passed":
            status = "accepted"
            summary = f"Задача '{result.selected_step_id or task_id}' успешно выполнена повторно."
        else:
            status = "warning"
            summary = result.reasons[0] if result.reasons else "Повторный запуск задачи завершился с ошибкой."
        return CommandResultView(
            status=status,
            command_name="retry-task",
            summary=summary,
            changed_projections=GRAPH_PROJECTIONS,
            resource_id=task_id,
        )

    def set_goal(self, project_id: str, *, text: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._project_service.set_goal(workspace_ref.workspace, text)
        return CommandResultView(
            status="accepted",
            command_name="set-goal",
            summary="Цель проекта обновлена.",
            changed_projections=("shell", "situation", "timeline", "state"),
        )

    def close_gap(self, project_id: str, *, gap_id: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._project_service.close_gap(workspace_ref.workspace, gap_id)
        return CommandResultView(
            status="accepted",
            command_name="close-gap",
            summary=f"Gap '{gap_id}' закрыт.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=gap_id,
        )

    def set_readiness(
        self,
        project_id: str,
        *,
        dimension: str,
        status: str,
        blocking: bool,
        confidence: float,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._project_service.set_readiness(
            workspace_ref.workspace,
            dimension=dimension,
            status=status,
            blocking=blocking,
            confidence=confidence,
        )
        return CommandResultView(
            status="accepted",
            command_name="set-readiness",
            summary=f"Readiness '{dimension}' обновлена.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=dimension,
        )

    def enable_domain_pack(self, project_id: str, *, pack_ref: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        pack = snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref))
        self._project_service.enable_domain_pack(workspace_ref.workspace, pack)
        self._planning_service.expand_graph(workspace_ref.workspace, snapshot)
        return CommandResultView(
            status="accepted",
            command_name="enable-domain-pack",
            summary=f"Подключён доменный пакет '{pack_ref}'.",
            changed_projections=("shell", "task_graph", "situation", "timeline", "clarifications", "state", "debug"),
            resource_id=pack_ref,
        )

    def provide_requisite(
        self,
        project_id: str,
        *,
        key: str,
        mode: str = "reference",
        value: str = "",
        attachment_id: str = "",
        note: str = "",
    ) -> CommandResultView:
        """Зафиксировать предоставленный реквизит и пустить данные в работу.

        Реквизиты v2 (Ф3): данные реально втекают. Режим ``value`` кладёт
        значение в слой A как пользовательский факт (``source="user"``) — он
        попадает в контекст зависимых задач. Безопасность (инвариант 6): вид
        ``credential`` и режим ``reference`` НИКОГДА не несут значение в
        контекст/артефакты (только пометка «выдано вне системы»); прежнее
        value-положение при смене на reference снимается, чтобы секрет/
        устаревшее значение не осталось в контексте. Предоставление — событие:
        затем идёт переоценка графа (``expand_graph``), чтобы задача-потребитель
        могла продолжиться.
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        workspace = workspace_ref.workspace
        runtime = self._project_service.runtime
        ensure_project_unlocked(runtime, workspace)

        # Метаданные реквизита (вид/заголовок/потребитель) для безопасности и
        # содержательного положения. Реквизит мог стать невидимым (артефакт
        # переехал) — тогда работаем по ключу как есть.
        items, _, _ = gather_requisites(runtime, workspace)
        match = next(
            (it for it in items if (it.key or it.title) == key or it.title == key), None
        )
        title = match.title if match else key
        needed_for = match.needed_for if match else ""
        kind = match.kind if match else "other"

        # Инвариант безопасности: секрет не персистится. credential → только
        # reference; reference никогда не несёт значение.
        if kind == "credential" and mode == "value":
            mode = "reference"
        if mode == "reference":
            value = ""

        runtime.mark_requisite_provided(
            workspace,
            requisite_key=key,
            note=note,
            mode=mode,
            value=value,
            attachment_id=attachment_id,
        )

        # Втекание данных в слой A. Значение → пользовательский факт в контекст.
        position_id = f"{REQUISITE_POSITION_PREFIX}{key}"
        if mode == "value" and value.strip():
            statement = f"Данные, предоставленные пользователем по реквизиту «{title}»"
            if needed_for:
                statement += f" (нужно для: {needed_for})"
            statement += f":\n\n{value.strip()}"
            runtime.apply_knowledge_patch(
                workspace,
                UpsertPositionPatch(
                    Position(
                        identifier=position_id,
                        type="fact",
                        statement=statement,
                        visibility="architectural",
                        scope="global",
                        source="user",
                        taken_by="requisite",
                        taken_at=utc_now_iso(),
                        tags=("requisite", "user_input"),
                    )
                ),
                actor="requisite",
                reason=f"provided requisite {key} (value)",
            )
        else:
            # reference/file: значение в контексте не держим. Снимаем прежнее
            # value-положение (например, переключение credential value →
            # reference), чтобы секрет/устаревшее значение не осталось.
            knowledge = runtime.load_project_state(workspace).knowledge
            existing = knowledge.positions.get(position_id)
            if existing is not None and existing.status == "active":
                runtime.apply_knowledge_patch(
                    workspace,
                    RejectPositionPatch(
                        position_id=position_id,
                        reason=f"requisite {key} re-provided as {mode}",
                    ),
                    actor="requisite",
                    reason=f"provided requisite {key} ({mode})",
                )

        # Предоставление — событие: переоценка графа (дозапуск/реплан потребителя).
        snapshot = self._validated_snapshot()
        self._planning_service.expand_graph(workspace, snapshot)

        return CommandResultView(
            status="accepted",
            command_name="provide-requisite",
            summary=f"Реквизит «{title}» отмечен как предоставленный.",
            changed_projections=("situation", "timeline", "state", "task_graph"),
            resource_id=key,
        )

    def set_clarification_mode(self, project_id: str, *, mode: str) -> CommandResultView:
        """Сменить режим участия пользователя (v3.2).

        Делегирует в `CheckpointService.set_participation_mode`, который:
        - применяет SetClarificationModePatch к ProcessState;
        - реэвалюирует существующие proposed-decisions: те, что больше
          не должны показываться в новом режиме — auto-accept default;
        - финализирует pending checkpoint-сессии, у которых все
          decisions стали закрытыми;
        - переводит соответствующие failed-задачи обратно в ready.

        Этот endpoint возвращает понятный пользователю summary
        («приняты автоматически X, разблокированы Y задач»).
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        result = self._checkpoint_service.set_participation_mode(workspace_ref.workspace, mode)
        if result.resumed_task_count > 0:
            summary = (
                f"Режим участия изменён на «{mode}». "
                f"Автоматически приняты {result.auto_accepted_count} решений, "
                f"разблокированы {result.resumed_task_count} задач."
            )
        elif result.auto_accepted_count > 0:
            summary = (
                f"Режим участия изменён на «{mode}». "
                f"Автоматически приняты {result.auto_accepted_count} решений."
            )
        else:
            summary = f"Режим участия изменён на «{mode}»."
        return CommandResultView(
            status="accepted",
            command_name="set-clarification-mode",
            summary=summary,
            changed_projections=("shell", "situation", "timeline", "state", "task_graph", "overview"),
            resource_id=mode,
        )

    def set_methodology(self, project_id: str, *, pack_ref: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        snapshot.resolve_methodology_pack(ObjectRef.parse(pack_ref))
        self._project_service.set_methodology(workspace_ref.workspace, pack_ref)
        return CommandResultView(
            status="accepted",
            command_name="set-methodology",
            summary=f"Активная методология обновлена: '{pack_ref}'.",
            changed_projections=("shell", "situation", "timeline", "state", "debug"),
            resource_id=pack_ref,
        )

    def activate_next_objective(
        self,
        project_id: str,
        *,
        new_objective_ref: str,
    ) -> CommandResultView:
        """Переключить workspace на следующий objective (ТЗ → архитектура).

        Состояние (knowledge + process) сохраняется; новый objective получает
        собственное дерево задач рядом со старым. Существующие артефакты
        автоматически становятся доступны новым задачам через
        ``requires.artifacts.optional``.
        """
        workspace_ref = self._catalog.resolve_workspace(project_id)
        ensure_project_unlocked(self._project_service.runtime, workspace_ref.workspace)
        snapshot = self._validated_snapshot()
        new_ref_obj = ObjectRef.parse(new_objective_ref)
        new_spec = snapshot.resolve_objective(new_ref_obj)
        # Ф5: шлюз перехода на реализацию — блокирующие реквизиты должны быть
        # предоставлены. Гасим только переход в objective реализации; остальные
        # переходы (например, ТЗ → архитектура) реквизитами не держатся.
        if new_ref_obj.identifier.startswith("implementation"):
            missing = blocking_requisites_unprovided(
                self._project_service.runtime, workspace_ref.workspace
            )
            if missing:
                raise ConflictError(
                    "Нельзя перейти к реализации: не предоставлены обязательные "
                    "реквизиты — " + "; ".join(missing) + ". Заполните их во вкладке «Реквизиты»."
                )
        methodology_ref = (
            new_spec.default_methodology_pack_ref.as_string()
            if new_spec.default_methodology_pack_ref is not None
            else None
        )
        self._project_service.activate_next_objective(
            workspace_ref.workspace,
            new_ref_obj,
            default_methodology_pack_ref=methodology_ref,
        )
        self._planning_service.expand_graph(workspace_ref.workspace, snapshot)
        return CommandResultView(
            status="accepted",
            command_name="activate-next-objective",
            summary=f"Активирован следующий objective: '{new_objective_ref}'.",
            changed_projections=("shell", "situation", "task_graph", "timeline", "state"),
            resource_id=new_objective_ref,
        )

    def create_project(
        self,
        *,
        name: str,
        objective_ref: str,
        request_text: str,
        domain_pack_refs: tuple[str, ...] = (),
        selection_provider: str | None = None,
        selection_model: str | None = None,
    ) -> ProjectCreatedView:
        snapshot = self._validated_snapshot()
        objective_object_ref = ObjectRef.parse(objective_ref)
        if domain_pack_refs:
            resolved_pack_refs = tuple(sorted(set(domain_pack_refs)))
            packs = tuple(snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref)) for pack_ref in resolved_pack_refs)
            selection_summary = "Использован явный ручной выбор доменных пакетов."
        else:
            selection = self._domain_pack_selection_service.select_for_request(
                snapshot,
                objective_ref=objective_object_ref.as_string(),
                request_text=request_text.strip(),
                provider=selection_provider,
                model=selection_model,
            )
            resolved_pack_refs = selection.selected_pack_refs
            packs = tuple(snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref)) for pack_ref in resolved_pack_refs)
            selection_summary = (
                f"Автоматический модуль подбора доменных пакетов ({selection.provider}) выбрал: "
                f"{', '.join(selection.selected_pack_refs) if selection.selected_pack_refs else 'ничего'}. "
                f"Обоснование: {selection.rationale}"
            )
        workspace = self._allocate_workspace(name)
        objective_spec = snapshot.resolve_objective(objective_object_ref)
        methodology_ref = (
            objective_spec.default_methodology_pack_ref.as_string()
            if objective_spec.default_methodology_pack_ref is not None
            else None
        )
        bootstrap = self._project_service.init_project(
            workspace=workspace,
            name=name.strip(),
            objective_ref=objective_object_ref,
            request_text=request_text.strip(),
            domain_packs=packs,
            default_methodology_pack_ref=methodology_ref,
        )
        self._project_service.add_fact(
            workspace,
            identifier="domain_pack_selection",
            statement=selection_summary,
            source="system",
            taken_by_label="domain_pack_selector",
        )
        self._planning_service.expand_graph(workspace, snapshot)
        logger.info(
            f"проект создан «{bootstrap.manifest.name}»",
            objective=bootstrap.manifest.objective_ref.split("@")[0],
        )
        return ProjectCreatedView(
            project_id=bootstrap.manifest.project_id,
            name=bootstrap.manifest.name,
            objective_ref=bootstrap.manifest.objective_ref,
            domain_pack_refs=resolved_pack_refs,
            workspace_path=str(workspace),
        )

    def _validated_snapshot(self):
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Команды UI заблокированы.")
        return snapshot

    def _allocate_workspace(self, name: str) -> Path:
        bucket = self._catalog.runtime_root / "ui_cases"
        bucket.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9а-яё]+", "-", name.strip().lower())
        slug = slug.strip("-")[:32] or "project"
        return bucket / f"{slug}-{uuid.uuid4().hex[:8]}"
