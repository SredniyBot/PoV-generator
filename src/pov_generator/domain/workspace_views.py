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
    # История прошлых активных objective'ов (без текущего). Появляется,
    # когда workspace прошёл хотя бы одну смену цели через
    # ``activate_next_objective`` — например, ТЗ → архитектура.
    objective_history: tuple[str, ...] = ()
    # Сслыки на objective'ы, которые можно активировать как следующие
    # после текущего. Берутся из ``ObjectiveSpec.compatible_next_objectives``.
    compatible_next_objectives: tuple[str, ...] = ()
    # Все ``done_when.artifacts`` текущего objective'а созданы — UI
    # может показать кнопку перехода на следующий objective.
    objective_complete: bool = False


@dataclass(frozen=True)
class FanOutMeta:
    source_artifact_role: str
    total_instances: int
    completed_instances: int
    producer_task_id: str | None = None


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
    fan_out_meta: FanOutMeta | None = None
    # Ф1: доступна ли задача для действий. У неактивного (ещё не запущенного)
    # гейта задачи показываются как скелет и помечаются недоступными.
    available: bool = True
    # #2: задача исполняется автономным агентом (executor: harness) — граф
    # выделяет такие узлы (иконка/рамка), они работают иначе, чем LLM-узлы.
    is_harness: bool = False


@dataclass(frozen=True)
class ProjectTaskGraphView:
    project_id: str
    objective_ref: str
    current_task_id: str | None
    completed_leaf_tasks: int
    total_leaf_tasks: int
    nodes: tuple[TaskNodeView, ...]
    # Ф1: состояние гейта этого графа (done|active|locked) и его заголовок —
    # для подвкладок графа по гейтам. Активный гейт == прежнее поведение.
    objective_state: str = "active"
    title: str = ""


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
    # Низкая уверенность → мягкий маркер «подтвердите» (зеркально решениям).
    overall_confidence: float | None = None
    is_low_confidence: bool = False
    user_verified: bool = False
    # Архив: артефакт заархивирован откатом (archived) или заменён более новой
    # версией (is_superseded). В основном списке не показываются — только в «Архиве».
    archived: bool = False
    is_superseded: bool = False
    # Категория для подвкладок списка артефактов: documents | code | binary |
    # data | other. Структурные/markdown → documents; бандлы — по bundle_kind.
    category: str = "documents"


@dataclass(frozen=True)
class AttachmentView:
    """Проекция входного файла-вложения для UI (вкладка «Входные файлы»)."""

    attachment_id: str
    original_filename: str
    mime_type: str
    size_bytes: int
    extraction_status: str
    extraction_error: str | None
    used_in_context: bool
    can_delete: bool
    created_at: str
    # Реквизиты v2: "input" (входной материал) | "requisite" (файл-реквизит).
    purpose: str = "input"


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
    # Низкая уверенность → мягкий маркер «подтвердите» + метка подтверждения.
    is_low_confidence: bool = False
    user_verified: bool = False
    user_verified_at: str | None = None
    # Ф3: согласование итогового артефакта с заказчиком (sign-off). Тумблер в
    # окне артефакта; прохождение human_approval-гейта считается по нему.
    signed_off: bool = False
    signed_off_at: str | None = None
    # Разбивка токенов по стадиям сборки этого артефакта (метадата для карточки).
    # Ключи: primary_generation, methodology_stage:<id>, decision_identification.
    token_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    # Агрегат расхода токенов задачи (все её LLM-вызовы) из llm_usage-БД.
    # None в полях usage_* = «n/a» (провайдер не дал данных).
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_total_tokens: int | None = None
    # "actual" | "estimated" | None. estimated → UI помечает «оценка».
    usage_source: str | None = None
    usage_call_count: int = 0
    # Прошлые версии того же артефакта (с предыдущих запусков / неудачные /
    # заменённые), включая заархивированные откатом. От старой к новой, без
    # самой текущей. UI показывает их в подвкладке «Предыдущие версии (N)».
    previous_versions: tuple["ArtifactVersionItemView", ...] = ()
    # #2: бандл-артефакт (код/файлы) — дерево файлов для просмотра в окне.
    # Контент файла подгружается отдельным запросом (файлы бывают крупными).
    is_bundle: bool = False
    bundle_kind: str | None = None
    bundle_files: tuple["BundleFileView", ...] = ()


@dataclass(frozen=True)
class BundleFileView:
    """Один файл бандла для просмотра в окне артефакта (метаданные; контент —
    подгружается отдельным запросом ``/bundle/file``)."""

    path: str
    content_kind: str
    size_bytes: int


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
    # Учёт токенов: детализация по вызовам + агрегат по проекту.
    llm_usage: tuple[dict[str, object], ...] = ()
    llm_usage_total: dict[str, object] | None = None


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
# Stage status bar — степпер этапов (gate stepper) над вкладками.
#
# Проекция `stages`: цепочка objective'ов проекта как этапы-гейты
# (ТЗ → Архитектура → Реализация) со статусом каждого + прогресс и ошибки
# активного этапа. Единый источник правды для постоянного статус-бара
# (заменяет разрозненные шапку/Обзор/run-панель).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageFailingTaskView:
    """Упавшая/заблокированная задача активного этапа — для поповера бара."""

    task_id: str  # для дип-линка на граф (?focus=<task_id>)
    title: str
    status: str  # "failed" | "blocked" | "waiting_for_children"
    reason: str
    retryable: bool


@dataclass(frozen=True)
class StagePendingDecisionView:
    """Открытое решение активного этапа («ждут ответа») — для поповера бара."""

    decision_id: str
    title: str
    level: str  # business | architecture | detail


@dataclass(frozen=True)
class StageView:
    """Один этап (objective) в степпере.

    Счётчики ошибок/блокировок (``failed_count``/``blocked_count``/
    ``awaiting_signoff``/``failing_tasks``) считаются ТОЛЬКО для активного
    этапа; для done/locked они = 0. Причина: задачи реплана́тся по stable-key,
    и завершённые прошлые этапы не хранят историю падений — ошибки значимы на
    активном этапе.

    ``blocked_count`` — только actionable-блокировки (задача ждёт открытого
    решения), НЕ обычная очерёдность (ожидание upstream-артефакта).
    ``awaiting_signoff`` — число открытых proposed-Decisions («ждут решений»).
    """

    objective_ref: str
    title: str
    state: str  # "done" | "active" | "locked"
    is_current: bool
    artifacts_required: int
    artifacts_ready: int
    gates_required: int
    gates_passed: int
    failed_count: int
    blocked_count: int
    awaiting_signoff: int
    failing_tasks: tuple[StageFailingTaskView, ...]
    pending_decisions: tuple[StagePendingDecisionView, ...]
    # Ключевой дилеверабл этапа (ТЗ/Архитектура/...) — последний primary-артефакт
    # из done-артефактов цели. UI открывает его по клику на завершённый этап.
    key_artifact_id: str | None = None
    # Ф3: итоговый артефакт этапа согласован с заказчиком (sign-off). Для
    # активного этапа с готовыми артефактами, но без согласования — UI красит
    # этап жёлтым и блокирует «Следующий этап».
    signed_off: bool = False


@dataclass(frozen=True)
class ProjectStagesView:
    project_id: str
    objective_ref: str  # активный этап
    stages: tuple[StageView, ...]  # history(done) → active → forward-walk(locked)
    next_objective_refs: tuple[str, ...]  # compatible_next активного этапа
    objective_complete: bool  # активный этап завершён → можно «Перейти»
    # Ф5: непредоставленные блокирующие реквизиты — UI гасит переход на
    # реализацию и показывает, чего не хватает. Пусто → переход не держится.
    blocked_by_requisites: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Реквизиты — требуемые от пользователя входные данные (этап «Реализация»).
#
# Это НЕ приговор реализуемости, а отдельный канал просьб «дайте то-то»
# (доступ / файл / настройка / значение). На фазе 1 выводятся из предусловий
# артефакта оценки реализуемости; блокеры (причины невыполнимости) сюда не
# попадают — они относятся к статусу реализуемости, а не к запросу данных.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequisiteItemView:
    """Одна требуемая от пользователя единица входных данных."""

    title: str        # что нужно предоставить
    needed_for: str   # для какой части/требования это нужно
    status: str       # "requested" | "provided"
    # Ф5: многоисточниковая агрегация (реализуемость + архитектура).
    key: str = ""     # устойчивый ключ провижена (если пуст — берётся title)
    kind: str = "other"          # credential|dataset|file|setting|interface_format|sample|other
    blocking: bool = False       # блокирует переход на реализацию
    stage: str = "realizability" # источник: realizability | architecture
    # Реквизиты v2 (план 2026-06-08):
    consumer_ref: str = ""       # кому нужен: component_id/stable-key задачи (пусто = ранний запрос без привязки)
    # Структура реквизита (редизайн): конкретность вместо «пустых слов».
    why: str = ""                # зачем это нужно (1 фраза) — обоснование запроса
    example: str = ""            # конкретный пример/формат («например, CSV id,date,amount»)
    input_kind: str = "text"     # выводимая форма ввода для UI: text | file | access
    # Что и как предоставлено (для потребления и UI). Пусто, если не предоставлено.
    # provided_mode: "value" | "file" | "reference" (legacy note → "reference").
    # provided_value не используется для секретов (credential идёт через reference).
    provided_mode: str = ""
    provided_value: str = ""
    provided_note: str = ""
    provided_attachment_id: str = ""


@dataclass(frozen=True)
class ProjectRequisitesView:
    project_id: str
    status: str  # "ready" | "missing" (нет артефакта реализуемости)
    items: tuple[RequisiteItemView, ...]          # actionable: конкретные запросы данных (архитектура)
    source_artifact_id: str | None
    updated_at: str | None
    # Advisory: предпосылки реализуемости (условия, не «дай сейчас»). Показываются
    # мягко/отдельно — это ранние подсказки, конкретные запросы появятся на
    # архитектуре. Не предоставляются и не считаются в бейдже.
    advisory: tuple[RequisiteItemView, ...] = ()


# ---------------------------------------------------------------------------
# Зоны роста (пробелы в умениях) — требования, которые не закрыло ни одно
# умение каталога. Не приговор «никогда», а кандидат на расширение каталога:
# показываем заказчику как «пока не умеем», команде — как backlog роста.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityGapView:
    """Один пробел в умениях (зона роста)."""

    title: str        # что просили
    reason: str       # почему не закрыто умением
    suggestion: str   # как можно закрыть (если оценка подсказала), иначе пусто


@dataclass(frozen=True)
class ProjectGapsView:
    project_id: str
    status: str  # "ready" | "missing"
    items: tuple[CapabilityGapView, ...]
    source_artifact_id: str | None
    updated_at: str | None


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
    # Версия заархивирована откатом (а не просто заменена более новой).
    archived: bool = False


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


# ---------------------------------------------------------------------------
# Ролбек шага — превью инвалидации и история выполненных откатов.
#
# Превью отвечает на вопрос «что я потеряю, откатив этот шаг»: множество
# зависимых шагов (целевой + транзитивно зависящие) и артефакты, которые
# будут заархивированы. История — аудит выполненных откатов для вкладки.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackStepView:
    """Один шаг, который будет инвалидирован откатом."""

    task_id: str
    title: str
    template_ref: str
    status: str
    is_target: bool  # сам выбранный шаг (vs зависящий от него)


@dataclass(frozen=True)
class RollbackArtifactView:
    """Артефакт, который будет заархивирован откатом."""

    artifact_id: str
    artifact_role: str
    title: str
    created_by_task_id: str | None


@dataclass(frozen=True)
class RollbackPreviewView:
    """Превью отката: что будет инвалидировано/заархивировано (до подтверждения)."""

    project_id: str
    target_task_id: str
    target_title: str
    reverted_steps: tuple[RollbackStepView, ...]
    archived_artifacts: tuple[RollbackArtifactView, ...]
    # Доступен ли откат для этого шага. Откат требует точку восстановления
    # (чекпоинт pre-state). У шагов, выполненных до появления механизма
    # отката, чекпоинта нет — откат недоступен, UI гасит подтверждение.
    rollbackable: bool = True
    blocked_reason: str = ""


@dataclass(frozen=True)
class RollbackHistoryItemView:
    """Один выполненный откат (запись аудита)."""

    rollback_id: str
    target_task_id: str
    target_title: str
    reverted_count: int
    archived_artifact_count: int
    actor: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class ProjectRollbackHistoryView:
    """История откатов проекта (свежие сверху)."""

    project_id: str
    items: tuple[RollbackHistoryItemView, ...]
