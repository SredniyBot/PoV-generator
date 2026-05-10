from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProjectListItemView:
    project_id: str
    name: str
    status_label: str
    updated_at: str
    has_blockers: bool
    current_step_title: str | None


@dataclass(frozen=True)
class ObjectiveCatalogItemView:
    objective_ref: str
    title: str
    root_task_ref: str
    required_artifact_count: int


@dataclass(frozen=True)
class DomainPackCatalogItemView:
    pack_ref: str
    name: str
    domain: str
    description: str
    status: str
    entry_signals: tuple[str, ...]


@dataclass(frozen=True)
class ProjectCreatedView:
    project_id: str
    name: str
    objective_ref: str
    domain_pack_refs: tuple[str, ...]
    workspace_path: str
    changed_projections: tuple[str, ...] = field(
        default_factory=lambda: (
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
    )


@dataclass(frozen=True)
class ProjectShellView:
    project_id: str
    name: str
    business_request: str
    objective_ref: str
    active_domain_packs: tuple[str, ...]
    goal: str | None
    status_label: str
    updated_at: str


@dataclass(frozen=True)
class TaskNodeView:
    task_id: str
    task_key: str
    parent_task_id: str | None
    title: str
    template_ref: str
    template_type: str
    status: str
    status_summary: str | None
    origin_kind: str
    origin_ref: str
    slot_id: str | None
    depth: int
    retryable: bool
    is_current: bool
    blocking_clarification_count: int = 0
    children: tuple["TaskNodeView", ...] = ()


@dataclass(frozen=True)
class ProjectTaskGraphView:
    project_id: str
    objective_ref: str
    current_task_id: str | None
    completed_leaf_tasks: int
    total_leaf_tasks: int
    nodes: tuple[TaskNodeView, ...]


@dataclass(frozen=True)
class ActionDescriptor:
    kind: str
    label: str
    description: str
    target_view: str | None = None
    target_id: str | None = None
    command_name: str | None = None
    blocking: bool = False


@dataclass(frozen=True)
class SituationBlockerView:
    kind: str
    title: str
    summary: str
    severity: str
    detail_view: str
    related_id: str | None = None


@dataclass(frozen=True)
class ProjectSituationView:
    project_id: str
    status_label: str
    headline: str
    summary: str
    blocking: bool
    primary_action: ActionDescriptor | None
    secondary_actions: tuple[ActionDescriptor, ...] = ()
    blockers: tuple[SituationBlockerView, ...] = ()


@dataclass(frozen=True)
class TimelineEntryView:
    sequence: int
    kind: str
    title: str
    summary: str
    status: str
    created_at: str
    detail_view: str
    entity_type: str
    entity_id: str | None = None


@dataclass(frozen=True)
class ProjectTimelineView:
    project_id: str
    entries: tuple[TimelineEntryView, ...]
    total_entries: int


@dataclass(frozen=True)
class ClarificationOptionView:
    option_id: str
    label: str
    description: str
    effect_preview: str
    confidence: float | None = None


@dataclass(frozen=True)
class ClarificationItemView:
    clarification_id: str
    status: str
    priority: str
    title: str
    question: str
    description: str
    reason: str
    impact: str
    answer_mode: str
    options: tuple[ClarificationOptionView, ...]
    recommended_option_id: str | None
    min_participation_mode: str
    default_assumption: str | None
    blocking_scope: str
    affected_task_ids: tuple[str, ...]
    related_artifact_ids: tuple[str, ...]
    selected_option_ids: tuple[str, ...]
    free_text: str | None
    resolution_summary: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ProjectClarificationsView:
    project_id: str
    mode: str
    open_count: int
    answered_count: int
    assumed_count: int
    blocking_count: int
    items: tuple[ClarificationItemView, ...]


@dataclass(frozen=True)
class ArtifactSummaryView:
    artifact_id: str
    artifact_role: str
    title: str
    created_at: str
    created_by_task_id: str | None
    has_markdown: bool


@dataclass(frozen=True)
class ArtifactValidationView:
    validation_run_id: str
    status: str
    finding_messages: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class ArtifactDetailView:
    artifact_id: str
    artifact_role: str
    title: str
    description: str
    created_at: str
    created_by_task_id: str | None
    template_ref: str | None
    json_content: str
    markdown_content: str | None
    validations: tuple[ArtifactValidationView, ...] = ()


@dataclass(frozen=True)
class ReviewIssueView:
    severity: str
    message: str


@dataclass(frozen=True)
class ProjectReviewView:
    project_id: str
    status: str
    summary: str | None
    strengths: tuple[str, ...]
    issues: tuple[ReviewIssueView, ...]
    recommendations: tuple[str, ...]
    artifact_id: str | None
    updated_at: str | None


@dataclass(frozen=True)
class ProjectStateView:
    project_id: str
    goal: str | None
    active_gaps: tuple[dict[str, object], ...]
    assumptions: tuple[dict[str, object], ...]
    decisions: tuple[dict[str, object], ...]
    readiness: tuple[dict[str, object], ...]
    known_facts: tuple[dict[str, object], ...]
    active_domain_packs: tuple[dict[str, object], ...]
    active_methodology_packs: tuple[dict[str, object], ...]
    clarification_mode: str
    root_task_id: str | None
    updated_at: str


@dataclass(frozen=True)
class ContextManifestSummaryView:
    manifest_id: str
    task_id: str
    template_ref: str
    problem_state_version: int
    used_tokens: int
    max_input_tokens: int
    item_count: int
    created_at: str


@dataclass(frozen=True)
class ProjectDebugView:
    project_id: str
    tasks: tuple[dict[str, object], ...]
    task_events: tuple[dict[str, object], ...]
    planning_history: tuple[dict[str, object], ...]
    execution_runs: tuple[dict[str, object], ...]
    execution_traces: tuple[dict[str, object], ...]
    context_manifests: tuple[ContextManifestSummaryView, ...]
    validation_runs: tuple[dict[str, object], ...]
    escalations: tuple[dict[str, object], ...]
    clarification_candidates: tuple[dict[str, object], ...] = ()
    clarification_requests: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class CommandResultView:
    status: str
    command_name: str
    summary: str
    changed_projections: tuple[str, ...] = field(default_factory=tuple)
    resource_id: str | None = None


@dataclass(frozen=True)
class OverviewClarificationItem:
    clarification_id: str
    title: str
    priority: str
    blocking_scope: str
    source_type: str


@dataclass(frozen=True)
class OverviewArtifactItem:
    artifact_id: str
    artifact_role: str
    title: str
    created_at: str


@dataclass(frozen=True)
class ObjectiveProgressView:
    artifacts_required: int
    artifacts_ready: int
    gates_required: int
    gates_passed: int


@dataclass(frozen=True)
class ProjectOverviewView:
    project_id: str
    name: str
    objective_ref: str
    stage_summary: str
    current_activity: str
    objective_progress: ObjectiveProgressView
    critical_clarifications: tuple[OverviewClarificationItem, ...]
    key_artifacts: tuple[OverviewArtifactItem, ...]
    active_methodology: str | None
    active_domain_packs: tuple[str, ...]
    clarification_mode: str
    updated_at: str
