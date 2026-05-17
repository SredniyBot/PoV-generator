from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..common.serialization import utc_now_iso
from ..domain.positions import Position
from ..domain.process_state import CloseGapPatch, UpsertReadinessPatch
from ..domain.project_knowledge import GOAL_POSITION_ID, UpsertPositionPatch
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
    # v3.0: если задача приостановлена pre-flight checkpoint'ом — id
    # сессии, которую пользователь должен закрыть. validation_status
    # тогда = "paused_for_checkpoint" (псевдо-статус; настоящих
    # validation_runs не создаётся).
    checkpoint_session_id: str | None = None


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
        if decision.outcome != "selected" or not decision.selected_task_id:
            return WorkflowStepResult(
                planning_outcome=decision.outcome,
                task_id=None,
                selected_step_id=decision.selected_task_key,
                execution_run_id=None,
                validation_status=None,
                reasons=decision.reasons,
            )

        task_id = decision.selected_task_id
        return self._execute_existing_task(
            workspace,
            snapshot,
            task_id=task_id,
            planning_outcome=decision.outcome,
            selected_step_id=decision.selected_task_key,
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
            selected_step_id=task.task_key,
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
            # v3.0: pre-flight checkpoint может остановить задачу до
            # основной генерации. В этом случае:
            #   - не запускаем валидацию (артефакта нет);
            #   - task возвращаем в статус "blocked" (через `fail`-transition
            #     это даст возможность retry после submit_answers);
            #   - возвращаем step с маркером paused_for_checkpoint.
            if execution_bundle.result.status == "paused_for_checkpoint":
                pause_message = (
                    "Pre-flight checkpoint: ждём ответа пользователя в "
                    f"сессии {execution_bundle.result.checkpoint_session_id}"
                )
                self._planning_service.transition_task(
                    workspace,
                    task_id,
                    "fail",
                    payload={
                        "error_message": pause_message,
                        "error_type": "paused_for_checkpoint",
                    },
                )
                return WorkflowStepResult(
                    planning_outcome=planning_outcome,
                    task_id=task_id,
                    selected_step_id=selected_step_id,
                    execution_run_id=None,
                    validation_status="paused_for_checkpoint",
                    reasons=(pause_message,),
                    checkpoint_session_id=execution_bundle.result.checkpoint_session_id,
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
            if result.planning_outcome not in {"selected", "retried"}:
                # Planner может вернуть "objective_completed", когда нет ни одной
                # допустимой задачи и все gate'ы пройдены — это успешный финал,
                # а не блокировка. Раньше это было невидимо, потому что цепочка
                # планировщик → дальше всегда успевала выполнить хотя бы один
                # шаг и попадала в пост-step проверку ниже. С появлением
                # human_approval gate цикл может закончиться без шага.
                stopped_reason = (
                    "objective_completed"
                    if result.planning_outcome == "objective_completed"
                    else "planner_blocked"
                )
                return WorkflowRunResult(steps=tuple(steps), stopped_reason=stopped_reason)
            if result.validation_status != "passed":
                return WorkflowRunResult(
                    steps=tuple(steps),
                    stopped_reason="execution_failed" if result.execution_run_id is None else "validation_failed",
                )
            if self._planning_service.plan(workspace, snapshot, mode="dry-run", record=False).outcome == "objective_completed":
                return WorkflowRunResult(steps=tuple(steps), stopped_reason="objective_completed")
        return WorkflowRunResult(steps=tuple(steps), stopped_reason="max_steps_reached")

    def _apply_success_effects(self, workspace: Path, snapshot, task_id: str, execution_bundle: ExecutionBundle) -> list[str]:
        task = self._runtime.get_task(workspace, task_id)
        template = snapshot.resolve_template(task.template_ref)
        process = self._runtime.load_process_state(workspace)
        applied: list[str] = []

        # Цель проекта — положение Layer A; обновляем через UpsertPositionPatch.
        if execution_bundle.result.proposed_goal:
            now = utc_now_iso()
            goal_position = Position(
                identifier=GOAL_POSITION_ID,
                type="fact",
                statement=execution_bundle.result.proposed_goal,
                visibility="principal",
                scope="global",
                source="artifact",
                taken_by=f"task:{task.task_key}",
                taken_at=now,
                tags=("project", "goal"),
            )
            self._runtime.apply_knowledge_patch(
                workspace,
                UpsertPositionPatch(position=goal_position),
                actor="workflow",
                reason=f"goal extracted from {task.task_key}",
            )
            applied.append("UpsertPositionPatch:project.goal")

        for gap_id in template.effects.closes_gaps:
            if gap_id in process.active_gaps:
                self._runtime.apply_process_patch(
                    workspace,
                    CloseGapPatch(gap_id=gap_id),
                    actor="workflow",
                    reason=f"gap closed by {task.task_key}",
                )
                applied.append(f"CloseGapPatch:{gap_id}")

        latest_process = self._runtime.load_process_state(workspace)
        for readiness_raise in template.effects.raises_readiness:
            current = latest_process.readiness.get(readiness_raise.dimension)
            blocking = current.blocking if current is not None else False
            self._runtime.apply_process_patch(
                workspace,
                UpsertReadinessPatch(
                    dimension=readiness_raise.dimension,
                    status=readiness_raise.status,
                    blocking=blocking,
                    confidence=1.0,
                    evidence=(execution_bundle.result.execution_run_id,),
                ),
                actor="workflow",
                reason=f"readiness raised by {task.task_key}",
            )
            applied.append(f"UpsertReadinessPatch:{readiness_raise.dimension}")

        return applied
