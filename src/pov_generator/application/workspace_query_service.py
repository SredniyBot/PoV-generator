from __future__ import annotations

import json
from dataclasses import dataclass
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
    ClarificationItemView,
    ClarificationOptionView,
    ContextManifestSummaryView,
    DecisionLogEntryView,
    DomainPackCatalogItemView,
    FailurePinView,
    ObjectiveCatalogItemView,
    ObjectiveProgressView,
    OverviewArtifactItem,
    OverviewClarificationItem,
    ProjectArtifactVersionsView,
    ProjectClarificationsView,
    ProjectDebugView,
    ProjectDecisionLogView,
    ProjectFailurePinsView,
    ProjectListItemView,
    ProjectOverviewView,
    ProjectReviewView,
    ProjectShellView,
    ProjectSituationView,
    ProjectStateView,
    ProjectTaskGraphView,
    ProjectTimelineView,
    ReviewIssueView,
    SituationBlockerView,
    TaskNodeView,
    TimelineEntryView,
)
from ..infrastructure.sqlite_runtime import SqliteRuntime
from .planning_service import PlanningService
from .registry_service import RegistryService
from .workspace_catalog import WorkspaceCatalog, WorkspaceRef

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
        "clarifications",
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
            task_graph = self.project_task_graph(context.workspace_ref.project_id)
            current_title = self._find_current_title(task_graph.nodes, task_graph.current_task_id)
            items.append(
                ProjectListItemView(
                    project_id=context.manifest.project_id,
                    name=context.manifest.name,
                    status_label=situation.status_label,
                    updated_at=context.state.process.updated_at,
                    has_blockers=situation.blocking,
                    current_step_title=current_title,
                )
            )
        return tuple(sorted(items, key=lambda item: (item.updated_at, item.project_id), reverse=True))

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
        self._planning_service.plan(context.workspace, context.snapshot, mode="dry-run", record=False)
        tasks = self._runtime.list_tasks(context.workspace)
        leaf_tasks = [task for task in tasks if task.template_type == "leaf"]
        ready = next((task for task in leaf_tasks if task.status == "ready"), None)
        nodes = self._build_task_tree(context.workspace, tasks, ready.task_id if ready else None)
        return ProjectTaskGraphView(
            project_id=context.manifest.project_id,
            objective_ref=context.manifest.objective_ref,
            current_task_id=ready.task_id if ready else None,
            completed_leaf_tasks=sum(1 for task in leaf_tasks if task.status == "completed"),
            total_leaf_tasks=len(leaf_tasks),
            nodes=nodes,
        )

    def project_situation(self, project_id: str) -> ProjectSituationView:
        context = self._load_context(project_id)
        return self._build_situation(context)

    def project_timeline(self, project_id: str, *, after_sequence: int = 0) -> ProjectTimelineView:
        context = self._load_context(project_id)
        entries = self._build_timeline(context)
        filtered = tuple(entry for entry in entries if entry.sequence > after_sequence)
        return ProjectTimelineView(project_id=context.manifest.project_id, entries=filtered, total_entries=len(entries))

    def project_clarifications(self, project_id: str) -> ProjectClarificationsView:
        context = self._load_context(project_id)
        items = tuple(
            self._clarification_view(item)
            for item in self._runtime.list_clarification_requests(context.workspace)
        )
        return ProjectClarificationsView(
            project_id=context.manifest.project_id,
            mode=context.state.process.clarification_mode,
            open_count=sum(1 for item in items if item.status == "open"),
            answered_count=sum(1 for item in items if item.status == "answered"),
            assumed_count=sum(1 for item in items if item.status == "assumed"),
            blocking_count=sum(1 for item in items if item.status == "open" and item.blocking_scope != "none"),
            items=items,
        )

    def clarification_detail(self, project_id: str, clarification_id: str) -> ClarificationItemView:
        context = self._load_context(project_id)
        return self._clarification_view(self._runtime.get_clarification_request(context.workspace, clarification_id))

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
            )
            for artifact in self._runtime.list_artifacts(context.workspace)
        )

    def artifact_detail(self, project_id: str, artifact_id: str) -> ArtifactDetailView:
        context = self._load_context(project_id)
        artifact = self._runtime.load_artifact(context.workspace, artifact_id)
        markdown_path = context.workspace / artifact.storage_path.replace(".json", ".md")
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


    def project_overview(self, project_id: str) -> ProjectOverviewView:
        context = self._load_context(project_id)
        state = context.state
        manifest = context.manifest
        snapshot = context.snapshot

        # Прогресс по done_when
        objective = snapshot.resolve_objective(manifest.objective_ref)
        artifact_roles_present = {a.artifact_role for a in self._runtime.list_artifacts(context.workspace)}
        artifacts_required = len(objective.done_artifact_refs)
        artifacts_ready = sum(
            1 for ref in objective.done_artifact_refs
            if ref.identifier.rsplit(".", 1)[-1] in artifact_roles_present
        )
        requests = list(self._runtime.list_clarification_requests(context.workspace))
        gates_required = len(objective.done_gate_refs)
        gates_passed = 0
        # Артефакты по ролям — нужны для проверки прохождения automated-gate'ов.
        artifacts_by_role: dict[str, list] = {}
        for art in self._runtime.list_artifacts(context.workspace):
            artifacts_by_role.setdefault(art.artifact_role, []).append(art)

        for gate_ref in objective.done_gate_refs:
            try:
                gate = snapshot.resolve_quality_gate(gate_ref)
            except Exception:
                continue

            if gate.check_type == "human_approval":
                # Засчитывается только если есть approved-clarification на этот gate.
                if any(
                    r.source_type == "quality_gate"
                    and r.source_id == gate.ref.as_string()
                    and r.status == "answered"
                    and "approved" in r.selected_option_ids
                    for r in requests
                ):
                    gates_passed += 1
                continue

            # Не-human gate'ы — automated_review и потенциальные другие.
            # Раньше засчитывались UNCONDITIONALLY, из-за чего проект стартовал
            # с «1/2 проверок» ещё до того как что-то реально выполнилось.
            # Теперь проверяем фактическое прохождение:
            #   1) все required-артефакты gate'а должны существовать в проекте;
            #   2) если артефакт — review_report, его overall_status должен быть
            #      "passed" / "passed_with_remarks" (не "failed").
            required_roles = list(gate.required_artifact_roles) if hasattr(gate, "required_artifact_roles") else []
            all_present = all(role in artifacts_by_role for role in required_roles) if required_roles else False
            if not all_present:
                continue

            # Дополнительная семантическая проверка для gate'ов, привязанных к review_report.
            review_ok = True
            if "review_report" in required_roles:
                review_records = artifacts_by_role.get("review_report") or []
                if not review_records:
                    review_ok = False
                else:
                    latest_review = max(review_records, key=lambda a: a.created_at)
                    try:
                        import json as _json
                        payload = _json.loads(
                            self._runtime.load_artifact_content(
                                context.workspace, latest_review.artifact_id,
                            )
                        )
                        status = str(payload.get("overall_status", "")).lower()
                        review_ok = status in {"passed", "passed_with_remarks"}
                    except Exception:
                        review_ok = False
            if review_ok:
                gates_passed += 1

        # Критичные открытые уточнения
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        critical = [
            r for r in requests
            if r.status == "open" and r.priority in {"high", "critical"}
        ]
        critical.sort(key=lambda r: priority_order.get(r.priority, 9))
        critical_items = tuple(
            OverviewClarificationItem(
                clarification_id=r.request_id,
                title=r.title,
                priority=r.priority,
                blocking_scope=r.blocking_scope,
                source_type=r.source_type,
            )
            for r in critical[:5]
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
            objective_progress=ObjectiveProgressView(
                artifacts_required=artifacts_required,
                artifacts_ready=artifacts_ready,
                gates_required=gates_required,
                gates_passed=gates_passed,
            ),
            critical_clarifications=critical_items,
            key_artifacts=key_artifacts,
            active_methodology=active_methodology,
            active_domain_packs=tuple(sorted(state.process.active_domain_pack_records.keys())),
            clarification_mode=state.process.clarification_mode,
            updated_at=state.process.updated_at,
        )

    def project_state(self, project_id: str) -> ProjectStateView:
        context = self._load_context(project_id)
        state = context.state
        process = state.process
        knowledge = state.knowledge
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
                sorted(
                    (to_primitive(item) for item in knowledge.by_type("decision")),
                    key=lambda item: item["identifier"],
                )
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
            clarification_candidates=tuple(to_primitive(item) for item in self._runtime.list_clarification_candidates(context.workspace)),
            clarification_requests=tuple(to_primitive(item) for item in self._runtime.list_clarification_requests(context.workspace)),
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

    def project_decision_log(self, project_id: str) -> ProjectDecisionLogView:
        """P7: журнал решений проекта.

        Агрегируется из ClarificationRequest со статусом answered/assumed.
        Один request = одно решение. Альтернативы — options, которые не
        выбраны. Сортировка — по `updated_at` descending (свежее сверху).
        """
        context = self._load_context(project_id)
        requests = list(self._runtime.list_clarification_requests(context.workspace))
        decisions = [r for r in requests if r.status in ("answered", "assumed")]
        decisions.sort(key=lambda r: r.updated_at or "", reverse=True)
        entries = tuple(
            DecisionLogEntryView(
                decision_id=r.request_id,
                kind=r.status,
                title=r.title,
                question=r.question,
                resolution_summary=r.resolution_summary,
                selected_option_ids=r.selected_option_ids,
                free_text=r.free_text,
                rationale=r.reason,
                impact=r.impact,
                blocking_scope=r.blocking_scope,
                decision_owner_role=r.decision_owner_role,
                source_type=r.source_type,
                source_id=r.source_id,
                affected_task_ids=r.affected_task_ids,
                related_artifact_ids=r.related_artifact_ids,
                alternatives=tuple(
                    ClarificationOptionView(
                        option_id=opt.option_id,
                        label=opt.label,
                        description=opt.description,
                        effect_preview=opt.effect_preview,
                        confidence=opt.confidence,
                    )
                    for opt in r.options
                    if opt.option_id not in r.selected_option_ids
                ),
                auto_resolved=r.auto_resolved,
                decided_at=r.updated_at,
                created_at=r.created_at,
            )
            for r in decisions
        )
        return ProjectDecisionLogView(
            project_id=context.manifest.project_id,
            entries=entries,
            total_count=len(entries),
            answered_count=sum(1 for e in entries if e.kind == "answered"),
            assumed_count=sum(1 for e in entries if e.kind == "assumed"),
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
        pins: list[FailurePinView] = []
        # 1. Open candidates with low confidence — подозрительные места
        for cand in self._runtime.list_clarification_candidates(workspace):
            status = getattr(cand, "status", "open")
            if status != "open":
                continue
            confidence = getattr(cand, "confidence_without_user", None)
            if confidence is not None and confidence >= self._CONFIDENCE_PIN_THRESHOLD:
                continue
            related = getattr(cand, "related_artifact_ids", ()) or ()
            if not related:
                continue
            severity = self._severity_from_priority(getattr(cand, "priority", "medium"))
            for art_id in related:
                if artifact_id_filter is not None and art_id != artifact_id_filter:
                    continue
                pins.append(
                    FailurePinView(
                        pin_id=getattr(cand, "candidate_id", ""),
                        artifact_id=art_id,
                        section_id=getattr(cand, "section_id", None),
                        severity=severity,
                        kind="candidate_open",
                        message=getattr(cand, "title", ""),
                        source_type=getattr(cand, "source_type", "unknown"),
                        source_id=getattr(cand, "source_id", None),
                        confidence_without_user=confidence,
                        related_clarification_id=None,
                    )
                )
        # 2. Assumptions — авто-решения, требующие точечной проверки
        for r in self._runtime.list_clarification_requests(workspace):
            if r.status != "assumed":
                continue
            if not r.related_artifact_ids:
                continue
            severity = self._severity_from_priority(r.priority)
            for art_id in r.related_artifact_ids:
                if artifact_id_filter is not None and art_id != artifact_id_filter:
                    continue
                pins.append(
                    FailurePinView(
                        pin_id=r.request_id,
                        artifact_id=art_id,
                        section_id=None,
                        severity=severity,
                        kind="assumption",
                        message=r.title,
                        source_type=r.source_type,
                        source_id=r.source_id,
                        confidence_without_user=None,
                        related_clarification_id=r.request_id,
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
            elif name == "clarifications":
                values[name] = self.project_clarifications(project_id)
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

    def _load_context(self, project_id: str) -> ProjectContext:
        return self._load_context_by_ref(self._catalog.resolve_workspace(project_id))

    def _load_context_by_ref(self, workspace_ref: WorkspaceRef) -> ProjectContext:
        snapshot, report = self._registry_service.validate()
        if not report.is_valid:
            raise ConflictError("Registry невалиден. Невозможно построить UI-проекции.")
        workspace = workspace_ref.workspace
        return ProjectContext(
            workspace_ref=workspace_ref,
            workspace=workspace,
            manifest=workspace_ref.manifest,
            state=self._runtime.load_project_state(workspace),
            snapshot=snapshot,
        )

    def _clarification_view(self, request) -> ClarificationItemView:
        return ClarificationItemView(
            clarification_id=request.request_id,
            status=request.status,
            priority=request.priority,
            title=request.title,
            question=request.question,
            description=request.description,
            reason=request.reason,
            impact=request.impact,
            answer_mode=request.answer_mode,
            options=tuple(
                ClarificationOptionView(
                    option_id=option.option_id,
                    label=option.label,
                    description=option.description,
                    effect_preview=option.effect_preview,
                    confidence=option.confidence,
                )
                for option in request.options
            ),
            recommended_option_id=request.recommended_option_id,
            visibility=request.visibility,
            default_assumption=request.default_assumption,
            blocking_scope=request.blocking_scope,
            decision_owner_role=request.decision_owner_role,
            auto_resolved=request.auto_resolved,
            affected_task_ids=request.affected_task_ids,
            related_artifact_ids=request.related_artifact_ids,
            selected_option_ids=request.selected_option_ids,
            free_text=request.free_text,
            resolution_summary=request.resolution_summary,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )

    def _build_task_tree(self, workspace: Path, tasks: list[TaskRecord], current_task_id: str | None) -> tuple[TaskNodeView, ...]:
        children_by_parent: dict[str | None, list[TaskRecord]] = {}
        for task in tasks:
            children_by_parent.setdefault(task.parent_task_id, []).append(task)
        open_clarifications = self._runtime.list_clarification_requests(workspace, statuses=("open",))
        clarification_counts: dict[str, int] = {}
        for clarification in open_clarifications:
            for task_id in clarification.affected_task_ids:
                clarification_counts[task_id] = clarification_counts.get(task_id, 0) + 1

        def build(task: TaskRecord) -> TaskNodeView:
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
            )

        return tuple(build(task) for task in sorted(children_by_parent.get(None, []), key=lambda item: item.created_at))

    def _build_situation(self, context: ProjectContext) -> ProjectSituationView:
        self._planning_service.plan(context.workspace, context.snapshot, mode="dry-run", record=False)
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
        open_clarifications = [
            item
            for item in self._runtime.list_clarification_requests(context.workspace, statuses=("open",))
            if item.blocking_scope != "none"
        ]
        for clarification in open_clarifications[:3]:
            blockers.append(
                SituationBlockerView(
                    kind="clarification",
                    title=clarification.title,
                    summary=clarification.question,
                    severity=clarification.priority,
                    detail_view="clarification",
                    related_id=clarification.request_id,
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
        for clarification in self._runtime.list_clarification_requests(context.workspace):
            if clarification.status == "open":
                title = "Система запросила уточнение"
                status = "warning" if clarification.blocking_scope != "none" else "info"
            elif clarification.status == "assumed":
                title = "Принято рабочее допущение"
                status = "info"
            elif clarification.status == "answered":
                title = "Пользователь ответил на уточнение"
                status = "success"
            else:
                title = "Уточнение обновлено"
                status = "info"
            entries.append(
                (
                    (clarification.updated_at or clarification.created_at, 4),
                    TimelineEntryView(
                        sequence=0,
                        kind=f"clarification_{clarification.status}",
                        title=title,
                        summary=clarification.resolution_summary or clarification.question,
                        status=status,
                        created_at=clarification.updated_at or clarification.created_at,
                        detail_view="clarification",
                        entity_type="clarification",
                        entity_id=clarification.request_id,
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

    def _objective_done(self, context: ProjectContext) -> bool:
        objective = context.snapshot.resolve_objective(context.manifest.objective_ref)
        artifacts = {artifact.artifact_role for artifact in self._runtime.list_artifacts(context.workspace)}
        return all(ref.identifier.rsplit(".", 1)[-1] in artifacts for ref in objective.done_artifact_refs)

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
