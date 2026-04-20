from __future__ import annotations

from pathlib import Path
import re
import uuid

from ..common.errors import ConflictError
from ..domain.registry import ObjectRef
from ..domain.workspace_views import CommandResultView, ProjectCreatedView
from .domain_pack_selection_service import DomainPackSelectionService
from .planning_service import PlanningService
from .project_service import ProjectService
from .registry_service import RegistryService
from .workflow_service import WorkflowService
from .workspace_catalog import WorkspaceCatalog


class WorkspaceCommandService:
    def __init__(
        self,
        catalog: WorkspaceCatalog,
        registry_service: RegistryService,
        project_service: ProjectService,
        planning_service: PlanningService,
        workflow_service: WorkflowService,
        domain_pack_selection_service: DomainPackSelectionService,
    ) -> None:
        self._catalog = catalog
        self._registry_service = registry_service
        self._project_service = project_service
        self._planning_service = planning_service
        self._workflow_service = workflow_service
        self._domain_pack_selection_service = domain_pack_selection_service

    def run_next(self, project_id: str, *, provider: str | None = None, model: str | None = None) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.run_next(workspace_ref.workspace, snapshot, provider=provider, model=model)
        status = "accepted" if result.planning_outcome == "materialized" else "blocked"
        summary = (
            f"Запущен шаг '{result.selected_step_id}'."
            if result.task_id
            else (result.reasons[0] if result.reasons else "Команда не изменила состояние проекта.")
        )
        return CommandResultView(
            status=status,
            command_name="run-next",
            summary=summary,
            changed_projections=("journey", "situation", "timeline", "artifacts", "review", "state", "debug"),
            resource_id=result.task_id,
        )

    def run_until_blocked(
        self,
        project_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int = 20,
    ) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        result = self._workflow_service.run_until_blocked(
            workspace_ref.workspace,
            snapshot,
            provider=provider,
            model=model,
            max_steps=max_steps,
        )
        if result.stopped_reason == "recipe_completed":
            status = "accepted"
            summary = f"Workflow завершён успешно: все шаги recipe пройдены за {len(result.steps)} шагов."
        elif result.stopped_reason == "validation_failed":
            status = "warning"
            summary = "Workflow остановлен: ревью или валидация требуют внимания."
        elif result.stopped_reason == "planner_blocked":
            status = "blocked"
            summary = "Workflow остановлен: автоматических следующих шагов сейчас нет."
        else:
            status = "warning"
            summary = f"Workflow остановлен со статусом '{result.stopped_reason}' после {len(result.steps)} шагов."
        return CommandResultView(
            status=status,
            command_name="run-until-blocked",
            summary=summary,
            changed_projections=("journey", "situation", "timeline", "artifacts", "review", "state", "debug"),
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
            summary = f"Шаг '{result.selected_step_id or task_id}' успешно выполнен повторно."
        else:
            status = "warning"
            summary = result.reasons[0] if result.reasons else "Повторный запуск шага завершился с ошибкой."
        return CommandResultView(
            status=status,
            command_name="retry-task",
            summary=summary,
            changed_projections=("journey", "situation", "timeline", "artifacts", "review", "state", "debug"),
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
            changed_projections=("situation", "timeline", "state"),
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
            changed_projections=("situation", "timeline", "state"),
            resource_id=dimension,
        )

    def enable_domain_pack(self, project_id: str, *, pack_ref: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        snapshot = self._validated_snapshot()
        pack = snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref))
        self._project_service.enable_domain_pack(workspace_ref.workspace, pack)
        self._planning_service.current_composed_recipe(workspace_ref.workspace, snapshot)
        return CommandResultView(
            status="accepted",
            command_name="enable-domain-pack",
            summary=f"Подключён доменный пакет '{pack_ref}'.",
            changed_projections=("shell", "journey", "situation", "timeline", "state", "debug"),
            resource_id=pack_ref,
        )

    def create_project(
        self,
        *,
        name: str,
        recipe_ref: str,
        request_text: str,
        domain_pack_refs: tuple[str, ...] = (),
        selection_provider: str | None = None,
        selection_model: str | None = None,
    ) -> ProjectCreatedView:
        snapshot = self._validated_snapshot()
        recipe_object_ref = ObjectRef.parse(recipe_ref)
        if domain_pack_refs:
            resolved_pack_refs = tuple(sorted(set(domain_pack_refs)))
            for pack_ref in resolved_pack_refs:
                snapshot.resolve_domain_pack(ObjectRef.parse(pack_ref))
            selection_summary = "Использован явный ручной выбор domain pack."
        else:
            selection = self._domain_pack_selection_service.select_for_request(
                snapshot,
                recipe_ref=recipe_object_ref.as_string(),
                request_text=request_text.strip(),
                provider=selection_provider,
                model=selection_model,
            )
            resolved_pack_refs = selection.selected_pack_refs
            selection_summary = (
                f"Автоматический модуль подбора доменных пакетов ({selection.provider}) выбрал: "
                f"{', '.join(selection.selected_pack_refs) if selection.selected_pack_refs else 'ничего'}. "
                f"Обоснование: {selection.rationale}"
            )
        bootstrap_recipe = self._planning_service.build_recipe_bootstrap(
            snapshot,
            recipe_object_ref.as_string(),
            enabled_domain_pack_refs=resolved_pack_refs,
        )
        workspace = self._allocate_workspace(name)
        bootstrap = self._project_service.init_project(
            workspace=workspace,
            name=name.strip(),
            recipe_ref=recipe_object_ref,
            request_text=request_text.strip(),
            bootstrap_recipe=bootstrap_recipe,
        )
        self._project_service.add_fact(
            workspace,
            fact_id="domain_pack_selection",
            statement=selection_summary,
            source="domain_pack_selector",
        )
        return ProjectCreatedView(
            project_id=bootstrap.manifest.project_id,
            name=bootstrap.manifest.name,
            recipe_ref=bootstrap.manifest.recipe_ref,
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
        slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
        slug = slug.strip("-")[:32] or "project"
        workspace = bucket / f"{slug}-{uuid.uuid4().hex[:8]}"
        return workspace
