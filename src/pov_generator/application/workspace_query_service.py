from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json

from ..common.errors import ConflictError
from ..common.serialization import to_primitive
from ..domain.registry import ComposedRecipe, RegistrySnapshot, compose_recipe
from ..domain.workspace_views import (
    ActionDescriptor,
    ArtifactDetailView,
    ArtifactSummaryView,
    ArtifactValidationView,
    ContextManifestSummaryView,
    DomainPackCatalogItemView,
    JourneyStepView,
    ProjectDebugView,
    ProjectJourneyView,
    ProjectListItemView,
    ProjectReviewView,
    ProjectShellView,
    ProjectSituationView,
    ProjectStateView,
    ProjectTimelineView,
    RecipeCatalogItemView,
    ReviewIssueView,
    SituationBlockerView,
    TimelineEntryView,
)
from ..infrastructure.sqlite_runtime import ProjectManifest, SqliteRuntime
from .planning_service import PlanningService
from .registry_service import RegistryService
from .workspace_catalog import WorkspaceCatalog, WorkspaceRef


ProjectionName = str


@dataclass(frozen=True)
class ProjectContext:
    workspace_ref: WorkspaceRef
    workspace: Path
    manifest: ProjectManifest
    state: object
    snapshot: RegistrySnapshot
    composed_recipe: ComposedRecipe


class WorkspaceQueryService:
    DEFAULT_PROJECTIONS: tuple[ProjectionName, ...] = (
        "shell",
        "journey",
        "situation",
        "timeline",
        "artifacts",
        "review",
        "state",
        "debug",
    )

    def __init__(
        self,
        catalog: WorkspaceCatalog,
        registry_service: RegistryService,
        runtime: SqliteRuntime,
        planning_service: PlanningService,
    ) -> None:
        self._catalog = catalog
        self._registry_service = registry_service
        self._runtime = runtime
        self._planning_service = planning_service

    def list_projects(self) -> tuple[ProjectListItemView, ...]:
        items: list[ProjectListItemView] = []
        for workspace_ref in self._catalog.list_workspaces():
            context = self._load_context_by_ref(workspace_ref)
            situation = self.project_situation(context.workspace_ref.project_id)
            current_step_title = None
            journey = self.project_journey(context.workspace_ref.project_id)
            for step in journey.steps:
                if step.is_current:
                    current_step_title = step.title
                    break
            items.append(
                ProjectListItemView(
                    project_id=context.manifest.project_id,
                    name=context.manifest.name,
                    status_label=situation.status_label,
                    updated_at=context.state.updated_at,
                    has_blockers=situation.blocking,
                    current_step_title=current_step_title,
                )
            )
        return tuple(sorted(items, key=lambda item: (item.updated_at, item.project_id), reverse=True))

    def list_recipes(self) -> tuple[RecipeCatalogItemView, ...]:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно отобразить список recipes.")
        items = [
            RecipeCatalogItemView(
                recipe_ref=recipe.ref.as_string(),
                name=recipe.name,
                domain=recipe.domain,
                stage_gate=recipe.stage_gate,
                step_count=len(recipe.steps),
            )
            for recipe in snapshot.recipes.values()
        ]
        return tuple(sorted(items, key=lambda item: (item.domain, item.name, item.recipe_ref)))

    def list_domain_packs(self) -> tuple[DomainPackCatalogItemView, ...]:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно отобразить список domain packs.")
        items = [
            DomainPackCatalogItemView(
                pack_ref=pack.ref.as_string(),
                name=pack.name,
                domain=pack.domain,
                description=pack.description,
                status=pack.status,
                entry_signals=pack.entry_signals,
            )
            for pack in snapshot.domain_packs.values()
        ]
        return tuple(sorted(items, key=lambda item: (item.domain, item.name, item.pack_ref)))

    def project_shell(self, project_id: str) -> ProjectShellView:
        context = self._load_context(project_id)
        situation = self._build_situation(context)
        return ProjectShellView(
            project_id=context.manifest.project_id,
            name=context.manifest.name,
            business_request=context.state.business_request,
            recipe_ref=context.manifest.recipe_ref,
            enabled_domain_packs=tuple(sorted(context.state.enabled_domain_packs.keys())),
            goal=context.state.goal,
            status_label=situation.status_label,
            updated_at=context.state.updated_at,
        )

    def project_journey(self, project_id: str) -> ProjectJourneyView:
        context = self._load_context(project_id)
        recipe_progress = {
            item.recipe_step_id: item for item in self._runtime.list_recipe_progress(context.workspace, context.manifest.recipe_ref)
        }
        preview = self._planning_service.plan(
            context.workspace,
            context.snapshot,
            mode="dry-run",
            record=False,
            refresh_composition=False,
        )
        current_step_id = preview.selected_step_id
        steps: list[JourneyStepView] = []
        completed_steps = 0
        for step in context.composed_recipe.steps:
            progress = recipe_progress.get(step.identifier)
            status = progress.status if progress is not None else "pending"
            status_summary = self._step_status_summary(context.workspace, step.identifier, status)
            tasks_for_step = [item for item in self._runtime.list_tasks(context.workspace) if item.recipe_step_id == step.identifier]
            tasks_for_step.sort(key=lambda item: item.updated_at, reverse=True)
            latest_task = tasks_for_step[0] if tasks_for_step else None
            if status == "completed":
                completed_steps += 1
            steps.append(
                JourneyStepView(
                    step_id=step.identifier,
                    title=step.title,
                    template_ref=step.template_ref.as_string(),
                    source_kind=step.source_kind,
                    source_ref=step.source_ref,
                    status=status,
                    status_summary=status_summary,
                    latest_task_id=latest_task.task_id if latest_task is not None else None,
                    retryable=latest_task.status == "failed" if latest_task is not None else False,
                    required=step.required,
                    is_current=step.identifier == current_step_id,
                )
            )
        return ProjectJourneyView(
            project_id=context.manifest.project_id,
            recipe_ref=context.manifest.recipe_ref,
            domain_pack_refs=context.composed_recipe.domain_pack_refs,
            recipe_fragment_refs=context.composed_recipe.recipe_fragment_refs,
            current_step_id=current_step_id,
            completed_steps=completed_steps,
            total_steps=len(context.composed_recipe.steps),
            steps=tuple(steps),
        )

    def project_situation(self, project_id: str) -> ProjectSituationView:
        context = self._load_context(project_id)
        return self._build_situation(context)

    def project_timeline(self, project_id: str, *, after_sequence: int = 0) -> ProjectTimelineView:
        context = self._load_context(project_id)
        entries = self._build_timeline(context)
        filtered = tuple(entry for entry in entries if entry.sequence > after_sequence)
        return ProjectTimelineView(
            project_id=context.manifest.project_id,
            entries=filtered,
            total_entries=len(entries),
        )

    def project_artifacts(self, project_id: str) -> tuple[ArtifactSummaryView, ...]:
        context = self._load_context(project_id)
        result: list[ArtifactSummaryView] = []
        for artifact in self._runtime.list_artifacts(context.workspace):
            markdown_path = context.workspace / artifact.storage_path.replace(".json", ".md")
            result.append(
                ArtifactSummaryView(
                    artifact_id=artifact.artifact_id,
                    artifact_role=artifact.artifact_role,
                    title=artifact.title,
                    created_at=artifact.created_at,
                    created_by_task_id=artifact.created_by_task_id,
                    has_markdown=markdown_path.exists(),
                )
            )
        return tuple(result)

    def artifact_detail(self, project_id: str, artifact_id: str) -> ArtifactDetailView:
        context = self._load_context(project_id)
        artifact = self._runtime.load_artifact(context.workspace, artifact_id)
        json_content = self._runtime.load_artifact_content(context.workspace, artifact.artifact_id)
        markdown_path = context.workspace / artifact.storage_path.replace(".json", ".md")
        validations = self._artifact_validations(context.workspace, artifact.artifact_id)
        return ArtifactDetailView(
            artifact_id=artifact.artifact_id,
            artifact_role=artifact.artifact_role,
            title=artifact.title,
            description=artifact.description,
            created_at=artifact.created_at,
            created_by_task_id=artifact.created_by_task_id,
            template_ref=str(artifact.metadata.get("template_ref")) if artifact.metadata.get("template_ref") else None,
            json_content=json_content,
            markdown_content=markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else None,
            validations=validations,
        )

    def project_review(self, project_id: str) -> ProjectReviewView:
        context = self._load_context(project_id)
        artifact = self._runtime.latest_artifact_by_role(context.workspace, "review_report")
        if artifact is None:
            return ProjectReviewView(
                project_id=context.manifest.project_id,
                status="missing",
                summary=None,
                strengths=(),
                issues=(),
                recommendations=(),
                artifact_id=None,
                updated_at=None,
            )

        payload = json.loads(self._runtime.load_artifact_content(context.workspace, artifact.artifact_id))
        issues = tuple(
            ReviewIssueView(severity=item["severity"], message=item["message"])
            for item in payload.get("issues", [])
        )
        return ProjectReviewView(
            project_id=context.manifest.project_id,
            status=payload.get("overall_status", "unknown"),
            summary=payload.get("summary"),
            strengths=tuple(payload.get("strengths", [])),
            issues=issues,
            recommendations=tuple(payload.get("recommendations", [])),
            artifact_id=artifact.artifact_id,
            updated_at=artifact.created_at,
        )

    def project_state(self, project_id: str) -> ProjectStateView:
        context = self._load_context(project_id)
        state = context.state
        return ProjectStateView(
            project_id=state.project_id,
            goal=state.goal,
            active_gaps=tuple(sorted((to_primitive(item) for item in state.active_gaps.values()), key=lambda item: item["identifier"])),
            readiness=tuple(sorted((to_primitive(item) for item in state.readiness.values()), key=lambda item: item["dimension"])),
            known_facts=tuple(sorted((to_primitive(item) for item in state.known_facts.values()), key=lambda item: item["identifier"])),
            enabled_domain_packs=tuple(sorted((to_primitive(item) for item in state.enabled_domain_packs.values()), key=lambda item: item["ref"])),
            recipe_composition=to_primitive(state.recipe_composition) if state.recipe_composition else None,
            updated_at=state.updated_at,
        )

    def project_debug(self, project_id: str) -> ProjectDebugView:
        context = self._load_context(project_id)
        context_manifests = tuple(
            ContextManifestSummaryView(
                manifest_id=item.manifest_id,
                task_id=item.task_id,
                template_ref=item.template_ref,
                problem_state_version=item.problem_state_version,
                used_tokens=item.budget.used_tokens,
                max_input_tokens=item.budget.max_input_tokens,
                item_count=len(item.items),
                created_at=item.created_at,
            )
            for item in self._runtime.list_context_manifests(context.workspace)
        )
        return ProjectDebugView(
            project_id=context.manifest.project_id,
            tasks=tuple(to_primitive(item) for item in self._runtime.list_tasks(context.workspace)),
            task_events=tuple(to_primitive(item) for item in self._runtime.list_task_events(context.workspace)),
            planning_history=tuple(to_primitive(item) for item in self._runtime.list_planning_decisions(context.workspace)),
            execution_runs=tuple(self._normalize_json_columns(item) for item in self._runtime.list_execution_runs(context.workspace)),
            execution_traces=tuple(self._normalize_json_columns(item) for item in self._runtime.list_execution_traces(context.workspace)),
            context_manifests=context_manifests,
            validation_runs=tuple(to_primitive(item) for item in self._runtime.list_validation_runs(context.workspace)),
            escalations=tuple(to_primitive(item) for item in self._runtime.list_escalations(context.workspace)),
        )

    def projection_signatures(
        self,
        project_id: str,
        projections: tuple[ProjectionName, ...] | None = None,
    ) -> dict[str, str]:
        projection_names = projections or self.DEFAULT_PROJECTIONS
        values: dict[str, object] = {}
        for name in projection_names:
            if name == "shell":
                values[name] = self.project_shell(project_id)
            elif name == "journey":
                values[name] = self.project_journey(project_id)
            elif name == "situation":
                values[name] = self.project_situation(project_id)
            elif name == "timeline":
                values[name] = self.project_timeline(project_id)
            elif name == "artifacts":
                values[name] = self.project_artifacts(project_id)
            elif name == "review":
                values[name] = self.project_review(project_id)
            elif name == "state":
                values[name] = self.project_state(project_id)
            elif name == "debug":
                values[name] = self.project_debug(project_id)
            else:
                raise ConflictError(f"Неизвестная проекция '{name}'.")
        return {name: self._signature(value) for name, value in values.items()}

    def realtime_token(self, project_id: str) -> str:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        workspace = workspace_ref.workspace
        token_parts: list[str] = []
        for file_path in (
            workspace / self._runtime.MANIFEST_FILENAME,
            workspace / self._runtime.DB_FILENAME,
        ):
            if not file_path.exists():
                continue
            stat = file_path.stat()
            token_parts.append(f"{file_path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        if not token_parts:
            raise ConflictError(f"Не удалось вычислить realtime token для проекта '{project_id}'.")
        return sha256("|".join(token_parts).encode("utf-8")).hexdigest()

    def _load_context(self, project_id: str) -> ProjectContext:
        workspace_ref = self._catalog.resolve_workspace(project_id)
        return self._load_context_by_ref(workspace_ref)

    def _load_context_by_ref(self, workspace_ref: WorkspaceRef) -> ProjectContext:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно построить UI-проекции.")
        workspace = workspace_ref.workspace
        state = self._runtime.load_problem_state(workspace)
        composed_recipe = compose_recipe(snapshot, workspace_ref.manifest.recipe_ref, tuple(sorted(state.enabled_domain_packs.keys())))
        return ProjectContext(
            workspace_ref=workspace_ref,
            workspace=workspace,
            manifest=workspace_ref.manifest,
            state=state,
            snapshot=snapshot,
            composed_recipe=composed_recipe,
        )

    def _build_situation(self, context: ProjectContext) -> ProjectSituationView:
        state = context.state
        review = self.project_review(context.manifest.project_id)
        escalations = self._runtime.list_escalations(context.workspace)
        validation_runs = self._runtime.list_validation_runs(context.workspace)
        execution_runs = self._runtime.list_execution_runs(context.workspace)
        active_tasks = [
            task
            for task in self._runtime.list_tasks(context.workspace)
            if task.status in {"queued", "in_progress", "waiting_for_children", "blocked"}
        ]
        failed_tasks = [
            task for task in self._runtime.list_tasks(context.workspace) if task.status == "failed"
        ]
        journey = self.project_journey(context.manifest.project_id)
        preview = self._planning_service.plan(
            context.workspace,
            context.snapshot,
            mode="dry-run",
            record=False,
            refresh_composition=False,
        )

        blockers: list[SituationBlockerView] = []
        for task in sorted(failed_tasks, key=lambda item: item.updated_at, reverse=True)[:3]:
            step_title = next(
                (step.title for step in context.composed_recipe.steps if step.identifier == task.recipe_step_id),
                task.recipe_step_id,
            )
            blockers.append(
                SituationBlockerView(
                    kind="task_failure",
                    title=f"Ошибка на шаге «{step_title}»",
                    summary=self._failure_summary_for_task(
                        context.workspace,
                        task.task_id,
                        default=f"Шаг '{step_title}' завершился с ошибкой и требует разбора.",
                    ),
                    severity="error",
                    detail_view="debug",
                    related_id=task.task_id,
                )
            )

        for run in sorted(validation_runs, key=lambda item: item.created_at, reverse=True):
            if run.status != "failed":
                continue
            blockers.append(
                SituationBlockerView(
                    kind="validation_failure",
                    title="Валидация шага не пройдена",
                    summary=run.findings[0].message if run.findings else "Валидация завершилась ошибкой.",
                    severity="error",
                    detail_view="review",
                    related_id=run.validation_run_id,
                )
            )
            break

        for run in sorted(execution_runs, key=lambda item: str(item.get("created_at", "")), reverse=True):
            if str(run.get("status")) != "failed":
                continue
            blockers.append(
                SituationBlockerView(
                    kind="execution_failure",
                    title="Исполнение шага завершилось ошибкой",
                    summary=str(run.get("failure_message") or run.get("failure_code") or "Исполнение завершилось ошибкой."),
                    severity="error",
                    detail_view="debug",
                    related_id=str(run.get("task_id") or ""),
                )
            )
            break

        for escalation in escalations[-3:]:
            blockers.append(
                SituationBlockerView(
                    kind="escalation",
                    title="Требуется внимание",
                    summary=escalation.summary,
                    severity=escalation.severity,
                    detail_view="review" if escalation.reason_code == "validation_failed" else "debug",
                    related_id=escalation.escalation_ticket_id,
                )
            )

        if review.status == "needs_changes":
            for issue in review.issues:
                blockers.append(
                    SituationBlockerView(
                        kind="review_issue",
                        title="Замечание ревью",
                        summary=issue.message,
                        severity=issue.severity,
                        detail_view="review",
                        related_id=review.artifact_id,
                    )
                )

        blocking = bool(blockers)
        if blocking:
            primary_blocker = blockers[0]
            primary_action = (
                ActionDescriptor(
                    kind="open_debug",
                    label="Открыть технические детали",
                    description="Посмотреть ошибку, трассировку и состояние шага.",
                    target_view="debug",
                    target_id=primary_blocker.related_id,
                    blocking=True,
                )
                if primary_blocker.detail_view == "debug"
                else ActionDescriptor(
                    kind="open_review",
                    label="Открыть замечания",
                    description="Посмотреть замечания и решить, что делать дальше.",
                    target_view=primary_blocker.detail_view,
                    target_id=primary_blocker.related_id,
                    blocking=True,
                )
            )
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Ошибка этапа" if primary_blocker.detail_view == "debug" else "Требуется внимание",
                headline="Проект остановлен из-за ошибки шага"
                if primary_blocker.detail_view == "debug"
                else "Проект остановлен на замечаниях",
                summary=primary_blocker.summary,
                blocking=True,
                primary_action=primary_action,
                secondary_actions=(
                    ActionDescriptor(
                        kind="open_state",
                        label="Состояние проекта",
                        description="Посмотреть readiness, gaps и контекст блокировки.",
                        target_view="state",
                    ),
                ),
                blockers=tuple(blockers),
            )

        completed = journey.completed_steps == journey.total_steps and journey.total_steps > 0
        if completed and review.status == "passed":
            spec = self._runtime.latest_artifact_by_role(context.workspace, "requirements_spec")
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Готово",
                headline="Техническое задание готово",
                summary="Все шаги recipe завершены, ревью пройдено.",
                blocking=False,
                primary_action=ActionDescriptor(
                    kind="open_artifact",
                    label="Открыть ТЗ",
                    description="Посмотреть итоговый артефакт требований.",
                    target_view="artifact",
                    target_id=spec.artifact_id if spec else None,
                ),
                secondary_actions=(
                    ActionDescriptor(
                        kind="open_journey",
                        label="Открыть путь выполнения",
                        description="Посмотреть весь пройденный маршрут проекта.",
                        target_view="journey",
                    ),
                ),
            )

        if active_tasks:
            first_task = active_tasks[0]
            step_title = next(
                (step.title for step in context.composed_recipe.steps if step.identifier == first_task.recipe_step_id),
                first_task.recipe_step_id,
            )
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Выполняется",
                headline=f"Сейчас выполняется шаг: {step_title}",
                summary="Система работает над очередным шагом проекта.",
                blocking=False,
                primary_action=ActionDescriptor(
                    kind="open_journey",
                    label="Открыть ход выполнения",
                    description="Посмотреть активный шаг и состояние recipe.",
                    target_view="journey",
                ),
            )

        if preview.outcome == "selected" and preview.selected_step_id is not None:
            step_title = next(
                (step.title for step in context.composed_recipe.steps if step.identifier == preview.selected_step_id),
                preview.selected_step_id,
            )
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Готов к продолжению",
                headline=f"Следующий шаг: {step_title}",
                summary="Проект готов к следующему действию. Его можно запустить прямо сейчас.",
                blocking=False,
                primary_action=ActionDescriptor(
                    kind="run_next",
                    label="Продолжить выполнение",
                    description="Запустить следующий допустимый шаг процесса.",
                    command_name="run-next",
                ),
                secondary_actions=(
                    ActionDescriptor(
                        kind="open_state",
                        label="Открыть состояние проекта",
                        description="Посмотреть readiness, gaps и текущее понимание проекта.",
                        target_view="state",
                    ),
                ),
            )

        blocking_gaps = [gap for gap in state.active_gaps.values() if gap.blocking]
        summary = preview.reasons[0] if preview.reasons else "Нет допустимых шагов для продолжения."
        if blocking_gaps:
            blockers = tuple(
                SituationBlockerView(
                    kind="gap",
                    title=gap.title,
                    summary=gap.description,
                    severity=gap.severity,
                    detail_view="state",
                    related_id=gap.identifier,
                )
                for gap in blocking_gaps[:3]
            )
        return ProjectSituationView(
            project_id=context.manifest.project_id,
            status_label="Ожидание решения",
            headline="Проект не может двигаться дальше автоматически",
            summary=summary,
            blocking=bool(blockers),
            primary_action=ActionDescriptor(
                kind="open_state",
                label="Открыть состояние проекта",
                description="Понять, какие gaps и readiness мешают продолжению.",
                target_view="state",
                blocking=bool(blockers),
            ),
            blockers=tuple(blockers),
        )

    def _build_timeline(self, context: ProjectContext) -> tuple[TimelineEntryView, ...]:
        entries: list[tuple[tuple[str, int], TimelineEntryView]] = []
        manifest = context.manifest
        entries.append(
            (
                (manifest.created_at, 0),
                TimelineEntryView(
                    sequence=0,
                    kind="project_created",
                    title="Проект создан",
                    summary="Инициализирован новый кейс и сформировано стартовое состояние проекта.",
                    status="info",
                    created_at=manifest.created_at,
                    detail_view="state",
                    entity_type="project",
                    entity_id=manifest.project_id,
                ),
            )
        )

        for event in self._runtime.list_problem_events(context.workspace):
            if event.patch_type == "SetGoalPatch":
                entries.append(
                    (
                        (event.created_at, 1),
                        TimelineEntryView(
                            sequence=0,
                            kind="goal_updated",
                            title="Цель проекта уточнена",
                            summary=str(event.payload.get("text", "Цель обновлена.")),
                            status="success",
                            created_at=event.created_at,
                            detail_view="state",
                            entity_type="problem_state",
                            entity_id=context.manifest.project_id,
                        ),
                    )
                )
            elif event.patch_type == "EnableDomainPackPatch":
                pack_ref = str(event.payload.get("pack_ref", ""))
                entries.append(
                    (
                        (event.created_at, 2),
                        TimelineEntryView(
                            sequence=0,
                            kind="domain_pack_enabled",
                            title="Подключено доменное расширение",
                            summary=f"Активирован доменный пакет {pack_ref}.",
                            status="info",
                            created_at=event.created_at,
                            detail_view="state",
                            entity_type="domain_pack",
                            entity_id=pack_ref,
                        ),
                    )
                )

        for artifact in self._runtime.list_artifacts(context.workspace):
            title, summary, status, detail_view = self._timeline_entry_for_artifact(context.workspace, artifact)
            entries.append(
                (
                    (artifact.created_at, 10),
                    TimelineEntryView(
                        sequence=0,
                        kind="artifact_created",
                        title=title,
                        summary=summary,
                        status=status,
                        created_at=artifact.created_at,
                        detail_view=detail_view,
                        entity_type="artifact",
                        entity_id=artifact.artifact_id,
                    ),
                )
            )

        for task in self._runtime.list_tasks(context.workspace):
            if task.status != "failed":
                continue
            step_title = next(
                (step.title for step in context.composed_recipe.steps if step.identifier == task.recipe_step_id),
                task.recipe_step_id,
            )
            entries.append(
                (
                    (task.updated_at, 15),
                    TimelineEntryView(
                        sequence=0,
                        kind="task_failed",
                        title=f"Шаг завершился ошибкой: {step_title}",
                        summary=self._failure_summary_for_task(
                            context.workspace,
                            task.task_id,
                            default=f"Шаг '{step_title}' завершился с ошибкой.",
                        ),
                        status="error",
                        created_at=task.updated_at,
                        detail_view="debug",
                        entity_type="task",
                        entity_id=task.task_id,
                    ),
                )
            )

        for run in self._runtime.list_execution_runs(context.workspace):
            if str(run.get("status")) != "failed":
                continue
            created_at = str(run.get("created_at") or run.get("updated_at") or "")
            entries.append(
                (
                    (created_at, 16),
                    TimelineEntryView(
                        sequence=0,
                        kind="execution_failed",
                        title="Исполнение шага завершилось ошибкой",
                        summary=str(run.get("failure_message") or run.get("failure_code") or "Во время исполнения произошла ошибка."),
                        status="error",
                        created_at=created_at,
                        detail_view="debug",
                        entity_type="execution_run",
                        entity_id=str(run.get("execution_run_id") or ""),
                    ),
                )
            )

        for run in self._runtime.list_validation_runs(context.workspace):
            if run.status != "failed":
                continue
            entries.append(
                (
                    (run.created_at, 17),
                    TimelineEntryView(
                        sequence=0,
                        kind="validation_failed",
                        title="Валидация не пройдена",
                        summary=run.findings[0].message if run.findings else "Валидация шага завершилась ошибкой.",
                        status="warning",
                        created_at=run.created_at,
                        detail_view="review",
                        entity_type="validation_run",
                        entity_id=run.validation_run_id,
                    ),
                )
            )

        for escalation in self._runtime.list_escalations(context.workspace):
            entries.append(
                (
                    (escalation.created_at, 20),
                    TimelineEntryView(
                        sequence=0,
                        kind="escalation",
                        title="Процесс остановлен",
                        summary=escalation.summary,
                        status="error",
                        created_at=escalation.created_at,
                        detail_view="review" if escalation.reason_code == "validation_failed" else "debug",
                        entity_type="escalation",
                        entity_id=escalation.escalation_ticket_id,
                    ),
                )
            )

        ordered = [item[1] for item in sorted(entries, key=lambda item: item[0])]
        normalized: list[TimelineEntryView] = []
        for sequence, item in enumerate(ordered, start=1):
            normalized.append(
                TimelineEntryView(
                    sequence=sequence,
                    kind=item.kind,
                    title=item.title,
                    summary=item.summary,
                    status=item.status,
                    created_at=item.created_at,
                    detail_view=item.detail_view,
                    entity_type=item.entity_type,
                    entity_id=item.entity_id,
                )
            )
        return tuple(normalized)

    def _timeline_entry_for_artifact(self, workspace: Path, artifact) -> tuple[str, str, str, str]:
        role_map = {
            "clarification_notes": ("Уточнена бизнес-цель", "Система собрала уточнение цели и критериев успеха.", "success", "artifact"),
            "request_fact_sheet": ("Выделены факты запроса", "Система извлекла явные факты, сущности и ожидаемые результаты из исходного брифа.", "success", "artifact"),
            "goal_hypothesis": ("Сформирована гипотеза цели", "Система зафиксировала рабочую гипотезу цели и успеха этапа.", "success", "artifact"),
            "constraint_inventory": ("Собраны ограничения", "Система выделила явные и подразумеваемые ограничения проекта.", "success", "artifact"),
            "ambiguity_gap_report": ("Разобраны неоднозначности", "Система нашла пробелы, противоречия и безопасные рабочие допущения.", "success", "artifact"),
            "normalized_request": ("Запрос нормализован", "Система собрала сводную нормализацию исходного бизнес-запроса.", "success", "artifact"),
            "stakeholder_map": ("Выделены стейкхолдеры", "Система собрала группы стейкхолдеров и их ожидания.", "success", "artifact"),
            "decision_ownership_matrix": ("Определены владельцы решений", "Система разложила ownership по ключевым решениям и согласованиям.", "success", "artifact"),
            "operating_model_outline": ("Собран черновой контур операционной модели", "Система описала поток работы, роли и риски передачи ответственности.", "success", "artifact"),
            "solution_option_inventory": ("Сформирован набор вариантов решения", "Система выделила жизнеспособные варианты решения для текущего этапа.", "success", "artifact"),
            "delivery_scope_definition": ("Определён состав поставки этапа", "Система зафиксировала, что именно должно быть поставлено на текущем этапе.", "success", "artifact"),
            "acceptance_model_definition": ("Определена модель приемки этапа", "Система зафиксировала критерии приемки, доказательства и обязательные согласования.", "success", "artifact"),
            "dependency_map": ("Собраны критические зависимости", "Система выделила входы, решения и зависимости, без которых этап не сможет двигаться дальше.", "success", "artifact"),
            "user_story_map": ("Собраны user story", "Определены роли, сценарии и граничные случаи.", "success", "artifact"),
            "alternatives_analysis": ("Разобраны альтернативы", "Система сравнила варианты решения и сформировала рекомендацию.", "success", "artifact"),
            "ui_requirements_outline": ("Разобраны пользовательские потоки интерфейса", "Добавлен доменный анализ UI и экранов.", "success", "artifact"),
            "requirements_spec": ("Подготовлен черновик ТЗ", "Сформирован первый структурированный вариант требований.", "success", "artifact"),
        }
        if artifact.artifact_role == "review_report":
            payload = json.loads(self._runtime.load_artifact_content(workspace, artifact.artifact_id))
            overall_status = payload.get("overall_status", "unknown")
            if overall_status == "passed":
                return (
                    "Ревью ТЗ пройдено",
                    payload.get("summary", "Черновик ТЗ можно принимать."),
                    "success",
                    "review",
                )
            return (
                "Ревью выявило замечания",
                payload.get("summary", "Черновик ТЗ требует доработки."),
                "warning",
                "review",
            )
        return role_map.get(
            artifact.artifact_role,
            ("Создан артефакт", f"Получен артефакт роли {artifact.artifact_role}.", "info", "artifact"),
        )

    def _artifact_validations(self, workspace: Path, artifact_id: str) -> tuple[ArtifactValidationView, ...]:
        items: list[ArtifactValidationView] = []
        for run in self._runtime.list_validation_runs(workspace):
            related_messages = [
                finding.message
                for finding in run.findings
                if artifact_id in finding.related_artifact_ids
            ]
            if not related_messages:
                continue
            items.append(
                ArtifactValidationView(
                    validation_run_id=run.validation_run_id,
                    status=run.status,
                    finding_messages=tuple(related_messages),
                    created_at=run.created_at,
                )
            )
        return tuple(items)

    def _step_status_summary(self, workspace: Path, recipe_step_id: str, status: str) -> str | None:
        tasks = [item for item in self._runtime.list_tasks(workspace) if item.recipe_step_id == recipe_step_id]
        if not tasks:
            return None
        tasks.sort(key=lambda item: item.updated_at, reverse=True)
        latest = tasks[0]
        if latest.status == "failed" or status == "failed":
            return self._failure_summary_for_task(
                workspace,
                latest.task_id,
                default=f"Последняя попытка шага завершилась ошибкой.",
            )
        if latest.status == "completed":
            return "Шаг успешно завершён и его результат принят."
        if latest.status == "in_progress":
            return "Шаг сейчас выполняется."
        if latest.status == "queued":
            return "Шаг поставлен в очередь на выполнение."
        if latest.status == "waiting_for_children":
            return "Шаг ожидает завершения вложенных задач."
        return None

    def _failure_summary_for_task(self, workspace: Path, task_id: str, *, default: str) -> str:
        validation_runs = [
            item
            for item in self._runtime.list_validation_runs(workspace)
            if item.task_id == task_id and item.status == "failed"
        ]
        validation_runs.sort(key=lambda item: item.created_at, reverse=True)
        if validation_runs and validation_runs[0].findings:
            return validation_runs[0].findings[0].message

        execution_runs = [
            item
            for item in self._runtime.list_execution_runs(workspace)
            if str(item.get("task_id")) == task_id and str(item.get("status")) == "failed"
        ]
        execution_runs.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
        if execution_runs:
            run = execution_runs[0]
            return str(run.get("failure_message") or run.get("failure_code") or default)

        task_events = [
            item
            for item in self._runtime.list_task_events(workspace, task_id=task_id)
            if item.event_type == "fail"
        ]
        task_events.sort(key=lambda item: item.created_at, reverse=True)
        if task_events:
            payload = task_events[0].payload or {}
            if isinstance(payload, dict):
                message = payload.get("error_message")
                if isinstance(message, str) and message.strip():
                    return message.strip()

        return default

    def _normalize_json_columns(self, payload: dict[str, object]) -> dict[str, object]:
        result = dict(payload)
        for field_name in ("output_artifact_ids_json", "trace_ids_json", "findings_json", "details_json"):
            if field_name in result and isinstance(result[field_name], str):
                try:
                    result[field_name] = json.loads(result[field_name])
                except json.JSONDecodeError:
                    pass
        return result

    def _signature(self, value: object) -> str:
        normalized = json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(normalized.encode("utf-8")).hexdigest()
