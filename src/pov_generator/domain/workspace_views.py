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
class RecipeCatalogItemView:
    recipe_ref: str
    name: str
    domain: str
    stage_gate: str
    step_count: int


@dataclass(frozen=True)
class DomainPackCatalogItemView:
    pack_ref: str
    name: str
    domain: str
    description: str
    status: str
    entry_signals: tuple[str, ...]


@dataclass(frozen=True)
class ProjectShellView:
    project_id: str
    name: str
    business_request: str
    recipe_ref: str
    enabled_domain_packs: tuple[str, ...]
    goal: str | None
    status_label: str
    updated_at: str


@dataclass(frozen=True)
class JourneyStepView:
    step_id: str
    title: str
    template_ref: str
    source_kind: str
    source_ref: str
    status: str
    required: bool
    is_current: bool


@dataclass(frozen=True)
class ProjectJourneyView:
    project_id: str
    recipe_ref: str
    domain_pack_refs: tuple[str, ...]
    recipe_fragment_refs: tuple[str, ...]
    current_step_id: str | None
    completed_steps: int
    total_steps: int
    steps: tuple[JourneyStepView, ...]


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
    readiness: tuple[dict[str, object], ...]
    known_facts: tuple[dict[str, object], ...]
    enabled_domain_packs: tuple[dict[str, object], ...]
    recipe_composition: dict[str, object] | None
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


@dataclass(frozen=True)
class CommandResultView:
    status: str
    command_name: str
    summary: str
    changed_projections: tuple[str, ...] = field(default_factory=tuple)
    resource_id: str | None = None


@dataclass(frozen=True)
class ProjectCreatedView:
    project_id: str
    name: str
    recipe_ref: str
    domain_pack_refs: tuple[str, ...]
    workspace_path: str
    changed_projections: tuple[str, ...] = field(
        default_factory=lambda: (
            "shell",
            "journey",
            "situation",
            "timeline",
            "artifacts",
            "review",
            "state",
            "debug",
        )
    )
