from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..common.serialization import utc_now_iso
from ..domain.problem_state import CloseGapPatch, SetGoalPatch, UpsertReadinessPatch
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .execution_service import ExecutionBundle, ExecutionService
from .planning_service import PlanningService
from .validation_service import ValidationService


@dataclass(frozen=True)
class WorkflowStepResult:
    planning_outcome: str
    task_id: str | None
    selected_step_id: str | None
    execution_run_id: str | None
    validation_status: str | None
    applied_patches: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WorkflowRunResult:
    steps: tuple[WorkflowStepResult, ...]
    stopped_reason: str


class WorkflowService:
    def __init__(
        self,
        runtime: SqliteRuntime,
        planning_service: PlanningService,
        execution_service: ExecutionService,
        validation_service: ValidationService,
    ) -> None:
        self._runtime = runtime
        self._planning_service = planning_service
        self._execution_service = execution_service
        self._validation_service = validation_service

    def run_next(
        self,
        workspace: Path,
        snapshot,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> WorkflowStepResult:
        decision = self._planning_service.plan(workspace, snapshot, mode="apply")
        if decision.outcome != "materialized" or not decision.created_task_id:
            return WorkflowStepResult(
                planning_outcome=decision.outcome,
                task_id=None,
                selected_step_id=decision.selected_step_id,
                execution_run_id=None,
                validation_status=None,
                reasons=decision.reasons,
            )

        task_id = decision.created_task_id
        return self._execute_existing_task(
            workspace,
            snapshot,
            task_id=task_id,
            planning_outcome=decision.outcome,
            selected_step_id=decision.selected_step_id,
            provider=provider,
            model=model,
            reasons=decision.reasons,
        )

    def retry_task(
        self,
        workspace: Path,
        snapshot,
        *,
        task_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> WorkflowStepResult:
        task = self._runtime.get_task(workspace, task_id)
        self._planning_service.transition_task(workspace, task_id, "retry")
        return self._execute_existing_task(
            workspace,
            snapshot,
            task_id=task_id,
            planning_outcome="retried",
            selected_step_id=task.recipe_step_id,
            provider=provider,
            model=model,
            reasons=("Шаг запущен повторно после ошибки.",),
        )

    def _execute_existing_task(
        self,
        workspace: Path,
        snapshot,
        *,
        task_id: str,
        planning_outcome: str,
        selected_step_id: str | None,
        provider: str | None,
        model: str | None,
        reasons: tuple[str, ...],
    ) -> WorkflowStepResult:
        self._planning_service.transition_task(workspace, task_id, "start")
        try:
            execution_bundle = self._execution_service.execute_task(
                workspace,
                snapshot,
                task_id,
                provider=provider,
                model=model,
            )
            validation_run = self._validation_service.validate_execution(
                workspace,
                snapshot,
                task_id=task_id,
                execution_bundle=execution_bundle,
            )
        except Exception as exc:
            message = str(exc).strip() or "Во время исполнения шага произошла ошибка."
            self._planning_service.transition_task(
                workspace,
                task_id,
                "fail",
                payload={
                    "error_message": message,
                    "error_type": exc.__class__.__name__,
                },
            )
            return WorkflowStepResult(
                planning_outcome=planning_outcome,
                task_id=task_id,
                selected_step_id=selected_step_id,
                execution_run_id=None,
                validation_status="failed",
                reasons=(message,),
            )
        if validation_run.status != "passed":
            self._planning_service.transition_task(
                workspace,
                task_id,
                "fail",
                payload={
                    "error_message": "; ".join(finding.message for finding in validation_run.findings)
                    or "Проверка результата завершилась с ошибкой.",
                    "error_type": "validation_failed",
                },
            )
            return WorkflowStepResult(
                planning_outcome=planning_outcome,
                task_id=task_id,
                selected_step_id=selected_step_id,
                execution_run_id=execution_bundle.result.execution_run_id,
                validation_status=validation_run.status,
                reasons=tuple(finding.message for finding in validation_run.findings),
            )

        applied_patches = list(self._apply_success_effects(workspace, snapshot, task_id, execution_bundle))
        self._planning_service.transition_task(workspace, task_id, "complete")
        return WorkflowStepResult(
            planning_outcome=planning_outcome,
            task_id=task_id,
            selected_step_id=selected_step_id,
            execution_run_id=execution_bundle.result.execution_run_id,
            validation_status=validation_run.status,
            applied_patches=tuple(applied_patches),
            reasons=reasons,
        )

    def run_until_blocked(
        self,
        workspace: Path,
        snapshot,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_steps: int = 64,
    ) -> WorkflowRunResult:
        steps: list[WorkflowStepResult] = []
        for _ in range(max_steps):
            result = self.run_next(workspace, snapshot, provider=provider, model=model)
            steps.append(result)
            if result.planning_outcome != "materialized":
                return WorkflowRunResult(steps=tuple(steps), stopped_reason="planner_blocked")
            if result.validation_status != "passed":
                return WorkflowRunResult(
                    steps=tuple(steps),
                    stopped_reason="execution_failed" if result.execution_run_id is None else "validation_failed",
                )
            manifest = self._runtime.load_manifest(workspace)
            state = self._runtime.load_problem_state(workspace)
            expected_step_ids = set(state.recipe_composition.step_ids) if state.recipe_composition else set()
            progress = {
                item.recipe_step_id: item
                for item in self._runtime.list_recipe_progress(workspace, manifest.recipe_ref)
            }
            if expected_step_ids and all(
                step_id in progress and progress[step_id].status == "completed"
                for step_id in expected_step_ids
            ):
                return WorkflowRunResult(steps=tuple(steps), stopped_reason="recipe_completed")
        return WorkflowRunResult(steps=tuple(steps), stopped_reason="max_steps_reached")

    def _apply_success_effects(self, workspace: Path, snapshot, task_id: str, execution_bundle: ExecutionBundle) -> list[str]:
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(f"{task.template_id}@{task.template_version}")
        state = self._runtime.load_problem_state(workspace)
        applied: list[str] = []

        if execution_bundle.result.proposed_goal:
            self._runtime.apply_problem_patch(
                workspace,
                SetGoalPatch(text=execution_bundle.result.proposed_goal),
                actor="workflow",
                reason=f"goal extracted from {task.recipe_step_id}",
            )
            applied.append("SetGoalPatch")

        for gap_id in template.semantics.closes_gaps:
            if gap_id in state.active_gaps:
                self._runtime.apply_problem_patch(
                    workspace,
                    CloseGapPatch(gap_id=gap_id),
                    actor="workflow",
                    reason=f"gap closed by {task.recipe_step_id}",
                )
                applied.append(f"CloseGapPatch:{gap_id}")

        latest_state = self._runtime.load_problem_state(workspace)
        for readiness_raise in template.semantics.raises_readiness:
            current = latest_state.readiness.get(readiness_raise.dimension)
            blocking = current.blocking if current is not None else False
            self._runtime.apply_problem_patch(
                workspace,
                UpsertReadinessPatch(
                    dimension=readiness_raise.dimension,
                    status=readiness_raise.status,
                    blocking=blocking,
                    confidence=1.0,
                    evidence=(execution_bundle.result.execution_run_id,),
                ),
                actor="workflow",
                reason=f"readiness raised by {task.recipe_step_id}",
            )
            applied.append(f"UpsertReadinessPatch:{readiness_raise.dimension}")

        return applied
