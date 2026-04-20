from __future__ import annotations

from ..common.errors import ConflictError
from ..domain.registry import ObjectRef
from ..domain.workspace_views import CommandResultView
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
    ) -> None:
        self._catalog = catalog
        self._registry_service = registry_service
        self._project_service = project_service
        self._planning_service = planning_service
        self._workflow_service = workflow_service

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
        return CommandResultView(
            status="accepted",
            command_name="run-until-blocked",
            summary=f"Workflow завершён со статусом '{result.stopped_reason}' после {len(result.steps)} шагов.",
            changed_projections=("journey", "situation", "timeline", "artifacts", "review", "state", "debug"),
        )

    def retry_task(self, project_id: str, *, task_id: str) -> CommandResultView:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        self._planning_service.transition_task(workspace_ref.workspace, task_id, "retry")
        return CommandResultView(
            status="accepted",
            command_name="retry-task",
            summary=f"Задача '{task_id}' переведена в retry.",
            changed_projections=("journey", "situation", "timeline", "debug"),
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

    def _validated_snapshot(self):
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Команды UI заблокированы.")
        return snapshot
