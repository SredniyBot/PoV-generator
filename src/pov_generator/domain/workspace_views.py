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
    # Время последней смены статуса. Для status=in_progress это время старта
    # текущего LLM-вызова — UI отображает в виджете workflow реальный
    # секундомер «задача X работает T сек.».
    updated_at: str = ""
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


# --- v3.0 — Decision ledger views --------------------------------------------
#
# Эти view зеркалируют доменную модель Decision из
# `domain/decisions.py`, но «уплощают» вложенные альтернативы и
# добавляют производные поля для UI (на каком уровне находится, что
# выбрано, нужно ли подсветить рискованным). Сериализуются как dict
# через `to_primitive` в REST endpoint.


@dataclass(frozen=True)
class DecisionAlternativeView:
    option_id: str
    label: str
    description: str
    pros: tuple[str, ...]
    cons: tuple[str, ...]
    confidence: float | None
    is_chosen: bool


@dataclass(frozen=True)
class DecisionItemView:
    decision_id: str
    project_id: str
    title: str
    description: str
    category: str
    level: str  # effective_level (с учётом возможной user-переклассификации)
    raw_level: str  # исходный уровень от LLM, если был переопределён
    level_rationale: str
    rationale: str
    chosen_option_id: str
    chosen_option_label: str  # для UI: «PostgreSQL», не option_id
    alternatives: tuple[DecisionAlternativeView, ...]
    confidence: float
    is_low_confidence: bool  # маркер для подсветки рискованного
    status: str
    source: str
    source_task_id: str | None
    affected_artifact_ids: tuple[str, ...]
    depends_on_decision_ids: tuple[str, ...]
    user_action: str
    was_user_modified: bool
    user_free_text_answer: str | None
    created_at: str
    updated_at: str
    # v3.1: миграция clarifications → decisions
    answer_mode: str = "single"
    chosen_option_ids: tuple[str, ...] = ()
    # v3.4: пользовательская верификация рискового решения.
    # Снимает маркер is_low_confidence в UI без изменения самого решения.
    user_verified: bool = False
    user_verified_at: str | None = None
    # v3.9: list endpoints can return compact items and let callers lazy-load
    # heavy alternatives/rationale through the detail endpoint.
    details_included: bool = True


@dataclass(frozen=True)
class ProjectDecisionsView:
    """Реестр решений проекта с агрегатами для UI.

    Counts по уровням нужны для бэйджей в навигации; counts по статусу —
    для разделения «требует моего внимания» vs «решено само». Items в
    том же порядке, что в БД (хронология появления решений).
    """

    project_id: str
    mode: str
    # Счётчики «сколько решений на твоём уровне» для текущего mode.
    surfaced_total: int
    surfaced_pending: int
    # По уровням (всегда все три, даже если 0).
    business_count: int
    architecture_count: int
    detail_count: int
    # По статусам.
    proposed_count: int
    accepted_count: int
    overridden_count: int
    low_confidence_count: int  # для индикатора «X рискованных решений»
    items: tuple[DecisionItemView, ...]


# --- v3.0 — Checkpoint session views -----------------------------------------


@dataclass(frozen=True)
class CheckpointSessionView:
    """View checkpoint-сессии для UI.

    В отличие от доменной CheckpointSession включает развёрнутые
    Decision-карточки, а не только их id — UI рисует сессию одним
    запросом, без дополнительных round-trip'ов.
    """

    session_id: str
    project_id: str
    task_id: str
    task_title: str
    artifact_role: str
    status: str
    created_at: str
    finalized_at: str | None
    finalized_by: str | None
    decisions: tuple[DecisionItemView, ...]


@dataclass(frozen=True)
class ProjectCheckpointsView:
    """Список checkpoint-сессий проекта.

    pending_count — для бэйджа в навигации проекта («3 решения ждут
    вашего внимания»). items в обратном хронологическом порядке —
    свежие сверху.
    """

    project_id: str
    pending_count: int
    items: tuple[CheckpointSessionView, ...]


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
    # Метаинформация артефакта (Этапы 1 + 5). Раньше показывалась только
    # через provenance-модалку; теперь публикуется в карточке артефакта.
    artifact_kind: str = "primary"
    provider: str | None = None
    model: str | None = None
    complexity: str | None = None
    methodology_pack_ref: str | None = None
    merge_strategy: str | None = None
    used_position_ids: tuple[str, ...] = ()
    input_artifact_ids: tuple[str, ...] = ()
    parent_artifact_id: str | None = None
    is_superseded: bool = False
    overall_confidence: float | None = None
    # v3.5: разбивка токенов по стадиям сборки этого артефакта.
    # Ключи стадий: pre_flight_planning, primary_generation,
    # methodology_stage:<id>. Значения: {input_tokens, output_tokens,
    # cache_read_tokens, cache_write_tokens, total_tokens}.
    token_usage: dict[str, dict[str, int]] = field(default_factory=dict)


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
    # v3.1: единый реестр решений вместо двух legacy-полей clarification_*.
    decisions: tuple[dict[str, object], ...] = ()


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


# ---------------------------------------------------------------------------
# L6 design extensions (P3 v2 skeleton, P5 failure pins, P7 decisions, P8 versions)
#
# Эти views агрегируют существующие данные без миграций БД. Все поля —
# производные от ArtifactRecord / ClarificationRequest / ClarificationCandidate
# / ProblemState.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSectionView:
    """Раздел артефакта со статусом — для P3 v2 skeleton mission control."""

    section_id: str
    title: str
    status: str  # "done" | "in_progress" | "pending" | "needs_review"
    summary: str | None  # короткая выжимка содержания (≤ 200 символов)
    has_pins: bool  # есть ли P5 failure pins в этом разделе
    pin_count: int


@dataclass(frozen=True)
class ArtifactSkeletonView:
    """Skeleton артефакта: список разделов со статусами + сводный прогресс.

    Используется на L1 Обзоре проекта (P3 v2). Парсит json_content
    артефакта по эвристике "top-level dict keys = разделы".
    """

    project_id: str
    artifact_id: str
    artifact_role: str
    title: str
    sections: tuple[ArtifactSectionView, ...]
    sections_done: int
    sections_total: int
    has_markdown: bool
    created_at: str


@dataclass(frozen=True)
class ArtifactVersionItemView:
    """Одна версия артефакта в цепочке (P8 snapshots)."""

    artifact_id: str
    artifact_role: str
    title: str
    label: str  # "v1", "v2", … или "v1 — 11.05"
    is_current: bool
    created_at: str
    created_by_task_id: str | None
    parent_artifact_id: str | None
    description: str


@dataclass(frozen=True)
class ProjectArtifactVersionsView:
    """Все цепочки версий артефактов проекта, сгруппированные по artifact_role.

    Цепочка строится по parent_artifact_id когда поле заполнено; иначе
    fallback: артефакты с одним artifact_role сортируются по created_at,
    последний = current.
    """

    project_id: str
    chains: tuple[tuple[ArtifactVersionItemView, ...], ...]  # каждая цепочка — tuple версий от старой к новой


@dataclass(frozen=True)
class FailurePinView:
    """Маркер «здесь система не уверена / есть допущение» (P5)."""

    pin_id: str  # candidate_id или request_id
    artifact_id: str
    section_id: str | None  # если можно определить раздел; иначе раздел "общий"
    severity: str  # "high" | "medium" | "low" — из priority кандидата
    kind: str  # "candidate_open" | "assumption" | "validation_finding"
    message: str  # title / question / message
    source_type: str
    source_id: str | None
    confidence_without_user: float | None
    related_clarification_id: str | None


@dataclass(frozen=True)
class ProjectFailurePinsView:
    """Сводка failure pins по проекту (или фильтр по артефакту)."""

    project_id: str
    artifact_id: str | None  # None = по всему проекту
    pins: tuple[FailurePinView, ...]
    total_count: int
