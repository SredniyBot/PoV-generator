from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

from ..common.errors import ConflictError
from ..common.serialization import to_primitive
from ..domain.project_state import ProjectManifest, ProjectState
from ..domain.registry import RegistrySnapshot
from ..domain.tasks import TaskRecord
from ..domain.workspace_views import (
    ActionDescriptor,
    ArtifactDetailView,
    ArtifactSectionView,
    ArtifactSkeletonView,
    ArtifactSummaryView,
    ArtifactValidationView,
    ArtifactVersionItemView,
    AttachmentView,
    CapabilityGapView,
    CheckpointSessionView,
    ContextManifestSummaryView,
    DecisionAlternativeView,
    DecisionItemView,
    DomainPackCatalogItemView,
    FailurePinView,
    FanOutMeta,
    ObjectiveCatalogItemView,
    ObjectiveProgressView,
    OverviewArtifactItem,
    OverviewClarificationItem,
    ProjectArtifactVersionsView,
    ProjectCheckpointsView,
    ProjectDebugView,
    ProjectDecisionsView,
    ProjectFailurePinsView,
    ProjectGapsView,
    ProjectListItemView,
    ProjectOverviewView,
    ProjectRequisitesView,
    ProjectReviewView,
    ProjectRollbackHistoryView,
    ProjectShellView,
    ProjectSituationView,
    ProjectStagesView,
    ProjectStateView,
    ProjectTaskGraphView,
    ProjectTimelineView,
    RequisiteItemView,
    ReviewIssueView,
    RollbackArtifactView,
    RollbackHistoryItemView,
    RollbackPreviewView,
    RollbackStepView,
    SituationBlockerView,
    StageFailingTaskView,
    StagePendingDecisionView,
    StageView,
    TaskNodeView,
    TimelineEntryView,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .planning_service import (
    PlanningService,
    SkeletonNode,
    count_skeleton_leaves,
    walk_composite_skeleton,
)
from .project_registry import ProjectRegistryResolver
from .registry_service import RegistryService
from .rollback_graph import collect_step_footprints, compute_rollback_set
from .workspace_catalog import WorkspaceCatalog, WorkspaceRef

logger = logging.getLogger(__name__)

ProjectionName = str


@dataclass(frozen=True)
class ProjectContext:
    workspace_ref: WorkspaceRef
    workspace: Path
    manifest: ProjectManifest
    state: ProjectState
    snapshot: RegistrySnapshot


class WorkspaceQueryService:
    DEFAULT_PROJECTIONS: tuple[ProjectionName, ...] = (
        "shell",
        "task_graph",
        "situation",
        "timeline",
        "artifacts",
        "attachments",
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
        registry_resolver: "ProjectRegistryResolver | None" = None,
    ) -> None:
        self._catalog = catalog
        self._registry_service = registry_service
        self._runtime = runtime
        self._planning_service = planning_service
        # Закреплённый граф проекта (Ф-pin): per-project просмотр идёт на снимке
        # реестра проекта. None — fallback на живой реестр (тесты/CLI).
        self._registry_resolver = registry_resolver

    def list_projects(self) -> tuple[ProjectListItemView, ...]:
        """Список всех проектов воркспейса.

        Каждый проект строится изолированно: если один из них не удаётся
        отрисовать целиком (например, его граф задач ссылается на шаблон,
        удалённый из реестра в новой версии), это не должно ронять весь
        список и оставлять оператора с пустым экраном. Такой проект всё
        равно попадает в выдачу — в деградированном виде с пометкой об
        ошибке, чтобы он не «исчезал» и причина была видна в логах.
        """
        # Реестр валидируется ОДИН раз на весь список (а не на каждый
        # проект): это глобальное свойство, не зависящее от конкретного
        # workspace. Снапшот переиспользуется при сборке каждого проекта.
        snapshot = self._validated_snapshot()
        items: list[ProjectListItemView] = []
        for workspace_ref in self._catalog.list_workspaces():
            try:
                items.append(self._build_project_list_item(workspace_ref, snapshot))
            except Exception as exc:  # noqa: BLE001 — изоляция на уровне проекта
                manifest = getattr(workspace_ref, "manifest", None)
                logger.warning(
                    "list_projects: не удалось построить полный вид проекта "
                    "%s (%s): %s",
                    getattr(manifest, "project_id", "<unknown>"),
                    getattr(manifest, "name", "<unknown>"),
                    exc,
                )
                degraded = self._degraded_list_item(workspace_ref, exc)
                if degraded is not None:
                    items.append(degraded)
        return tuple(sorted(items, key=lambda item: (item.updated_at, item.project_id), reverse=True))

    def _build_project_list_item(
        self, workspace_ref: WorkspaceRef, snapshot: RegistrySnapshot
    ) -> ProjectListItemView:
        """Построить полный элемент списка для одного проекта.

        Контекст загружается один раз (переданный ``snapshot`` исключает
        повторную валидацию реестра), планирование выполняется один раз, а
        situation и task-graph строятся из общего среза задач — вместо трёх
        независимых перезагрузок контекста и двух планирований, как было
        при reuse публичных методов ``project_situation`` /
        ``project_task_graph``.
        """
        context = self._load_context_by_ref(workspace_ref, snapshot=snapshot)
        # Один dry-run план на проект; обе проекции читают один срез задач.
        self._planning_service.plan(
            context.workspace, context.snapshot, mode="dry-run", record=False
        )
        tasks = self._runtime.list_tasks(context.workspace)
        situation = self._build_situation(context, tasks=tasks)
        task_graph = self._build_task_graph(context, tasks=tasks)
        current_title = self._find_current_title(task_graph.nodes, task_graph.current_task_id)
        return ProjectListItemView(
            project_id=context.manifest.project_id,
            name=context.manifest.name,
            status_label=situation.status_label,
            updated_at=context.state.process.updated_at,
            has_blockers=situation.blocking,
            current_step_title=current_title,
        )

    def _degraded_list_item(
        self, workspace_ref: WorkspaceRef, exc: Exception
    ) -> ProjectListItemView | None:
        """Минимальный элемент списка для проекта, который не загрузился.

        Берём только то, что гарантированно доступно из манифеста
        (id, имя, дата создания), не трогая граф задач/реестр. Для
        сортировки пытаемся взять ``updated_at`` из состояния процесса;
        если и оно не читается — откатываемся к ``created_at`` манифеста.
        Возвращаем ``None`` только если нет даже манифеста — тогда показать
        нечего.
        """
        manifest = getattr(workspace_ref, "manifest", None)
        if manifest is None:
            return None
        updated_at = manifest.created_at
        try:
            state = self._runtime.load_project_state(workspace_ref.workspace)
            updated_at = state.process.updated_at or manifest.created_at
        except Exception:  # noqa: BLE001 — деградация не должна падать вторично
            pass
        return ProjectListItemView(
            project_id=manifest.project_id,
            name=manifest.name,
            status_label="Ошибка загрузки",
            updated_at=updated_at,
            has_blockers=True,
            current_step_title=None,
        )

    def list_objectives(self) -> tuple[ObjectiveCatalogItemView, ...]:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно отобразить список целей.")
        return tuple(
            sorted(
                (
                    ObjectiveCatalogItemView(
                        objective_ref=objective.ref.as_string(),
                        title=objective.title,
                        root_task_ref=objective.root_task_ref.as_string(),
                        required_artifact_count=len(objective.done_artifact_refs),
                    )
                    for objective in snapshot.objectives.values()
                ),
                key=lambda item: item.title,
            )
        )


    def list_methodology_packs(self) -> tuple[dict, ...]:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно отобразить список методологий.")
        items: list[dict] = []
        for pack in snapshot.methodology_packs.values():
            items.append(
                {
                    "pack_ref": pack.ref.as_string(),
                    "title": pack.title,
                    "description": pack.description,
                    "status": pack.status,
                    "stage_execution_mode": pack.stage_execution_mode,
                    "stages": [
                        {
                            "id": stage.identifier,
                            "title": stage.title,
                            "description": stage.description,
                            "produces": [
                                {"field": p.field_name, "type": p.field_type, "required": p.required}
                                for p in stage.produces
                            ],
                            "rules": [
                                {"id": r.identifier, "if": r.if_expression}
                                for r in stage.rules
                            ],
                        }
                        for stage in pack.stages
                    ],
                    "required_stages": list(pack.reasoning_artifact.required_stages),
                    "optional_stages": list(pack.reasoning_artifact.optional_stages),
                }
            )
        return tuple(sorted(items, key=lambda item: item["pack_ref"]))

    def list_domain_packs(self) -> tuple[DomainPackCatalogItemView, ...]:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно отобразить список доменных пакетов.")
        return tuple(
            sorted(
                (
                    DomainPackCatalogItemView(
                        pack_ref=pack.ref.as_string(),
                        name=pack.title,
                        domain=pack.domain,
                        description=pack.description,
                        status=pack.status,
                        entry_signals=pack.entry_signals,
                    )
                    for pack in snapshot.domain_packs.values()
                ),
                key=lambda item: (item.domain, item.name, item.pack_ref),
            )
        )

    def project_shell(self, project_id: str) -> ProjectShellView:
        context = self._load_context(project_id)
        situation = self._build_situation(context)
        objective = context.snapshot.resolve_objective(context.manifest.objective_ref)
        return ProjectShellView(
            project_id=context.manifest.project_id,
            name=context.manifest.name,
            business_request=context.state.manifest.business_request,
            objective_ref=context.manifest.objective_ref,
            active_domain_packs=tuple(sorted(context.state.process.active_domain_pack_records.keys())),
            goal=context.state.knowledge.goal_statement(),
            status_label=situation.status_label,
            updated_at=context.state.process.updated_at,
            objective_history=context.manifest.objective_history,
            compatible_next_objectives=tuple(
                ref.as_string() for ref in objective.compatible_next_objectives
            ),
            objective_complete=self._objective_done(context),
        )

    def project_task_graph(self, project_id: str) -> ProjectTaskGraphView:
        context = self._load_context(project_id)
        return self._build_task_graph(context)

    def _build_task_graph(
        self, context: ProjectContext, *, tasks: list[TaskRecord] | None = None
    ) -> ProjectTaskGraphView:
        """Построить проекцию графа задач из контекста.

        Если ``tasks`` не передан — выполняем dry-run план и читаем задачи
        сами (поведение standalone-вызова). Если передан (агрегирующий путь
        уже спланировал и прочитал задачи) — переиспользуем срез, не
        планируя повторно.
        """
        if tasks is None:
            self._planning_service.plan(
                context.workspace, context.snapshot, mode="dry-run", record=False
            )
            tasks = self._runtime.list_tasks(context.workspace)
        leaf_tasks = [task for task in tasks if task.template_type == "leaf"]
        ready = next((task for task in leaf_tasks if task.status == "ready"), None)
        nodes = self._build_task_tree(context.workspace, tasks, ready.task_id if ready else None, context.snapshot)
        try:
            title = context.snapshot.resolve_objective(context.manifest.objective_ref).title
        except Exception:
            title = ""
        return ProjectTaskGraphView(
            project_id=context.manifest.project_id,
            objective_ref=context.manifest.objective_ref,
            current_task_id=ready.task_id if ready else None,
            completed_leaf_tasks=sum(1 for task in leaf_tasks if task.status == "completed"),
            total_leaf_tasks=len(leaf_tasks),
            nodes=nodes,
            objective_state="active",
            title=title,
        )

    def project_objective_task_graph(
        self, project_id: str, objective_ref: str
    ) -> ProjectTaskGraphView:
        """Граф задач ЛЮБОГО гейта проекта (для подвкладок, Ф1):
        - активный гейт → живой граф (как ``project_task_graph``);
        - завершённый → сохранённые задачи этого objective (read-only);
        - ещё не запущенный → статический скелет из реестра, fan-out не раскрыт.
        Задачи неактивных гейтов помечаются ``available=False``.
        """
        from ..common.errors import NotFoundError

        context = self._load_context(project_id)
        snapshot = context.snapshot
        try:
            objective = snapshot.resolve_objective(objective_ref)
        except Exception as exc:
            raise NotFoundError(f"Цель '{objective_ref}' не найдена в реестре проекта.") from exc
        title = objective.title
        active_ref = context.manifest.objective_ref
        if objective_ref == active_ref:
            return self._build_task_graph(context)

        history = set(context.manifest.objective_history or ())
        if objective_ref in history:
            tasks = [
                t
                for t in self._runtime.list_tasks(context.workspace)
                if t.objective_ref == objective_ref
            ]
            nodes = self._build_task_tree(
                context.workspace, tasks, None, snapshot, available=False
            )
            leaf = [t for t in tasks if t.template_type == "leaf"]
            return ProjectTaskGraphView(
                project_id=context.manifest.project_id,
                objective_ref=objective_ref,
                current_task_id=None,
                completed_leaf_tasks=sum(1 for t in leaf if t.status == "completed"),
                total_leaf_tasks=len(leaf),
                nodes=nodes,
                objective_state="done",
                title=title,
            )

        # locked: статический скелет из реестра (без записи в runtime).
        active_pack_refs = tuple(sorted(context.state.process.active_domain_pack_records.keys()))
        skeleton = walk_composite_skeleton(snapshot, objective.root_task_ref, active_pack_refs)
        root_node = self._skeleton_node_view(skeleton, objective_ref, parent_id=None, path="root")
        return ProjectTaskGraphView(
            project_id=context.manifest.project_id,
            objective_ref=objective_ref,
            current_task_id=None,
            completed_leaf_tasks=0,
            total_leaf_tasks=count_skeleton_leaves(skeleton),
            nodes=(root_node,),
            objective_state="locked",
            title=title,
        )

    def _skeleton_node_view(
        self, node: SkeletonNode, objective_ref: str, *, parent_id: str | None, path: str
    ) -> TaskNodeView:
        node_id = f"skeleton:{objective_ref}:{path}"
        children = tuple(
            self._skeleton_node_view(child, objective_ref, parent_id=node_id, path=f"{path}.{idx}")
            for idx, child in enumerate(node.children)
        )
        return TaskNodeView(
            task_id=node_id,
            task_key=node_id,
            parent_task_id=parent_id,
            title=node.title,
            template_ref=node.template_ref,
            template_type=node.template_type,
            status="candidate",
            status_summary=None,
            origin_kind=node.origin_kind,
            origin_ref=node.origin_ref,
            slot_id=node.slot_id,
            depth=node.depth,
            retryable=False,
            is_current=False,
            children=children,
            available=False,
        )

    def project_situation(self, project_id: str) -> ProjectSituationView:
        context = self._load_context(project_id)
        return self._build_situation(context)

    def project_timeline(self, project_id: str, *, after_sequence: int = 0) -> ProjectTimelineView:
        context = self._load_context(project_id)
        entries = self._build_timeline(context)
        filtered = tuple(entry for entry in entries if entry.sequence > after_sequence)
        return ProjectTimelineView(project_id=context.manifest.project_id, entries=filtered, total_entries=len(entries))

    # ---- v3.0 — Decision ledger -----------------------------------------------

    def decisions_for_artifact(
        self,
        project_id: str,
        artifact_id: str,
        *,
        include_details: bool = True,
    ) -> tuple["DecisionItemView", ...]:
        """Решения, которые были приняты при сборке этого артефакта.

        Связь определяется через ``Decision.affected_artifact_ids``,
        которое формирует ExecutionService при сохранении (см. также
        scenario «решение → артефакт» в spec v3.0).
        """
        context = self._load_context(project_id)
        all_decisions = self._runtime.list_decisions(context.workspace, project_id=project_id)
        relevant = [d for d in all_decisions if artifact_id in d.affected_artifact_ids]
        return tuple(
            self._decision_view(d, include_details=include_details) for d in relevant
        )

    def project_decisions(
        self,
        project_id: str,
        *,
        level: str | None = None,
        status: str | None = None,
        include_details: bool = True,
    ) -> ProjectDecisionsView:
        """Реестр решений проекта с агрегатами по уровням и статусам.

        Опциональные фильтры ``level`` / ``status`` сужают ``items``,
        но **не** меняют агрегатные счётчики — они всегда считаются по
        полному реестру проекта. Это даёт UI стабильные счётчики в
        навигации независимо от текущего фильтра.

        ``surfaced_total`` / ``surfaced_pending`` — счётчики «на твоём
        уровне» в текущем режиме проекта. Это основной индикатор для
        пользователя: «N решений ждут моего внимания».
        """
        from ..domain.decisions import should_surface_to_user

        context = self._load_context(project_id)
        mode = context.state.process.clarification_mode

        # Полный реестр для счётчиков
        all_decisions = self._runtime.list_decisions(
            context.workspace, project_id=project_id
        )
        # Отфильтрованный для items
        if level is not None or status is not None:
            filtered = self._runtime.list_decisions(
                context.workspace,
                project_id=project_id,
                level=level,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
            )
        else:
            filtered = all_decisions

        # «На твоём уровне в этом режиме» — это про режим, не про фильтр
        # пользователя. Поэтому считается от all_decisions.
        surfaced_total = sum(1 for d in all_decisions if should_surface_to_user(d, mode))
        surfaced_pending = sum(
            1
            for d in all_decisions
            if should_surface_to_user(d, mode) and d.status == "proposed"
        )

        return ProjectDecisionsView(
            project_id=project_id,
            mode=mode,
            surfaced_total=surfaced_total,
            surfaced_pending=surfaced_pending,
            business_count=sum(1 for d in all_decisions if d.effective_level == "business"),
            architecture_count=sum(1 for d in all_decisions if d.effective_level == "architecture"),
            detail_count=sum(1 for d in all_decisions if d.effective_level == "detail"),
            proposed_count=sum(1 for d in all_decisions if d.status == "proposed"),
            accepted_count=sum(1 for d in all_decisions if d.status == "accepted_default"),
            overridden_count=sum(1 for d in all_decisions if d.status == "user_overridden"),
            low_confidence_count=sum(1 for d in all_decisions if d.is_low_confidence),
            items=tuple(
                self._decision_view(d, include_details=include_details)
                for d in filtered
            ),
        )

    def decision_detail(self, project_id: str, decision_id: str) -> DecisionItemView:
        context = self._load_context(project_id)
        decision = self._runtime.get_decision(context.workspace, decision_id)
        if decision.project_id != project_id:
            # Защита от scope-confusion: id не должен открывать чужой проект
            from ..common.errors import NotFoundError
            raise NotFoundError(f"decision {decision_id!r} не принадлежит проекту {project_id!r}")
        return self._decision_view(decision)

    # ---- v3.0 — Checkpoint sessions ------------------------------------------

    def project_checkpoints(self, project_id: str) -> ProjectCheckpointsView:
        """Все checkpoint-сессии проекта с pending_count для бэйджа."""
        context = self._load_context(project_id)
        sessions = self._runtime.list_checkpoint_sessions(
            context.workspace, project_id=project_id
        )
        items = tuple(
            self._checkpoint_session_view(context.workspace, session) for session in sessions
        )
        return ProjectCheckpointsView(
            project_id=project_id,
            pending_count=sum(1 for s in sessions if s.status == "pending"),
            items=items,
        )

    def checkpoint_session_detail(
        self, project_id: str, session_id: str
    ) -> CheckpointSessionView:
        """Детали одной сессии. Scope-protected: id привязан к проекту."""
        context = self._load_context(project_id)
        session = self._runtime.get_checkpoint_session(context.workspace, session_id)
        if session.project_id != project_id:
            from ..common.errors import NotFoundError
            raise NotFoundError(
                f"checkpoint session {session_id!r} не принадлежит проекту {project_id!r}"
            )
        return self._checkpoint_session_view(context.workspace, session)

    def _checkpoint_session_view(self, workspace, session) -> CheckpointSessionView:
        """Развернуть сессию: подтянуть Decision-объекты по id."""
        decisions = tuple(
            self._decision_view(self._runtime.get_decision(workspace, decision_id))
            for decision_id in session.decision_ids
        )
        return CheckpointSessionView(
            session_id=session.session_id,
            project_id=session.project_id,
            task_id=session.task_id,
            task_title=session.task_title,
            artifact_role=session.artifact_role,
            status=session.status,
            created_at=session.created_at,
            finalized_at=session.finalized_at,
            finalized_by=session.finalized_by,
            decisions=decisions,
        )

    def _decision_view(self, decision, *, include_details: bool = True) -> DecisionItemView:
        chosen = decision.chosen_alternative
        return DecisionItemView(
            decision_id=decision.decision_id,
            project_id=decision.project_id,
            title=decision.title,
            description=decision.description_without_category if include_details else "",
            category=decision.normalized_category,
            level=decision.effective_level,
            raw_level=decision.level,
            level_rationale=decision.level_rationale if include_details else "",
            rationale=decision.rationale if include_details else "",
            chosen_option_id=decision.chosen_option_id,
            chosen_option_label=chosen.label if chosen else "",
            alternatives=tuple(
                DecisionAlternativeView(
                    option_id=alt.option_id,
                    label=alt.label,
                    description=alt.description,
                    pros=alt.pros,
                    cons=alt.cons,
                    confidence=alt.confidence,
                    is_chosen=(alt.option_id == decision.chosen_option_id),
                )
                for alt in decision.alternatives
            ) if include_details else (),
            confidence=decision.confidence,
            is_low_confidence=decision.is_low_confidence,
            status=decision.status,
            source=decision.source,
            source_task_id=decision.source_task_id,
            affected_artifact_ids=decision.affected_artifact_ids,
            depends_on_decision_ids=decision.depends_on_decision_ids,
            user_action=decision.user_action,
            was_user_modified=decision.was_user_modified,
            user_free_text_answer=decision.user_free_text_answer,
            created_at=decision.created_at,
            updated_at=decision.updated_at,
            answer_mode=decision.answer_mode,
            chosen_option_ids=decision.chosen_option_ids,
            user_verified=decision.user_verified,
            user_verified_at=decision.user_verified_at,
            details_included=include_details,
        )

    def project_artifacts(self, project_id: str) -> tuple[ArtifactSummaryView, ...]:
        context = self._load_context(project_id)
        return tuple(
            ArtifactSummaryView(
                artifact_id=artifact.artifact_id,
                artifact_role=artifact.artifact_role,
                title=artifact.title,
                created_at=artifact.created_at,
                created_by_task_id=artifact.created_by_task_id,
                has_markdown=(context.workspace / artifact.storage_path.replace(".json", ".md")).exists(),
                overall_confidence=artifact.metadata.overall_confidence,
                is_low_confidence=artifact.is_low_confidence,
                user_verified=artifact.user_verified,
            )
            for artifact in self._runtime.list_artifacts(context.workspace)
        )

    def project_attachments(self, project_id: str) -> tuple[AttachmentView, ...]:
        context = self._load_context(project_id)
        return tuple(
            AttachmentView(
                attachment_id=attachment.attachment_id,
                original_filename=attachment.original_filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                extraction_status=attachment.extraction_status,
                extraction_error=attachment.extraction_error,
                used_in_context=attachment.used_in_context,
                can_delete=attachment.can_delete,
                created_at=attachment.created_at,
            )
            for attachment in self._runtime.list_attachments(context.workspace)
        )

    def artifact_detail(self, project_id: str, artifact_id: str) -> ArtifactDetailView:
        context = self._load_context(project_id)
        artifact = self._runtime.load_artifact(context.workspace, artifact_id)
        markdown_path = context.workspace / artifact.storage_path.replace(".json", ".md")
        # Учёт токенов задачи, создавшей артефакт (агрегат всех её вызовов).
        usage = None
        if artifact.created_by_task_id is not None:
            usage = self._runtime.llm_usage_for_task(context.workspace, artifact.created_by_task_id)
        return ArtifactDetailView(
            artifact_id=artifact.artifact_id,
            artifact_role=artifact.artifact_role,
            title=artifact.title,
            description=artifact.description,
            created_at=artifact.created_at,
            created_by_task_id=artifact.created_by_task_id,
            template_ref=artifact.metadata.template_ref,
            json_content=self._runtime.load_artifact_content(context.workspace, artifact.artifact_id),
            markdown_content=markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else None,
            validations=self._artifact_validations(context.workspace, artifact.artifact_id),
            # Metadata Этапов 1 + 5 (раньше не выводилась в UI).
            artifact_kind=artifact.artifact_kind,
            provider=artifact.metadata.provider,
            model=artifact.metadata.model,
            complexity=artifact.metadata.complexity,
            methodology_pack_ref=artifact.metadata.methodology_pack_ref,
            merge_strategy=artifact.metadata.merge_strategy,
            used_position_ids=artifact.metadata.used_position_ids,
            input_artifact_ids=artifact.relations.input_artifact_ids,
            parent_artifact_id=artifact.relations.parent_artifact_id,
            is_superseded=artifact.is_superseded,
            overall_confidence=artifact.metadata.overall_confidence,
            is_low_confidence=artifact.is_low_confidence,
            user_verified=artifact.user_verified,
            user_verified_at=artifact.user_verified_at,
            signed_off=artifact.signed_off,
            signed_off_at=artifact.signed_off_at,
            token_usage={k: dict(v) for k, v in artifact.metadata.token_usage.items()},
            usage_input_tokens=usage.input_tokens if usage else None,
            usage_output_tokens=usage.output_tokens if usage else None,
            usage_total_tokens=usage.total_tokens if usage else None,
            usage_source=("estimated" if usage and usage.has_estimated else "actual") if usage else None,
            usage_call_count=usage.call_count if usage else 0,
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
        return ProjectReviewView(
            project_id=context.manifest.project_id,
            status=payload.get("overall_status", "unknown"),
            summary=payload.get("summary"),
            strengths=tuple(payload.get("strengths", [])),
            issues=tuple(ReviewIssueView(severity=item["severity"], message=item["message"]) for item in payload.get("issues", [])),
            recommendations=tuple(payload.get("recommendations", [])),
            artifact_id=artifact.artifact_id,
            updated_at=artifact.created_at,
        )

    def project_requisites(self, project_id: str) -> ProjectRequisitesView:
        """Реквизиты — требуемые от пользователя входные данные.

        Ф5: агрегируем из двух источников — предусловий артефакта
        реализуемости и поля ``requisites`` модели компонентов (архитектура).
        Факт предоставления (requisite_provisions) накладывается сверху. Вся
        логика — в :func:`gather_requisites` (используется и шлюзом перехода).
        """
        context = self._load_context(project_id)
        items, source_id, updated = gather_requisites(self._runtime, context.workspace)
        return ProjectRequisitesView(
            project_id=context.manifest.project_id,
            status="ready" if source_id is not None else "missing",
            items=items,
            source_artifact_id=source_id,
            updated_at=updated,
        )

    def project_capability_gaps(self, project_id: str) -> ProjectGapsView:
        """Зоны роста — требования, не закрытые ни одним умением каталога.

        Фаза 3: выводим из артефакта реализуемости (пункты без covered_by).
        Разбор изолирован в :func:`_extract_gaps`.
        """
        context = self._load_context(project_id)
        artifact = self._runtime.latest_artifact_by_role(
            context.workspace, "feasibility_assessment"
        )
        if artifact is None:
            return ProjectGapsView(
                project_id=context.manifest.project_id,
                status="missing",
                items=(),
                source_artifact_id=None,
                updated_at=None,
            )
        payload = json.loads(
            self._runtime.load_artifact_content(context.workspace, artifact.artifact_id)
        )
        return ProjectGapsView(
            project_id=context.manifest.project_id,
            status="ready",
            items=_extract_gaps(payload),
            source_artifact_id=artifact.artifact_id,
            updated_at=artifact.created_at,
        )

    def project_overview(self, project_id: str) -> ProjectOverviewView:
        context = self._load_context(project_id)
        state = context.state
        manifest = context.manifest
        snapshot = context.snapshot

        # Прогресс по done_when (артефакты + gate'ы) — вынесен в общий хелпер
        # _objective_progress, чтобы переиспользовать в проекции этапов (stages).
        objective = snapshot.resolve_objective(manifest.objective_ref)
        progress = self._objective_progress(context, objective)
        artifacts_required = progress.artifacts_required
        artifacts_ready = progress.artifacts_ready
        gates_required = progress.gates_required
        gates_passed = progress.gates_passed

        # Открытые proposed-Decisions — для critical-блока и current_activity.
        open_decisions = [
            d
            for d in self._runtime.list_decisions(
                context.workspace, project_id=manifest.project_id
            )
            if d.status == "proposed"
        ]

        # v3.1: «критичные открытые уточнения» = proposed-Decisions
        # business-уровня (= наивысший приоритет в decision-модели) или
        # low-confidence. Маппинг level → priority выбран так:
        #   business → critical, architecture → high, detail → medium.
        level_to_priority = {
            "business": "critical",
            "architecture": "high",
            "detail": "medium",
        }
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        critical_decisions = [
            d
            for d in open_decisions
            if d.effective_level in {"business", "architecture"}
            or d.is_low_confidence
        ]
        critical_decisions.sort(
            key=lambda d: priority_order.get(
                level_to_priority.get(d.effective_level, "medium"), 9
            )
        )
        critical_items = tuple(
            OverviewClarificationItem(
                clarification_id=d.decision_id,
                title=d.title,
                priority=level_to_priority.get(d.effective_level, "medium"),
                blocking_scope="task" if d.source_task_id else "none",
                source_type=d.source,
            )
            for d in critical_decisions[:5]
        )

        # Ключевые артефакты — последние primary
        all_artifacts = list(self._runtime.list_artifacts(context.workspace))
        primary_artifacts = sorted(
            (a for a in all_artifacts if a.artifact_kind == "primary"),
            key=lambda a: a.created_at,
            reverse=True,
        )[:5]
        key_artifacts = tuple(
            OverviewArtifactItem(
                artifact_id=a.artifact_id,
                artifact_role=a.artifact_role,
                title=a.title,
                created_at=a.created_at,
            )
            for a in primary_artifacts
        )

        active_methodology = next(iter(state.process.active_methodology_pack_records.keys()), None)

        # Stage summary — простое описание
        if artifacts_ready == 0:
            stage_summary = "Старт. Подготовка задач."
        elif artifacts_ready < artifacts_required:
            stage_summary = f"В работе: {artifacts_ready}/{artifacts_required} артефактов готовы."
        elif gates_passed < gates_required:
            stage_summary = "Артефакты готовы, ожидаем согласования gate."
        else:
            stage_summary = "Цель завершена."

        # Current activity — упрощённо: показываем сколько open уточнений и есть ли next task
        if critical_items:
            current_activity = f"Ожидаем ответа на {len(critical_items)} критичных уточнений."
        else:
            current_activity = "Система выполняет следующие задачи."

        return ProjectOverviewView(
            project_id=manifest.project_id,
            name=manifest.name,
            objective_ref=manifest.objective_ref,
            stage_summary=stage_summary,
            current_activity=current_activity,
            objective_progress=progress,
            critical_clarifications=critical_items,
            key_artifacts=key_artifacts,
            active_methodology=active_methodology,
            active_domain_packs=tuple(sorted(state.process.active_domain_pack_records.keys())),
            clarification_mode=state.process.clarification_mode,
            updated_at=state.process.updated_at,
        )

    def _objective_progress(self, context: ProjectContext, objective) -> ObjectiveProgressView:
        """Прогресс одного objective: артефакты ready/required + gate'ы passed/required.

        Вынесено из project_overview, чтобы считать прогресс для любого этапа
        в проекции stages (а не только активного). Поведение для активного
        objective идентично прежнему инлайну (страхуется тестами).
        """
        artifacts = list(self._runtime.list_artifacts(context.workspace))
        artifact_roles_present = {a.artifact_role for a in artifacts}
        artifacts_required = len(objective.done_artifact_refs)
        artifacts_ready = sum(
            1
            for ref in objective.done_artifact_refs
            if ref.identifier.rsplit(".", 1)[-1] in artifact_roles_present
        )
        # Ф3: human_approval-гейт пройден, если итоговый артефакт цели
        # согласован с заказчиком (signed_off) — не по решению в реестре.
        key_artifact_id = self._objective_key_artifact_id(context, objective)
        key_signed_off = bool(key_artifact_id) and any(
            a.artifact_id == key_artifact_id and a.signed_off for a in artifacts
        )
        gates_required = len(objective.done_gate_refs)
        gates_passed = 0
        artifacts_by_role: dict[str, list] = {}
        for art in artifacts:
            artifacts_by_role.setdefault(art.artifact_role, []).append(art)
        snapshot = context.snapshot
        for gate_ref in objective.done_gate_refs:
            try:
                gate = snapshot.resolve_quality_gate(gate_ref)
            except Exception:
                continue

            if gate.check_type == "human_approval":
                if key_signed_off:
                    gates_passed += 1
                continue

            # automated_review и пр.: фактическое прохождение, не безусловно.
            required_roles = (
                list(gate.required_artifact_roles)
                if hasattr(gate, "required_artifact_roles")
                else []
            )
            all_present = (
                all(role in artifacts_by_role for role in required_roles)
                if required_roles
                else False
            )
            if not all_present:
                continue

            review_ok = True
            if "review_report" in required_roles:
                review_records = artifacts_by_role.get("review_report") or []
                if not review_records:
                    review_ok = False
                else:
                    latest_review = max(review_records, key=lambda a: a.created_at)
                    try:
                        payload = json.loads(
                            self._runtime.load_artifact_content(
                                context.workspace, latest_review.artifact_id
                            )
                        )
                        status = str(payload.get("overall_status", "")).lower()
                        review_ok = status in {"passed", "passed_with_remarks"}
                    except Exception:
                        review_ok = False
            if review_ok:
                gates_passed += 1

        return ObjectiveProgressView(
            artifacts_required=artifacts_required,
            artifacts_ready=artifacts_ready,
            gates_required=gates_required,
            gates_passed=gates_passed,
        )

    def project_stages(self, project_id: str) -> ProjectStagesView:
        """Проекция этапов (gate stepper): цепочка objective'ов проекта.

        history (done) → active → forward-walk compatible_next (locked).
        Прогресс — для каждого этапа; ошибки/блокировки — только для активного
        (см. StageView docstring про реплан по stable-key).
        """
        context = self._load_context(project_id)
        manifest = context.manifest
        snapshot = context.snapshot
        active_ref = manifest.objective_ref

        # 1. Упорядоченная цепочка этапов.
        ordered: list[tuple[str, str]] = [
            (ref, "done") for ref in manifest.objective_history
        ]
        ordered.append((active_ref, "active"))
        seen = {ref for ref, _ in ordered}
        cursor = active_ref
        while True:
            try:
                spec = snapshot.resolve_objective(cursor)
            except Exception:
                break
            nexts = [ref.as_string() for ref in spec.compatible_next_objectives]
            if not nexts:
                break
            nxt = nexts[0]  # линейный степпер: берём первую ветку (R3)
            if nxt in seen:
                break
            ordered.append((nxt, "locked"))
            seen.add(nxt)
            cursor = nxt

        # 2. Прогресс + (для активного) скоуп ошибок по objective_ref задач.
        all_tasks = self._runtime.list_tasks(context.workspace)
        stages: list[StageView] = []
        for ref, state in ordered:
            try:
                spec = snapshot.resolve_objective(ref)
            except Exception:
                continue  # деградируем: пропускаем нерезолвящийся (дрейф реестра)
            progress = self._objective_progress(context, spec)
            key_artifact_id = self._objective_key_artifact_id(context, spec)
            stage_signed_off = self._human_approval_gate_signed_off(context, spec)
            failed_count = 0
            blocked_count = 0
            awaiting_signoff = 0
            failing: list[StageFailingTaskView] = []
            pending_decisions: list[StagePendingDecisionView] = []
            if state == "active":
                # «Ждут решений» = открытые proposed-Decisions (то, что реально
                # требует ответа пользователя). Карта по source_task_id — чтобы
                # отличить actionable-блокировку (задача ждёт решения) от обычной
                # очерёдности (задача ждёт upstream-артефакт — НЕ показываем).
                open_decisions = [
                    d
                    for d in self._runtime.list_decisions(
                        context.workspace, project_id=manifest.project_id
                    )
                    if d.status == "proposed"
                ]
                clar_by_task: dict[str, int] = {}
                for d in open_decisions:
                    if d.source_task_id:
                        clar_by_task[d.source_task_id] = clar_by_task.get(d.source_task_id, 0) + 1
                awaiting_signoff = len(open_decisions)
                pending_decisions = [
                    StagePendingDecisionView(
                        decision_id=d.decision_id,
                        title=d.title,
                        level=d.effective_level,
                    )
                    for d in open_decisions
                ]
                for task in all_tasks:
                    if task.objective_ref != ref:
                        continue
                    if task.status == "failed":
                        failed_count += 1
                        failing.append(self._stage_failing_task(task, retryable=True))
                    elif task.status == "blocked" and clar_by_task.get(task.task_id):
                        blocked_count += 1
                        failing.append(self._stage_failing_task(task, retryable=False))
            stages.append(
                StageView(
                    objective_ref=ref,
                    title=spec.title,
                    state=state,
                    is_current=state == "active",
                    artifacts_required=progress.artifacts_required,
                    artifacts_ready=progress.artifacts_ready,
                    gates_required=progress.gates_required,
                    gates_passed=progress.gates_passed,
                    failed_count=failed_count,
                    blocked_count=blocked_count,
                    awaiting_signoff=awaiting_signoff,
                    failing_tasks=tuple(failing),
                    pending_decisions=tuple(pending_decisions),
                    key_artifact_id=key_artifact_id,
                    signed_off=stage_signed_off,
                )
            )

        active_spec = snapshot.resolve_objective(active_ref)
        return ProjectStagesView(
            project_id=manifest.project_id,
            objective_ref=active_ref,
            stages=tuple(stages),
            next_objective_refs=tuple(
                ref.as_string() for ref in active_spec.compatible_next_objectives
            ),
            objective_complete=self._objective_done(context),
            # Ф5: непредоставленные блокирующие реквизиты держат переход на
            # реализацию (UI гасит кнопку и показывает, чего не хватает).
            blocked_by_requisites=blocking_requisites_unprovided(
                self._runtime, context.workspace
            ),
        )

    def _stage_failing_task(self, task: TaskRecord, *, retryable: bool) -> StageFailingTaskView:
        return StageFailingTaskView(
            task_id=task.task_id,
            title=task.title,
            status=task.status,
            reason=task.error_message or self._status_summary(task) or "",
            retryable=retryable,
        )

    def project_state(self, project_id: str) -> ProjectStateView:
        context = self._load_context(project_id)
        state = context.state
        process = state.process
        knowledge = state.knowledge
        try:
            ledger_decisions = self._runtime.list_decisions(
                context.workspace,
                project_id=state.manifest.project_id,
            )
        except Exception:
            ledger_decisions = []
        return ProjectStateView(
            project_id=state.manifest.project_id,
            goal=knowledge.goal_statement(),
            active_gaps=tuple(
                sorted(
                    (to_primitive(item) for item in process.active_gaps.values()),
                    key=lambda item: item["identifier"],
                )
            ),
            assumptions=tuple(
                sorted(
                    (to_primitive(item) for item in knowledge.by_type("assumption")),
                    key=lambda item: item["identifier"],
                )
            ),
            decisions=tuple(
                to_primitive(self._decision_view(decision))
                for decision in ledger_decisions
            ),
            readiness=tuple(
                sorted(
                    (to_primitive(item) for item in process.readiness.values()),
                    key=lambda item: item["dimension"],
                )
            ),
            known_facts=tuple(
                sorted(
                    (to_primitive(item) for item in knowledge.by_type("fact")),
                    key=lambda item: item["identifier"],
                )
            ),
            active_domain_packs=tuple(
                sorted(
                    (to_primitive(item) for item in process.active_domain_pack_records.values()),
                    key=lambda item: item["ref"],
                )
            ),
            active_methodology_packs=tuple(
                sorted(
                    (to_primitive(item) for item in process.active_methodology_pack_records.values()),
                    key=lambda item: item["ref"],
                )
            ),
            clarification_mode=process.clarification_mode,
            root_task_id=process.root_task_id,
            updated_at=process.updated_at,
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
            # v3.1: вместо двух legacy-полей clarification_* — единый список Decisions.
            decisions=tuple(
                to_primitive(item)
                for item in self._runtime.list_decisions(
                    context.workspace, project_id=context.manifest.project_id
                )
            ),
            llm_usage=tuple(to_primitive(item) for item in self._runtime.list_llm_usage(context.workspace)),
            llm_usage_total=(
                to_primitive(project_usage)
                if (project_usage := self._runtime.llm_usage_for_project(context.workspace)) is not None
                else None
            ),
        )

    # ------------------------------------------------------------------
    # L6 design extensions (P3 v2 skeleton, P5 failure pins, P7 decisions, P8 versions)
    # Все методы — read-only поверх существующих данных, без миграций.
    # ------------------------------------------------------------------

    def artifact_skeleton(self, project_id: str, artifact_id: str) -> ArtifactSkeletonView:
        """P3 v2: skeleton артефакта со статусами разделов для главного экрана.

        Эвристика парсинга json_content:
        - dict с полем `sections: list[{id, title, content}]` → используем как есть
        - dict без `sections` → top-level ключи (кроме служебных `_*` и `meta*`)
        - list → индекс = раздел
        - примитив или невалидный JSON → один раздел «Содержимое»
        """
        context = self._load_context(project_id)
        artifact = self._runtime.load_artifact(context.workspace, artifact_id)
        json_content = self._runtime.load_artifact_content(context.workspace, artifact_id)
        markdown_path = context.workspace / artifact.storage_path.replace(".json", ".md")
        try:
            data = json.loads(json_content) if json_content else None
        except (ValueError, TypeError):
            data = None
        pins = self._failure_pins_for_project(context.workspace, artifact_id)
        pins_by_section: dict[str | None, int] = {}
        for pin in pins:
            pins_by_section[pin.section_id] = pins_by_section.get(pin.section_id, 0) + 1
        sections = self._extract_artifact_sections(data, pins_by_section)
        sections_done = sum(1 for s in sections if s.status == "done")
        return ArtifactSkeletonView(
            project_id=context.manifest.project_id,
            artifact_id=artifact.artifact_id,
            artifact_role=artifact.artifact_role,
            title=artifact.title,
            sections=sections,
            sections_done=sections_done,
            sections_total=len(sections),
            has_markdown=markdown_path.exists(),
            created_at=artifact.created_at,
        )

    def project_artifact_versions(self, project_id: str) -> ProjectArtifactVersionsView:
        """P8: цепочки версий артефактов проекта.

        Группировка по `artifact_role` среди primary-артефактов.
        Внутри группы сортировка по `created_at`; последний = is_current.
        Если есть `parent_artifact_id`, используется для информации, но
        порядок всё равно определяется по `created_at` (стабильно).
        """
        context = self._load_context(project_id)
        artifacts = list(self._runtime.list_artifacts(context.workspace))
        primary = [a for a in artifacts if a.artifact_kind == "primary"]
        by_role: dict[str, list] = {}
        for a in primary:
            by_role.setdefault(a.artifact_role, []).append(a)
        chains: list[tuple[ArtifactVersionItemView, ...]] = []
        for role in sorted(by_role.keys()):
            group = sorted(by_role[role], key=lambda a: a.created_at or "")
            latest_idx = len(group) - 1
            items = tuple(
                ArtifactVersionItemView(
                    artifact_id=a.artifact_id,
                    artifact_role=a.artifact_role,
                    title=a.title,
                    label=self._format_version_label(idx, a.created_at),
                    is_current=(idx == latest_idx),
                    created_at=a.created_at,
                    created_by_task_id=a.created_by_task_id,
                    parent_artifact_id=a.relations.parent_artifact_id,
                    description=a.description,
                )
                for idx, a in enumerate(group)
            )
            chains.append(items)
        return ProjectArtifactVersionsView(
            project_id=context.manifest.project_id,
            chains=tuple(chains),
        )

    def project_failure_pins(
        self, project_id: str, artifact_id: str | None = None
    ) -> ProjectFailurePinsView:
        """P5: маркеры подозрительных мест в артефактах.

        Источники:
        - открытые ClarificationCandidate с низкой confidence_without_user
        - принятые допущения (ClarificationRequest со status=assumed)
        Привязка к артефакту — через related_artifact_ids.
        """
        context = self._load_context(project_id)
        pins = self._failure_pins_for_project(context.workspace, artifact_id)
        return ProjectFailurePinsView(
            project_id=context.manifest.project_id,
            artifact_id=artifact_id,
            pins=tuple(pins),
            total_count=len(pins),
        )

    # --- internal helpers for L6 extensions ----------------------------

    _ARTIFACT_META_PREFIXES: tuple[str, ...] = ("_", "meta")
    _CONFIDENCE_PIN_THRESHOLD: float = 0.85

    def _extract_artifact_sections(
        self,
        data: object,
        pins_by_section: dict[str | None, int],
    ) -> tuple[ArtifactSectionView, ...]:
        if data is None:
            return (
                ArtifactSectionView(
                    section_id="content",
                    title="Содержимое",
                    status="pending",
                    summary=None,
                    has_pins=False,
                    pin_count=0,
                ),
            )
        if isinstance(data, dict) and isinstance(data.get("sections"), list):
            sections: list[ArtifactSectionView] = []
            for idx, item in enumerate(data["sections"]):
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("id") or f"section_{idx + 1}")
                title = str(item.get("title") or self._humanize_key(sid))
                content = item.get("content")
                sections.append(self._build_section_view(sid, title, content, pins_by_section))
            return tuple(sections)
        if isinstance(data, dict):
            sections = []
            for key, value in data.items():
                key_str = str(key)
                if any(key_str.lower().startswith(prefix) for prefix in self._ARTIFACT_META_PREFIXES):
                    continue
                sections.append(
                    self._build_section_view(key_str, self._humanize_key(key_str), value, pins_by_section)
                )
            if not sections:
                sections.append(
                    ArtifactSectionView(
                        section_id="content",
                        title="Содержимое",
                        status="done",
                        summary=None,
                        has_pins=False,
                        pin_count=0,
                    )
                )
            return tuple(sections)
        if isinstance(data, list):
            return tuple(
                self._build_section_view(
                    f"item_{idx + 1}", f"Пункт {idx + 1}", item, pins_by_section
                )
                for idx, item in enumerate(data)
            )
        # primitive
        summary = str(data)[:200] if data is not None else None
        return (
            ArtifactSectionView(
                section_id="content",
                title="Содержимое",
                status="done" if summary else "pending",
                summary=summary,
                has_pins=False,
                pin_count=0,
            ),
        )

    def _build_section_view(
        self,
        section_id: str,
        title: str,
        value: object,
        pins_by_section: dict[str | None, int],
    ) -> ArtifactSectionView:
        is_empty = self._is_section_empty(value)
        pin_count = pins_by_section.get(section_id, 0)
        has_pins = pin_count > 0
        if is_empty:
            status = "pending"
        elif has_pins:
            status = "needs_review"
        else:
            status = "done"
        return ArtifactSectionView(
            section_id=section_id,
            title=title,
            status=status,
            summary=self._section_summary(value),
            has_pins=has_pins,
            pin_count=pin_count,
        )

    @staticmethod
    def _is_section_empty(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return True
            if normalized in {"tbd", "todo", "n/a", "—", "-"}:
                return True
            return False
        if isinstance(value, (list, tuple, dict)) and len(value) == 0:
            return True
        return False

    @staticmethod
    def _section_summary(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text[:200] if text else None
        if isinstance(value, (list, tuple)):
            return f"{len(value)} элементов" if value else None
        if isinstance(value, dict):
            return f"{len(value)} полей" if value else None
        return str(value)[:200]

    @staticmethod
    def _humanize_key(key: str) -> str:
        if not key:
            return key
        normalized = key.replace("_", " ").replace("-", " ").strip()
        if not normalized:
            return key
        return normalized[:1].upper() + normalized[1:]

    @staticmethod
    def _format_version_label(index: int, created_at: str | None) -> str:
        base = f"v{index + 1}"
        if not created_at:
            return base
        date_part = created_at.split("T")[0]
        return f"{base} · {date_part}"

    def _failure_pins_for_project(
        self, workspace: Path, artifact_id_filter: str | None
    ) -> list[FailurePinView]:
        """v3.1: failure-pins строятся из реестра Decisions:
            * ``proposed`` + ``is_low_confidence`` → kind="candidate_open"
              (раньше: open clarification_candidate с низкой уверенностью);
            * ``accepted_default`` / ``locked_in`` с user_action="not_shown"
              и низкой уверенностью → kind="assumption" (раньше: assumed
              ClarificationRequest, т.е. авто-решение).
        """
        pins: list[FailurePinView] = []
        # Без project_id Decisions нельзя выбрать — поднимаем manifest.
        try:
            manifest = self._runtime.load_manifest(workspace)
        except Exception:
            return pins
        decisions = self._runtime.list_decisions(
            workspace, project_id=manifest.project_id
        )
        level_to_priority = {
            "business": "critical",
            "architecture": "high",
            "detail": "medium",
        }
        for decision in decisions:
            related = decision.affected_artifact_ids or ()
            if not related:
                continue
            severity = self._severity_from_priority(
                level_to_priority.get(decision.effective_level, "medium")
            )
            if decision.status == "proposed":
                if not decision.is_low_confidence:
                    continue
                kind = "candidate_open"
            elif (
                decision.status in {"accepted_default", "locked_in"}
                and decision.user_action == "not_shown"
                and decision.is_low_confidence
            ):
                kind = "assumption"
            else:
                continue
            for art_id in related:
                if artifact_id_filter is not None and art_id != artifact_id_filter:
                    continue
                pins.append(
                    FailurePinView(
                        pin_id=decision.decision_id,
                        artifact_id=art_id,
                        section_id=None,
                        severity=severity,
                        kind=kind,
                        message=decision.title,
                        source_type=decision.source,
                        source_id=None,
                        confidence_without_user=decision.confidence,
                        related_clarification_id=(
                            decision.decision_id if kind == "assumption" else None
                        ),
                    )
                )
        return pins

    @staticmethod
    def _severity_from_priority(priority: str | None) -> str:
        mapping = {
            "critical": "high",
            "high": "high",
            "medium": "medium",
            "low": "low",
        }
        return mapping.get(priority or "", "medium")

    def projection_signatures(self, project_id: str, projections: tuple[ProjectionName, ...] | None = None) -> dict[str, str]:
        projection_names = projections or self.DEFAULT_PROJECTIONS
        values: dict[str, object] = {}
        for name in projection_names:
            if name == "shell":
                values[name] = self.project_shell(project_id)
            elif name == "task_graph":
                values[name] = self.project_task_graph(project_id)
            elif name == "situation":
                values[name] = self.project_situation(project_id)
            elif name == "timeline":
                values[name] = self.project_timeline(project_id)
            elif name == "artifacts":
                values[name] = self.project_artifacts(project_id)
            elif name == "attachments":
                values[name] = self.project_attachments(project_id)
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
        token_parts: list[str] = []
        for file_path in (
            workspace_ref.workspace / self._runtime.MANIFEST_FILENAME,
            workspace_ref.workspace / self._runtime.DB_FILENAME,
        ):
            if not file_path.exists():
                continue
            stat = file_path.stat()
            token_parts.append(f"{file_path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        if not token_parts:
            raise ConflictError(f"Не удалось вычислить realtime token для проекта '{project_id}'.")
        return sha256("|".join(token_parts).encode("utf-8")).hexdigest()

    # --- Ролбек: превью инвалидации и история -------------------------------

    def rollback_preview(self, project_id: str, target_task_id: str) -> RollbackPreviewView:
        """Что будет инвалидировано/заархивировано при откате выбранного шага.

        Чистое чтение: вычисляем множество зависимых шагов (целевой +
        транзитивно зависящие) теми же доменными примитивами, что и сам
        откат (DRY), и обогащаем названиями задач и списком артефактов,
        которые уйдут в архив. Состояние проекта не меняется.
        """
        context = self._load_context(project_id)
        workspace = context.workspace
        footprints = collect_step_footprints(self._runtime, workspace, context.snapshot)
        reverted = compute_rollback_set(target_task_id, footprints)

        tasks_by_id = {task.task_id: task for task in self._runtime.list_tasks(workspace)}
        target = tasks_by_id.get(target_task_id)

        # Откат возможен только если у целевого шага есть чекпоинт (pre-state) —
        # база реконструкции. Шаги, выполненные до появления механизма отката,
        # чекпоинта не имеют: показываем превью, но гасим подтверждение.
        checkpoint_task_ids = {
            checkpoint.task_id for checkpoint in self._runtime.list_step_checkpoints(workspace)
        }
        rollbackable = target_task_id in checkpoint_task_ids
        blocked_reason = (
            ""
            if rollbackable
            else (
                "Шаг выполнен до появления механизма отката — точка "
                "восстановления (чекпоинт) недоступна, откатить его нельзя."
            )
        )

        reverted_steps = tuple(
            sorted(
                (
                    RollbackStepView(
                        task_id=task_id,
                        title=task.title if task is not None else task_id,
                        template_ref=task.template_ref if task is not None else "",
                        status=task.status if task is not None else "",
                        is_target=task_id == target_task_id,
                    )
                    for task_id, task in (
                        (tid, tasks_by_id.get(tid)) for tid in reverted
                    )
                ),
                # Целевой шаг первым, затем по названию для стабильности.
                key=lambda step: (not step.is_target, step.title, step.task_id),
            )
        )

        archived_artifacts = tuple(
            RollbackArtifactView(
                artifact_id=artifact.artifact_id,
                artifact_role=artifact.artifact_role,
                title=artifact.title,
                created_by_task_id=artifact.created_by_task_id,
            )
            for artifact in self._runtime.list_artifacts(workspace)
            if artifact.created_by_task_id in reverted
        )

        return RollbackPreviewView(
            project_id=context.manifest.project_id,
            target_task_id=target_task_id,
            target_title=target.title if target is not None else target_task_id,
            reverted_steps=reverted_steps,
            archived_artifacts=archived_artifacts,
            rollbackable=rollbackable,
            blocked_reason=blocked_reason,
        )

    def rollback_history(self, project_id: str) -> ProjectRollbackHistoryView:
        """История выполненных откатов проекта (свежие сверху)."""
        context = self._load_context(project_id)
        tasks_by_id = {
            task.task_id: task for task in self._runtime.list_tasks(context.workspace)
        }
        items = tuple(
            RollbackHistoryItemView(
                rollback_id=record.rollback_id,
                target_task_id=record.target_task_id,
                target_title=(
                    tasks_by_id[record.target_task_id].title
                    if record.target_task_id in tasks_by_id
                    else record.target_task_id
                ),
                reverted_count=len(record.reverted_task_ids),
                archived_artifact_count=len(record.archived_artifact_ids),
                actor=record.actor,
                reason=record.reason,
                created_at=record.created_at,
            )
            for record in self._runtime.list_rollbacks(context.workspace)
        )
        return ProjectRollbackHistoryView(
            project_id=context.manifest.project_id, items=items
        )

    def _load_context(self, project_id: str) -> ProjectContext:
        return self._load_context_by_ref(self._catalog.resolve_workspace(project_id))

    def _validated_snapshot(self) -> RegistrySnapshot:
        """Получить валидный снапшот реестра или упасть с понятной ошибкой.

        Валидация мемоизирована в ``RegistryService`` по версии реестра,
        поэтому повторные вызовы в пределах одного запроса дешёвы.
        """
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно построить UI-проекции.")
        return snapshot

    def _load_context_by_ref(
        self, workspace_ref: WorkspaceRef, *, snapshot: RegistrySnapshot | None = None
    ) -> ProjectContext:
        """Собрать контекст проекта.

        ``snapshot`` можно передать, чтобы переиспользовать уже
        провалидированный реестр (агрегирующие пути вроде ``list_projects``
        валидируют его один раз на всю выборку). Если не передан —
        валидируем (дёшево за счёт мемоизации).
        """
        workspace = workspace_ref.workspace
        if snapshot is None:
            # Per-project путь без явного снимка: берём закреплённый граф
            # проекта (если резолвер внедрён), иначе — живой реестр.
            snapshot = (
                self._registry_resolver.snapshot_for(workspace)
                if self._registry_resolver is not None
                else self._validated_snapshot()
            )
        return ProjectContext(
            workspace_ref=workspace_ref,
            workspace=workspace,
            manifest=workspace_ref.manifest,
            state=self._runtime.load_project_state(workspace),
            snapshot=snapshot,
        )

    def _build_task_tree(self, workspace: Path, tasks: list[TaskRecord], current_task_id: str | None, snapshot: RegistrySnapshot | None = None, *, available: bool = True) -> tuple[TaskNodeView, ...]:
        children_by_parent: dict[str | None, list[TaskRecord]] = {}
        for task in tasks:
            children_by_parent.setdefault(task.parent_task_id, []).append(task)
        children_count_by_parent: dict[str, int] = {}
        completed_count_by_parent: dict[str, int] = {}
        for task in tasks:
            if task.parent_task_id:
                children_count_by_parent[task.parent_task_id] = (
                    children_count_by_parent.get(task.parent_task_id, 0) + 1
                )
                if task.status == "completed":
                    completed_count_by_parent[task.parent_task_id] = (
                        completed_count_by_parent.get(task.parent_task_id, 0) + 1
                    )
        # v3.1: счётчик «блокирующих уточнений» = число open Decisions с
        # source_task_id == task_id.
        try:
            manifest = self._runtime.load_manifest(workspace)
            open_decisions = self._runtime.list_decisions(
                workspace, project_id=manifest.project_id, status="proposed"
            )
        except Exception:
            open_decisions = []
        clarification_counts: dict[str, int] = {}
        for decision in open_decisions:
            if decision.source_task_id:
                clarification_counts[decision.source_task_id] = (
                    clarification_counts.get(decision.source_task_id, 0) + 1
                )

        def build(task: TaskRecord) -> TaskNodeView:
            fan_out_meta = None
            if task.template_type == "fan_out" and snapshot is not None:
                try:
                    tmpl = snapshot.resolve_template(task.template_ref)
                    if tmpl.fan_out_spec is not None:
                        artifact = self._runtime.latest_artifact_by_role(workspace, tmpl.fan_out_spec.artifact_role)
                        producer_task_id = artifact.created_by_task_id if artifact is not None else None
                        fan_out_meta = FanOutMeta(
                            source_artifact_role=tmpl.fan_out_spec.artifact_role,
                            total_instances=children_count_by_parent.get(task.task_id, 0),
                            completed_instances=completed_count_by_parent.get(task.task_id, 0),
                            producer_task_id=producer_task_id,
                        )
                except Exception:
                    pass
            return TaskNodeView(
                task_id=task.task_id,
                task_key=task.task_key,
                parent_task_id=task.parent_task_id,
                title=task.title,
                template_ref=task.template_ref,
                template_type=task.template_type,
                status=task.status,
                status_summary=task.error_message or self._status_summary(task),
                origin_kind=task.origin_kind,
                origin_ref=task.origin_ref,
                slot_id=task.slot_id,
                depth=task.depth,
                retryable=task.status == "failed",
                is_current=task.task_id == current_task_id,
                blocking_clarification_count=clarification_counts.get(task.task_id, 0),
                updated_at=task.updated_at,
                children=tuple(build(child) for child in sorted(children_by_parent.get(task.task_id, []), key=lambda item: item.created_at)),
                fan_out_meta=fan_out_meta,
                available=available,
            )

        return tuple(build(task) for task in sorted(children_by_parent.get(None, []), key=lambda item: item.created_at))

    def _build_situation(
        self, context: ProjectContext, *, tasks: list[TaskRecord] | None = None
    ) -> ProjectSituationView:
        if tasks is None:
            self._planning_service.plan(
                context.workspace, context.snapshot, mode="dry-run", record=False
            )
            tasks = self._runtime.list_tasks(context.workspace)
        failed = [task for task in tasks if task.status == "failed"]
        ready = [task for task in tasks if task.status == "ready"]
        blockers: list[SituationBlockerView] = []
        for task in failed[:3]:
            blockers.append(
                SituationBlockerView(
                    kind="task_failure",
                    title=f"Ошибка задачи «{task.title}»",
                    summary=task.error_message or "Задача завершилась ошибкой.",
                    severity="error",
                    detail_view="debug",
                    related_id=task.task_id,
                )
            )
        for gap in context.state.process.active_gaps.values():
            if not gap.blocking:
                continue
            blockers.append(
                SituationBlockerView(
                    kind="gap",
                    title=gap.title,
                    summary=gap.description,
                    severity=gap.severity,
                    detail_view="state",
                    related_id=gap.identifier,
                )
            )
        # v3.1: «открытые блокирующие уточнения» = proposed-Decisions,
        # привязанные к задаче (source_task_id != None). Decision не имеет
        # понятия blocking_scope, поэтому используем наличие task-привязки
        # как proxy «эта запись блокирует конкретную задачу».
        level_to_severity = {
            "business": "critical",
            "architecture": "high",
            "detail": "medium",
        }
        try:
            open_decisions = self._runtime.list_decisions(
                context.workspace,
                project_id=context.manifest.project_id,
                status="proposed",
            )
        except Exception:
            open_decisions = []
        blocking_decisions = [d for d in open_decisions if d.source_task_id]
        for decision in blocking_decisions[:3]:
            blockers.append(
                SituationBlockerView(
                    kind="clarification",
                    title=decision.title,
                    summary=decision.description_without_category or decision.title,
                    severity=level_to_severity.get(decision.effective_level, "medium"),
                    detail_view="clarification",
                    related_id=decision.decision_id,
                )
            )
        if failed or blockers:
            clarification_first = next((blocker for blocker in blockers if blocker.kind == "clarification"), None)
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Требуется внимание",
                headline="Нужен ответ пользователя" if clarification_first else "Проект остановлен на блокировке",
                summary=blockers[0].summary if blockers else "Есть задача в ошибке.",
                blocking=True,
                primary_action=ActionDescriptor(
                    kind="open_clarification" if clarification_first else "open_debug",
                    label="Ответить на вопрос" if clarification_first else "Открыть детали",
                    description="Открыть уточнение и применить ответ." if clarification_first else "Посмотреть ошибку и решить, нужен ли повтор.",
                    target_view="clarification" if clarification_first else "debug",
                    target_id=clarification_first.related_id if clarification_first else None,
                    blocking=True,
                ),
                blockers=tuple(blockers),
            )
        if ready:
            task = max(ready, key=lambda item: context.snapshot.resolve_template(item.template_ref).planning.priority)
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Готов к продолжению",
                headline=f"Следующая задача: {task.title}",
                summary="Система нашла допустимую листовую задачу и может запустить ее сейчас.",
                blocking=False,
                primary_action=ActionDescriptor(
                    kind="run_next",
                    label="Запустить задачу",
                    description="Выполнить следующую допустимую задачу.",
                    command_name="run-next",
                ),
                secondary_actions=(
                    ActionDescriptor(
                        kind="open_task_graph",
                        label="Открыть граф задач",
                        description="Посмотреть структуру проекта и причины статусов.",
                        target_view="task_graph",
                    ),
                ),
            )
        if self._objective_done(context):
            return ProjectSituationView(
                project_id=context.manifest.project_id,
                status_label="Готово",
                headline="Цель проекта завершена",
                summary="Обязательные итоговые артефакты получены.",
                blocking=False,
                primary_action=ActionDescriptor(
                    kind="open_artifact",
                    label="Открыть артефакты",
                    description="Посмотреть результат работы.",
                    target_view="artifact",
                ),
            )
        return ProjectSituationView(
            project_id=context.manifest.project_id,
            status_label="Ожидание",
            headline="Нет доступной задачи",
            summary="Граф задач раскрыт, но сейчас нет задач, которые можно запустить автоматически.",
            blocking=True,
            primary_action=ActionDescriptor(
                kind="open_task_graph",
                label="Открыть граф задач",
                description="Посмотреть причины блокировки.",
                target_view="task_graph",
                blocking=True,
            ),
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
                    summary="Инициализирован новый проект и стартовое состояние.",
                    status="info",
                    created_at=manifest.created_at,
                    detail_view="state",
                    entity_type="project",
                    entity_id=manifest.project_id,
                ),
            )
        )
        for event in self._runtime.list_state_events(context.workspace):
            # B5: goal теперь — положение Layer A с фиксированным id; ловим
            # UpsertPositionPatch именно для GOAL_POSITION_ID.
            if event.layer == "knowledge" and event.patch_type == "UpsertPositionPatch":
                position_payload = event.payload.get("position") or {}
                if isinstance(position_payload, dict) and position_payload.get("identifier") == "project.goal":
                    entries.append(
                        (
                            (event.created_at, 1),
                            TimelineEntryView(
                                sequence=0,
                                kind="goal_updated",
                                title="Цель проекта уточнена",
                                summary=str(position_payload.get("statement", "Цель обновлена.")),
                                status="success",
                                created_at=event.created_at,
                                detail_view="state",
                                entity_type="project_knowledge",
                                entity_id=context.manifest.project_id,
                            ),
                        )
                    )
                continue
            if event.patch_type == "ActivateDomainPackPatch":
                pack_ref = str(event.payload.get("pack_ref", ""))
                entries.append(
                    (
                        (event.created_at, 2),
                        TimelineEntryView(
                            sequence=0,
                            kind="domain_pack_enabled",
                            title="Подключён доменный пакет",
                            summary=f"Активирован доменный пакет {pack_ref}.",
                            status="info",
                            created_at=event.created_at,
                            detail_view="state",
                            entity_type="domain_pack",
                            entity_id=pack_ref,
                        ),
                    )
                )
            elif event.patch_type == "SetClarificationModePatch":
                entries.append(
                    (
                        (event.created_at, 3),
                        TimelineEntryView(
                            sequence=0,
                            kind="clarification_mode_changed",
                            title="Изменен режим уточнений",
                            summary=f"Новый режим участия пользователя: {event.payload.get('mode', 'balanced')}.",
                            status="info",
                            created_at=event.created_at,
                            detail_view="clarification",
                            entity_type="clarification_mode",
                            entity_id=context.manifest.project_id,
                        ),
                    )
                )
        # v3.1: события «уточнений» в timeline теперь приходят из реестра
        # Decisions. Маппинг статус → визуальный label:
        #   proposed → «Система запросила уточнение» (warning, если есть source_task_id)
        #   accepted_default → «Принят дефолт» (info)
        #   user_overridden → «Пользователь ответил на уточнение» (success)
        #   locked_in → «Решение зафиксировано» (success)
        try:
            timeline_decisions = self._runtime.list_decisions(
                context.workspace, project_id=context.manifest.project_id
            )
        except Exception:
            timeline_decisions = []
        for decision in timeline_decisions:
            if decision.status == "proposed":
                title = "Система запросила уточнение"
                status = "warning" if decision.source_task_id else "info"
            elif decision.status == "accepted_default":
                title = "Принят дефолт по умолчанию"
                status = "info"
            elif decision.status == "user_overridden":
                title = "Пользователь ответил на уточнение"
                status = "success"
            elif decision.status == "locked_in":
                title = "Решение зафиксировано"
                status = "success"
            elif decision.status == "deferred":
                title = "Решение отложено"
                status = "info"
            else:
                title = "Решение обновлено"
                status = "info"
            entries.append(
                (
                    (decision.updated_at or decision.created_at, 4),
                    TimelineEntryView(
                        sequence=0,
                        kind=f"clarification_{decision.status}",
                        title=title,
                        summary=decision.description_without_category or decision.title,
                        status=status,
                        created_at=decision.updated_at or decision.created_at,
                        detail_view="clarification",
                        entity_type="clarification",
                        entity_id=decision.decision_id,
                    ),
                )
            )
        for task in self._runtime.list_tasks(context.workspace):
            if task.status == "failed":
                entries.append(
                    (
                        (task.updated_at, 8),
                        TimelineEntryView(
                            sequence=0,
                            kind="task_failed",
                            title=f"Задача завершилась ошибкой: {task.title}",
                            summary=task.error_message or "Задача завершилась ошибкой.",
                            status="error",
                            created_at=task.updated_at,
                            detail_view="debug",
                            entity_type="task",
                            entity_id=task.task_id,
                        ),
                    )
                )
            elif task.status == "completed" and task.template_type == "leaf":
                entries.append(
                    (
                        (task.updated_at, 9),
                        TimelineEntryView(
                            sequence=0,
                            kind="task_completed",
                            title=f"Задача завершена: {task.title}",
                            summary="Результат задачи принят и сохранен в проекте.",
                            status="success",
                            created_at=task.updated_at,
                            detail_view="task_graph",
                            entity_type="task",
                            entity_id=task.task_id,
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
        ordered = [item[1] for item in sorted(entries, key=lambda item: item[0])]
        return tuple(
            TimelineEntryView(
                sequence=index,
                kind=item.kind,
                title=item.title,
                summary=item.summary,
                status=item.status,
                created_at=item.created_at,
                detail_view=item.detail_view,
                entity_type=item.entity_type,
                entity_id=item.entity_id,
            )
            for index, item in enumerate(ordered, start=1)
        )

    def _timeline_entry_for_artifact(self, workspace: Path, artifact) -> tuple[str, str, str, str]:
        role_map = {
            "request_fact_sheet": ("Выделены факты запроса", "Система извлекла явные факты исходного запроса.", "success", "artifact"),
            "goal_hypothesis": ("Сформирована гипотеза цели", "Система зафиксировала рабочую цель проекта.", "success", "artifact"),
            "constraint_inventory": ("Собраны ограничения", "Система выделила ограничения проекта.", "success", "artifact"),
            "ambiguity_gap_report": ("Разобраны неоднозначности", "Система нашла пробелы и рабочие допущения.", "success", "artifact"),
            "normalized_request": ("Запрос нормализован", "Система собрала опорную модель запроса.", "success", "artifact"),
            "business_outcome_model": ("Определен бизнес-результат", "Система описала ожидаемую ценность проекта.", "success", "artifact"),
            "scope_boundary_matrix": ("Определены границы этапа", "Система зафиксировала scope in/out.", "success", "artifact"),
            "stakeholder_map": ("Выделены стейкхолдеры", "Система собрала роли и ожидания участников.", "success", "artifact"),
            "solution_option_inventory": ("Сформирован набор вариантов", "Система выделила варианты решения.", "success", "artifact"),
            "predictive_problem_definition": ("Определена ML-задача", "Добавлена доменная постановка предиктивной аналитики.", "success", "artifact"),
            "data_landscape_assessment": ("Оценены данные", "Добавлена доменная оценка данных для аналитики.", "success", "artifact"),
            "security_compliance_constraints": ("Описаны ограничения ИБ", "Добавлены требования безопасности и приватности.", "success", "artifact"),
            "integration_operating_model": ("Описана интеграционная модель", "Добавлены требования к интеграциям и обновлению данных.", "success", "artifact"),
            "ui_requirements_outline": ("Разобраны пользовательские потоки", "Добавлены требования к интерфейсу.", "success", "artifact"),
            "requirements_spec": ("Подготовлен черновик ТЗ", "Сформирован структурированный вариант требований.", "success", "artifact"),
        }
        if artifact.artifact_role == "review_report":
            payload = json.loads(self._runtime.load_artifact_content(workspace, artifact.artifact_id))
            status = payload.get("overall_status", "unknown")
            return (
                "Ревью ТЗ пройдено" if status == "passed" else "Ревью выявило замечания",
                payload.get("summary", "Ревью завершено."),
                "success" if status == "passed" else "warning",
                "review",
            )
        return role_map.get(artifact.artifact_role, ("Создан артефакт", f"Получен артефакт {artifact.artifact_role}.", "info", "artifact"))

    def _artifact_validations(self, workspace: Path, artifact_id: str) -> tuple[ArtifactValidationView, ...]:
        items: list[ArtifactValidationView] = []
        for run in self._runtime.list_validation_runs(workspace):
            messages = [finding.message for finding in run.findings if artifact_id in finding.related_artifact_ids]
            if messages:
                items.append(
                    ArtifactValidationView(
                        validation_run_id=run.validation_run_id,
                        status=run.status,
                        finding_messages=tuple(messages),
                        created_at=run.created_at,
                    )
                )
        return tuple(items)

    def _status_summary(self, task: TaskRecord) -> str | None:
        if task.status == "completed":
            return "Задача завершена."
        if task.status == "ready":
            return "Задача готова к запуску."
        if task.status == "blocked":
            return "Задача пока заблокирована."
        if task.status == "in_progress":
            return "Задача выполняется."
        if task.status == "waiting_for_children":
            return "Композитная задача ожидает дочерние задачи."
        return None

    def _find_current_title(self, nodes: tuple[TaskNodeView, ...], current_task_id: str | None) -> str | None:
        for node in nodes:
            if node.task_id == current_task_id:
                return node.title
            nested = self._find_current_title(node.children, current_task_id)
            if nested:
                return nested
        return None

    def _objective_key_artifact_id(self, context: ProjectContext, objective) -> str | None:
        """Ключевой дилеверабл этапа — id артефакта финальной done-роли цели.

        Done-артефакты цели объявлены как refs; роль артефакта = последний
        сегмент identifier (та же логика, что в ``_objective_progress``). Берём
        primary-артефакт самой «поздней» по порядку done-роли (финальный
        результат этапа), при равенстве — свежий по времени. UI открывает его
        по клику на завершённый этап в степпере.
        """
        order = {
            ref.identifier.rsplit(".", 1)[-1]: idx
            for idx, ref in enumerate(objective.done_artifact_refs)
        }
        if not order:
            return None
        candidates = [
            artifact
            for artifact in self._runtime.list_artifacts(context.workspace)
            if artifact.artifact_role in order and artifact.artifact_kind == "primary"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda a: (order[a.artifact_role], a.created_at))
        return candidates[-1].artifact_id

    def _human_approval_gate_signed_off(self, context: ProjectContext, objective) -> bool:
        """human_approval-гейты цели пройдены, если её итоговый артефакт
        согласован с заказчиком (ArtifactRecord.signed_off). Заменяет прежнюю
        проверку по решению-согласованию в реестре (Ф3). Если у цели нет
        human_approval-гейтов — считается пройденным.
        """
        has_human_gate = False
        for gate_ref in getattr(objective, "done_gate_refs", ()) or ():
            try:
                gate = context.snapshot.resolve_quality_gate(gate_ref)
            except Exception:
                continue
            if gate.check_type == "human_approval":
                has_human_gate = True
                break
        if not has_human_gate:
            return True
        key_id = self._objective_key_artifact_id(context, objective)
        if key_id is None:
            return False
        try:
            artifact = self._runtime.load_artifact(context.workspace, key_id)
        except Exception:
            return False
        return bool(artifact.signed_off)

    def _objective_done(self, context: ProjectContext) -> bool:
        objective = context.snapshot.resolve_objective(context.manifest.objective_ref)
        artifacts = {artifact.artifact_role for artifact in self._runtime.list_artifacts(context.workspace)}
        artifacts_ready = all(
            ref.identifier.rsplit(".", 1)[-1] in artifacts for ref in objective.done_artifact_refs
        )
        if not artifacts_ready:
            return False
        # Ф3: цель завершена только если итоговый артефакт согласован.
        return self._human_approval_gate_signed_off(context, objective)

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

    def task_methodology_trace(self, project_id: str, task_id: str) -> dict:
        """Reasoning + methodology trace задачи.

        Этап 1.1: reasoning и methodology_trace больше не отдельные
        артефакты, они живут в ``ArtifactMetadata`` primary артефакта,
        созданного задачей. Возвращаем их оттуда.
        """
        context = self._load_context(project_id)
        artifacts = list(self._runtime.list_artifacts(context.workspace))
        primary_artifact = None
        for artifact in artifacts:
            if (
                artifact.created_by_task_id == task_id
                and artifact.artifact_kind == "primary"
                and not artifact.is_superseded
            ):
                primary_artifact = artifact
                break
        if primary_artifact is None:
            return {
                "task_id": task_id,
                "trace": None,
                "reasoning": None,
                "message": "Для задачи не найден primary артефакт с методологическим reasoning.",
            }
        trace_payload = dict(primary_artifact.metadata.methodology_trace) or None
        reasoning_payload = dict(primary_artifact.metadata.reasoning) or None
        # Найдём execution_run, в котором эта задача была исполнена. На один
        # taskId может прийтись несколько runs (ретраи) — берём последний
        # (list_execution_runs сортирует по created_at ASC).
        execution_summary: dict | None = None
        for run in self._runtime.list_execution_runs(context.workspace):
            if run.get("task_id") == task_id:
                execution_summary = {
                    "execution_run_id": run.get("execution_run_id"),
                    "provider": run.get("provider"),
                    "model": run.get("model"),
                    "status": run.get("status"),
                    "context_manifest_id": run.get("context_manifest_id"),
                    "created_at": run.get("created_at"),
                }
        return {
            "task_id": task_id,
            "trace": trace_payload,
            "reasoning": reasoning_payload,
            "primary_artifact_id": primary_artifact.artifact_id,
            "execution": execution_summary,
        }


def _extract_requisites(payload: dict) -> tuple[RequisiteItemView, ...]:
    """Достаёт требуемые входные данные из артефакта реализуемости.

    Берём только предусловия (``prerequisites``) по каждой возможности — это и
    есть «дайте то-то». Блокеры (причины невыполнимости) сюда НЕ попадают: они
    относятся к статусу реализуемости, а не к запросу данных. Разбор защитный
    (отсутствующие/кривые поля — пропускаем) и с дедупликацией, чтобы один и тот
    же реквизит из разных пунктов не повторялся.

    Намеренно изолировано на уровне модуля: артефакт реализуемости сейчас
    неструктурированный, и когда он станет структурным, переписывается только
    эта функция, а не проекция/эндпоинт/UI.
    """
    capabilities = payload.get("capabilities")
    rows = capabilities if isinstance(capabilities, list) else []
    seen: set[str] = set()
    items: list[RequisiteItemView] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        needed_for = str(row.get("name") or "").strip() or "проект"
        prerequisites = row.get("prerequisites")
        if not isinstance(prerequisites, list):
            continue
        for value in prerequisites:
            title = str(value).strip()
            if not title:
                continue
            dedup_key = title.casefold()
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            items.append(
                RequisiteItemView(
                    title=title,
                    needed_for=needed_for,
                    status="requested",
                    key=title,  # реализуемость: ключ = текст (как и прежде)
                    kind="other",
                    blocking=False,
                    stage="realizability",
                )
            )
    return tuple(items)


def _extract_component_model_requisites(payload: dict) -> tuple[RequisiteItemView, ...]:
    """Реквизиты из модели компонентов (архитектура, Ф5).

    Источник — поле ``requisites`` у каждого компонента. Ключ провижена —
    устойчивый составной (``architecture:<component>:<requisite>``), чтобы
    отметка «предоставлено» не слетала при переформулировке заголовка ИИ.
    Защитный разбор: кривые элементы пропускаются.
    """
    components = payload.get("components")
    rows = components if isinstance(components, list) else []
    items: list[RequisiteItemView] = []
    for comp in rows:
        if not isinstance(comp, dict):
            continue
        needed_for = str(comp.get("name") or comp.get("id") or "").strip() or "компонент"
        cid = str(comp.get("id") or "")
        requisites = comp.get("requisites")
        if not isinstance(requisites, list):
            continue
        for req in requisites:
            if not isinstance(req, dict):
                continue
            title = str(req.get("title") or "").strip()
            if not title:
                continue
            rid = str(req.get("id") or title)
            items.append(
                RequisiteItemView(
                    title=title,
                    needed_for=needed_for,
                    status="requested",
                    key=f"architecture:{cid}:{rid}",
                    kind=str(req.get("kind") or "other"),
                    blocking=bool(req.get("blocking")),
                    stage="architecture",
                )
            )
    return tuple(items)


def gather_requisites(
    runtime: SqliteRuntime, workspace: Path
) -> tuple[tuple[RequisiteItemView, ...], str | None, str | None]:
    """Собрать реквизиты из всех источников + наложить факт предоставления (Ф5).

    Источники: предусловия артефакта реализуемости и поле ``requisites`` модели
    компонентов. Дедуп по нормализованному заголовку (блокирующий флаг —
    логическое ИЛИ). Возвращает (items, source_artifact_id, updated_at).
    """
    raw: list[RequisiteItemView] = []
    source_id: str | None = None
    updated: str | None = None

    for role, extractor in (
        ("feasibility_assessment", _extract_requisites),
        ("component_model", _extract_component_model_requisites),
    ):
        artifact = runtime.latest_artifact_by_role(workspace, role)
        if artifact is None:
            continue
        try:
            payload = json.loads(runtime.load_artifact_content(workspace, artifact.artifact_id))
        except (json.JSONDecodeError, OSError):
            continue
        raw.extend(extractor(payload))
        if source_id is None:
            source_id = artifact.artifact_id
            updated = artifact.created_at

    # Дедуп по нормализованному заголовку; блокирующий флаг агрегируем по ИЛИ.
    deduped: list[RequisiteItemView] = []
    index_by_norm: dict[str, int] = {}
    for item in raw:
        norm = item.title.strip().casefold()
        if norm in index_by_norm:
            idx = index_by_norm[norm]
            if item.blocking and not deduped[idx].blocking:
                deduped[idx] = replace(deduped[idx], blocking=True)
            continue
        index_by_norm[norm] = len(deduped)
        deduped.append(item)

    provided = runtime.list_requisite_provisions(workspace)
    items = tuple(
        replace(item, status="provided")
        if ((item.key or item.title) in provided or item.title in provided)
        else item
        for item in deduped
    )
    return items, source_id, updated


def blocking_requisites_unprovided(runtime: SqliteRuntime, workspace: Path) -> tuple[str, ...]:
    """Заголовки непредоставленных блокирующих реквизитов (для шлюза перехода)."""
    items, _, _ = gather_requisites(runtime, workspace)
    return tuple(item.title for item in items if item.blocking and item.status != "provided")


def _extract_gaps(payload: dict) -> tuple[CapabilityGapView, ...]:
    """Достаёт зоны роста из артефакта реализуемости.

    Пробел = пункт, который не закрыт ни одним умением (пустой ``covered_by``).
    Это и есть «пока не умеем, но возможно добавим». Разбор защитный и с
    дедупликацией; изолирован тут — при переходе на структурный артефакт
    меняется только эта функция.
    """
    capabilities = payload.get("capabilities")
    rows = capabilities if isinstance(capabilities, list) else []
    seen: set[str] = set()
    items: list[CapabilityGapView] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("covered_by") or "").strip():
            continue  # закрыто умением — не пробел
        title = str(row.get("name") or "").strip()
        if not title:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        reason = str(row.get("rationale") or "").strip()
        if not reason:
            blockers = row.get("blockers")
            if isinstance(blockers, list) and blockers:
                reason = str(blockers[0]).strip()
        suggestion = str(row.get("suggestion") or "").strip()
        items.append(CapabilityGapView(title=title, reason=reason, suggestion=suggestion))
    return tuple(items)
