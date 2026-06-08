export type ProjectionName =
  | "shell"
  | "task_graph"
  | "situation"
  | "timeline"
  | "artifacts"
  | "attachments"
  | "review"
  | "state"
  | "debug"
  | "overview"
  | "methodology"
  | "stages"
  | "workflow_runs";

export interface AttachmentView {
  attachment_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  extraction_status: "pending" | "succeeded" | "failed" | "unsupported";
  extraction_error: string | null;
  used_in_context: boolean;
  can_delete: boolean;
  created_at: string;
}

export interface ProjectListItemView {
  project_id: string;
  name: string;
  status_label: string;
  updated_at: string;
  has_blockers: boolean;
  current_step_title: string | null;
}

export interface ObjectiveCatalogItemView {
  objective_ref: string;
  title: string;
  root_task_ref: string;
  required_artifact_count: number;
}

export interface DomainPackCatalogItemView {
  pack_ref: string;
  name: string;
  domain: string;
  description: string;
  status: string;
  entry_signals: string[];
}

export interface ProjectCreatedView {
  project_id: string;
  name: string;
  objective_ref: string;
  domain_pack_refs: string[];
  workspace_path: string;
  changed_projections: ProjectionName[];
}

export interface ProjectShellView {
  project_id: string;
  name: string;
  business_request: string;
  objective_ref: string;
  active_domain_packs: string[];
  goal: string | null;
  status_label: string;
  updated_at: string;
  objective_history: string[];
  compatible_next_objectives: string[];
  objective_complete: boolean;
}

export interface FanOutMeta {
  source_artifact_role: string;
  total_instances: number;
  completed_instances: number;
  producer_task_id?: string | null;
}

export interface TaskNodeView {
  task_id: string;
  task_key: string;
  parent_task_id: string | null;
  title: string;
  template_ref: string;
  template_type: string;
  status: string;
  status_summary: string | null;
  error_message?: string | null;
  origin_kind: string;
  origin_ref: string;
  slot_id: string | null;
  depth: number;
  retryable: boolean;
  is_current: boolean;
  blocking_clarification_count: number;
  updated_at: string;
  children: TaskNodeView[];
  fan_out_meta?: FanOutMeta | null;
  /** Ф1: доступна ли задача для действий. У неактивного гейта (скелет/история)
   *  задачи помечены недоступными (read-only, приглушены). По умолчанию true. */
  available?: boolean;
}

export interface ProjectTaskGraphView {
  project_id: string;
  objective_ref: string;
  current_task_id: string | null;
  completed_leaf_tasks: number;
  total_leaf_tasks: number;
  nodes: TaskNodeView[];
  /** Ф1: состояние гейта этого графа и его заголовок (для подвкладок). */
  objective_state?: "done" | "active" | "locked";
  title?: string;
}

export interface ActionDescriptor {
  kind: string;
  label: string;
  description: string;
  target_view: string | null;
  target_id: string | null;
  command_name: string | null;
  blocking: boolean;
}

export interface SituationBlockerView {
  kind: string;
  title: string;
  summary: string;
  severity: string;
  detail_view: string;
  related_id: string | null;
}

export interface ProjectSituationView {
  project_id: string;
  status_label: string;
  headline: string;
  summary: string;
  blocking: boolean;
  primary_action: ActionDescriptor | null;
  secondary_actions: ActionDescriptor[];
  blockers: SituationBlockerView[];
}

export interface TimelineEntryView {
  sequence: number;
  kind: string;
  title: string;
  summary: string;
  status: string;
  created_at: string;
  detail_view: string;
  entity_type: string;
  entity_id: string | null;
}

export interface ProjectTimelineView {
  project_id: string;
  entries: TimelineEntryView[];
  total_entries: number;
}

export interface ArtifactSummaryView {
  artifact_id: string;
  artifact_role: string;
  title: string;
  created_at: string;
  created_by_task_id: string | null;
  has_markdown: boolean;
  overall_confidence: number | null;
  is_low_confidence: boolean;
  user_verified: boolean;
  /** Архив: заархивирован откатом / заменён более новой версией. */
  archived?: boolean;
  is_superseded?: boolean;
}

export interface ArtifactValidationView {
  validation_run_id: string;
  status: string;
  finding_messages: string[];
  created_at: string;
}

export interface TokenUsageStage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

export interface ArtifactDetailView {
  artifact_id: string;
  artifact_role: string;
  title: string;
  description: string;
  created_at: string;
  created_by_task_id: string | null;
  template_ref: string | null;
  json_content: string;
  markdown_content: string | null;
  validations: ArtifactValidationView[];
  // Metadata Этапов 1 + 5.
  artifact_kind: string;
  provider: string | null;
  model: string | null;
  complexity: string | null;
  methodology_pack_ref: string | null;
  merge_strategy: string | null;
  used_position_ids: string[];
  input_artifact_ids: string[];
  parent_artifact_id: string | null;
  is_superseded: boolean;
  overall_confidence: number | null;
  is_low_confidence: boolean;
  user_verified: boolean;
  user_verified_at: string | null;
  // Ф3: согласование итогового артефакта с заказчиком (тумблер sign-off).
  signed_off: boolean;
  signed_off_at: string | null;
  // Разбивка токенов по стадиям сборки (метадата для карточки).
  token_usage: Record<string, TokenUsageStage>;
  // Агрегат расхода токенов задачи из llm_usage-БД. null = «n/a».
  usage_input_tokens: number | null;
  usage_output_tokens: number | null;
  usage_total_tokens: number | null;
  usage_source: "actual" | "estimated" | null;
  usage_call_count: number;
  /** Прошлые версии того же артефакта (включая заархивированные), от старой
   *  к новой, без текущей. Для подвкладки «Предыдущие версии (N)». */
  previous_versions?: ArtifactVersionItemView[];
}

export interface ReviewIssueView {
  severity: string;
  message: string;
}

export interface ProjectReviewView {
  project_id: string;
  status: string;
  summary: string | null;
  strengths: string[];
  issues: ReviewIssueView[];
  recommendations: string[];
  artifact_id: string | null;
  updated_at: string | null;
}

export interface ProjectStateView {
  project_id: string;
  goal: string | null;
  active_gaps: Record<string, unknown>[];
  assumptions: Record<string, unknown>[];
  decisions: Record<string, unknown>[];
  readiness: Record<string, unknown>[];
  known_facts: Record<string, unknown>[];
  active_domain_packs: Record<string, unknown>[];
  active_methodology_packs: Record<string, unknown>[];
  clarification_mode: string;
  root_task_id: string | null;
  updated_at: string;
}

export interface ContextManifestSummaryView {
  manifest_id: string;
  task_id: string;
  template_ref: string;
  problem_state_version: number;
  used_tokens: number;
  max_input_tokens: number;
  item_count: number;
  created_at: string;
}

export interface ProjectDebugView {
  project_id: string;
  tasks: Record<string, unknown>[];
  task_events: Record<string, unknown>[];
  planning_history: Record<string, unknown>[];
  execution_runs: Record<string, unknown>[];
  execution_traces: Record<string, unknown>[];
  context_manifests: ContextManifestSummaryView[];
  validation_runs: Record<string, unknown>[];
  escalations: Record<string, unknown>[];
  clarification_candidates: Record<string, unknown>[];
  clarification_requests: Record<string, unknown>[];
}

export interface CommandResultView {
  status: string;
  command_name: string;
  summary: string;
  changed_projections: ProjectionName[];
  resource_id: string | null;
}

// --- Ролбек шага -----------------------------------------------------------

export interface RollbackStepView {
  task_id: string;
  title: string;
  template_ref: string;
  status: string;
  is_target: boolean; // сам выбранный шаг (vs зависящий от него)
}

export interface RollbackArtifactView {
  artifact_id: string;
  artifact_role: string;
  title: string;
  created_by_task_id: string | null;
}

export interface RollbackPreviewView {
  project_id: string;
  target_task_id: string;
  target_title: string;
  reverted_steps: RollbackStepView[];
  archived_artifacts: RollbackArtifactView[];
  // Доступен ли откат (есть ли чекпоинт). Для шагов, выполненных до появления
  // механизма отката, чекпоинта нет — подтверждение гасится.
  rollbackable: boolean;
  blocked_reason: string;
}

export interface RollbackResultView {
  rollback_id: string;
  target_task_id: string;
  reverted_task_ids: string[];
  archived_artifact_ids: string[];
  restored_objective_ref: string;
}

export interface RollbackHistoryItemView {
  rollback_id: string;
  target_task_id: string;
  target_title: string;
  reverted_count: number;
  archived_artifact_count: number;
  actor: string;
  reason: string;
  created_at: string;
}

export interface ProjectRollbackHistoryView {
  project_id: string;
  items: RollbackHistoryItemView[];
}

export interface HealthView {
  status: string;
  time: string;
  runtime_root: string;
}

export interface WsSnapshotMessage {
  type: "snapshot";
  project_id: string;
  projections: ProjectionName[];
  signatures: Record<string, string>;
}

export interface WsProjectionChangedMessage {
  type: "projection_changed";
  project_id: string;
  projection: ProjectionName;
  signature: string;
}

export interface WsErrorMessage {
  type: "error";
  message: string;
}

export type WsMessage = WsSnapshotMessage | WsProjectionChangedMessage | WsErrorMessage;


export interface OverviewClarificationItem {
  clarification_id: string;
  title: string;
  priority: string;
  blocking_scope: string;
  source_type: string;
}

export interface OverviewArtifactItem {
  artifact_id: string;
  artifact_role: string;
  title: string;
  created_at: string;
}

export interface ObjectiveProgressView {
  artifacts_required: number;
  artifacts_ready: number;
  gates_required: number;
  gates_passed: number;
}

export interface ProjectOverviewView {
  project_id: string;
  name: string;
  objective_ref: string;
  stage_summary: string;
  current_activity: string;
  objective_progress: ObjectiveProgressView;
  critical_clarifications: OverviewClarificationItem[];
  key_artifacts: OverviewArtifactItem[];
  active_methodology: string | null;
  active_domain_packs: string[];
  clarification_mode: string;
  updated_at: string;
}


// ---- Stage status bar (gate stepper) -------------------------------------

export interface StageFailingTaskView {
  task_id: string;
  title: string;
  status: string; // "failed" | "blocked"
  reason: string;
  retryable: boolean;
}

export interface StagePendingDecisionView {
  decision_id: string;
  title: string;
  level: string; // business | architecture | detail
}

export interface StageView {
  objective_ref: string;
  title: string;
  state: "done" | "active" | "locked";
  is_current: boolean;
  artifacts_required: number;
  artifacts_ready: number;
  gates_required: number;
  gates_passed: number;
  failed_count: number;
  blocked_count: number;
  awaiting_signoff: number;
  failing_tasks: StageFailingTaskView[];
  pending_decisions: StagePendingDecisionView[];
  /** Ключевой дилеверабл этапа (ТЗ/Архитектура/...). Клик по завершённому
   *  этапу открывает этот артефакт. null — артефакта ещё нет. */
  key_artifact_id?: string | null;
  /** Ф3: итоговый артефакт этапа согласован с заказчиком. Активный этап с
   *  готовыми артефактами, но signed_off=false → жёлтый + блок «Следующий этап». */
  signed_off?: boolean;
}

export interface ProjectStagesView {
  project_id: string;
  objective_ref: string;
  stages: StageView[];
  next_objective_refs: string[];
  objective_complete: boolean;
  blocked_by_requisites?: string[]; // Ф5: непредоставленные блокирующие реквизиты
}

// ---- Реквизиты (требуемые от пользователя входные данные) ----------------

export interface RequisiteItemView {
  title: string;
  needed_for: string;
  status: string; // "requested" | "provided"
  key?: string; // устойчивый ключ провижена (если пуст — берётся title)
  kind?: string; // credential|dataset|file|setting|interface_format|sample|other
  blocking?: boolean; // блокирует переход на реализацию
  stage?: string; // realizability | architecture
}

export interface ProjectRequisitesView {
  project_id: string;
  status: string; // "ready" | "missing"
  items: RequisiteItemView[];
  source_artifact_id: string | null;
  updated_at: string | null;
}

export interface CapabilityGapView {
  title: string;
  reason: string;
  suggestion: string;
}

export interface ProjectGapsView {
  project_id: string;
  status: string; // "ready" | "missing"
  items: CapabilityGapView[];
  source_artifact_id: string | null;
  updated_at: string | null;
}


// ---- Async workflow runs (W4.1 / R1) -------------------------------------

export type WorkflowRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface WorkflowStepView {
  sequence: number;
  task_id: string | null;
  task_key: string | null;
  // Человеческое имя задачи (обогащается API из задач всех гейтов). Лента
  // показывает его вместо id для шагов прошлых/будущих гейтов.
  task_title?: string | null;
  selected_step_id: string | null;
  planning_outcome: string;
  validation_status: string | null;
  execution_run_id: string | null;
  started_at: string;
  finished_at: string;
  error_message: string | null;
}

export interface WorkflowRunView {
  run_id: string;
  project_id: string;
  status: WorkflowRunStatus;
  provider: string | null;
  model: string | null;
  max_steps: number;
  current_step: number;
  total_steps_completed: number;
  started_at: string;
  finished_at: string | null;
  last_step_summary: string;
  stop_reason: string | null;
  error_message: string | null;
  cancel_requested: boolean;
  steps: WorkflowStepView[];
}


// ---- Methodology pack catalog (для L2 MethodologyView) -------------------

export interface MethodologyStageProducesView {
  field: string;
  type: string;
  required: boolean;
}

export interface MethodologyStageRuleView {
  id: string;
  if: string | null;
}

export interface MethodologyStageView {
  id: string;
  title: string;
  description: string;
  produces: MethodologyStageProducesView[];
  rules: MethodologyStageRuleView[];
}

export interface MethodologyPackView {
  pack_ref: string;
  title: string;
  description: string;
  status: string;
  stage_execution_mode: string;
  stages: MethodologyStageView[];
  required_stages: string[];
  optional_stages: string[];
}


// ---- Methodology trace для L3 ReasoningPanel + L4 Provenance -------------

export interface MethodologyReasoningStageView {
  stage_id: string;
  title: string;
  outputs: Record<string, unknown>;
  _source?: Record<string, unknown> | null;
}

export interface MethodologyReasoningPayload {
  methodology_pack_ref: string;
  stages: MethodologyReasoningStageView[];
  complexity: string | null;
}

export interface MethodologyTraceRuleOutcome {
  stage_id: string;
  rule_id: string;
  fired: boolean;
  candidate_id?: string;
}

export interface MethodologyTraceCandidate {
  candidate_id: string;
  source_id: string;
  severity: string;
  blocking_scope: string;
}

export interface MethodologyTracePayload {
  methodology_pack_ref: string;
  stage_execution_mode: string;
  complexity: string | null;
  stages_executed: string[];
  stage_outputs: Record<string, Record<string, unknown>>;
  rules_evaluated: MethodologyTraceRuleOutcome[];
  candidates_emitted: MethodologyTraceCandidate[];
}

export interface MethodologyExecutionSummary {
  execution_run_id: string | null;
  provider: string | null;
  model: string | null;
  status: string | null;
  context_manifest_id: string | null;
  created_at: string | null;
}

export interface MethodologyTraceResponse {
  task_id: string;
  trace: MethodologyTracePayload | null;
  reasoning: MethodologyReasoningPayload | null;
  trace_artifact_id?: string;
  reasoning_artifact_id?: string | null;
  execution?: MethodologyExecutionSummary | null;
  message?: string;
}


// ---- Harness (агенты-исполнители) — наблюдаемость и настройки (Ф6/Ф7) ----

export interface HarnessDockerStatus {
  available: boolean;
  version?: string | null;
  error?: string | null;
  hint?: string | null;
  sdk_installed?: boolean;
}

export interface HarnessCapacityView {
  max_concurrent: number;
  cpu_count?: number;
  total_memory_mb?: number;
}

export interface HarnessPullProgress {
  image: string;
  in_progress: boolean;
  ready: boolean;
  status?: string | null;
  progress?: number | null;
  error?: string | null;
}

export interface HarnessReadinessView {
  docker: HarnessDockerStatus;
  capacity: HarnessCapacityView;
  default_image: string;
  image_ready: boolean;
  pull: HarnessPullProgress | null;
  ready: boolean;
  blockers: string[];
}

export interface HarnessSlotsView {
  capacity: number;
  in_use: number;
  waiting: number;
}

export interface HarnessBudgetView {
  runs: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface HarnessRunLimitsView {
  wall_clock_s: number | null;
  max_tokens: number | null;
  max_steps: number | null;
  max_cost_usd: number | null;
}

export interface HarnessRuntimeStatusView {
  provider_name: string;
  slots: HarnessSlotsView;
  budget: HarnessBudgetView;
  run_limits: HarnessRunLimitsView;
  budget_exceeded: string | null;
}

export interface HarnessAdapterCapability {
  title: string;
  autonomy: string;
  models: string;
  git_native: boolean;
  needs_docker: boolean;
  best_for: string;
  default_image?: string;
  default_model?: string;
}

export interface HarnessAdaptersView {
  active: string;
  capabilities: Record<string, HarnessAdapterCapability>;
}

export interface HarnessConnectionView {
  provider: string;
  image: string | null;
  model: string | null;
  command: string | null;
  default_timeout_s: number | null;
  source: string;
  updated_at: string | null;
}

export interface HarnessGateResultView {
  name: string;
  passed: boolean;
  exit_code: number;
  log: string;
}

export interface HarnessUsageView {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
}

export interface HarnessTracePayload {
  provider: string;
  model: string | null;
  output_kind: string;
  brief: string;
  transcript: string;
  gates: HarnessGateResultView[];
  usage: HarnessUsageView | null;
}

export interface HarnessTraceResponse {
  task_id: string;
  trace: HarnessTracePayload | null;
  primary_artifact_id?: string;
  message?: string;
}

export interface HarnessSelfTestView {
  ok: boolean;
  duration_ms: number;
  transcript: string;
  error: string | null;
}

export interface HarnessImageStatusView {
  image: string;
  ready: boolean;
  progress: HarnessPullProgress | null;
}

export interface HarnessPrepareView {
  status: string;
  pull: HarnessPullProgress | null;
}


// ---- L6 design extensions (P3v2 skeleton, P5 pins, P7 decisions, P8 versions) ----

export type ArtifactSectionStatus = "done" | "in_progress" | "pending" | "needs_review";

export interface ArtifactSectionView {
  section_id: string;
  title: string;
  status: ArtifactSectionStatus;
  summary: string | null;
  has_pins: boolean;
  pin_count: number;
}

export interface ArtifactSkeletonView {
  project_id: string;
  artifact_id: string;
  artifact_role: string;
  title: string;
  sections: ArtifactSectionView[];
  sections_done: number;
  sections_total: number;
  has_markdown: boolean;
  created_at: string;
}

export interface ArtifactVersionItemView {
  artifact_id: string;
  artifact_role: string;
  title: string;
  label: string;
  is_current: boolean;
  created_at: string;
  created_by_task_id: string | null;
  parent_artifact_id: string | null;
  description: string;
  /** Версия заархивирована откатом (а не просто заменена новой). */
  archived?: boolean;
}

export interface ProjectArtifactVersionsView {
  project_id: string;
  chains: ArtifactVersionItemView[][];
}

export type FailurePinKind = "candidate_open" | "assumption" | "validation_finding";
export type FailurePinSeverity = "high" | "medium" | "low";

export interface FailurePinView {
  pin_id: string;
  artifact_id: string;
  section_id: string | null;
  severity: FailurePinSeverity;
  kind: FailurePinKind;
  message: string;
  source_type: string;
  source_id: string | null;
  confidence_without_user: number | null;
  related_clarification_id: string | null;
}

export interface ProjectFailurePinsView {
  project_id: string;
  artifact_id: string | null;
  pins: FailurePinView[];
  total_count: number;
}

// --- LLM Settings ----------------------------------------------------------

export type ProviderType = "openrouter" | "anthropic" | "claude_cli";

export interface ProviderConnectionView {
  connection_id: string;
  provider_type: ProviderType;
  display_name: string;
  has_api_key: boolean;
  api_key_preview: string;
  extras: Record<string, string>;
  source: string;
  created_at: string;
  last_tested_at: string | null;
  last_test_status: "untested" | "ok" | "error";
  last_test_message: string;
}

export interface ModelRoutingView {
  routing_id: string;
  connection_id: string;
  model_name: string;
  priority: number;
  enabled: boolean;
}

export interface ModelCatalogEntry {
  model_name: string;
  routings: Array<
    ModelRoutingView & {
      connection_display_name: string;
      provider_type: ProviderType;
    }
  >;
  // Лимит контекста (токены) — бюджет секции «Контекст проекта» для модели.
  context_limit: number;
  context_limit_is_default: boolean;
}

export interface TestResultView {
  status: "ok" | "error";
  message: string;
  latency_ms: number;
  sample_response: string | null;
  tested_at: string;
}

// --- v3.0 — Decision ledger + checkpoint sessions ---------------------------
//
// Зеркалирует `domain/workspace_views.py` (DecisionItemView,
// ProjectDecisionsView, CheckpointSessionView, ProjectCheckpointsView).
// Уровни и статусы — точные строковые литералы в синхроне с backend
// `domain/decisions.py` и `domain/checkpoints.py`.

export type DecisionLevel = "business" | "architecture" | "detail";
export type DecisionStatus =
  | "proposed"
  | "accepted_default"
  | "user_overridden"
  | "deferred"
  | "locked_in"
  | "superseded";
export type DecisionSource =
  | "identification"
  | "emergent"
  | "reactive_validation"
  | "user_manual";
export type DecisionUserAction =
  | "not_shown"
  | "accepted_default"
  | "modified"
  | "deferred"
  | "pending";
// v3.1: пришёл из legacy ClarificationCandidate.answer_mode после миграции
export type DecisionAnswerMode = "single" | "multiple" | "free_text" | "confirmation";
export type CheckpointStatus = "pending" | "finalized" | "expired" | "cancelled";
export type CheckpointAnswerKind =
  | "accept_default"
  | "select_alternative"
  | "free_text"
  | "defer";

export interface DecisionAlternativeView {
  option_id: string;
  label: string;
  description: string;
  pros: string[];
  cons: string[];
  confidence: number | null;
  is_chosen: boolean;
}

export interface DecisionItemView {
  decision_id: string;
  project_id: string;
  title: string;
  description: string;
  category: string;
  level: DecisionLevel;
  raw_level: DecisionLevel;
  level_rationale: string;
  rationale: string;
  chosen_option_id: string;
  chosen_option_label: string;
  alternatives: DecisionAlternativeView[];
  confidence: number;
  is_low_confidence: boolean;
  status: DecisionStatus;
  source: DecisionSource;
  source_task_id: string | null;
  affected_artifact_ids: string[];
  depends_on_decision_ids: string[];
  user_action: DecisionUserAction;
  was_user_modified: boolean;
  user_free_text_answer: string | null;
  created_at: string;
  updated_at: string;
  // v3.1: режим ответа + multi-select поддержка
  answer_mode: DecisionAnswerMode;
  chosen_option_ids: string[];
  // v3.4: пользователь явно «верифицировал» рискованное решение
  user_verified: boolean;
  user_verified_at: string | null;
  // false means list endpoint returned a compact item; load detail for
  // alternatives/rationale/description.
  details_included: boolean;
}

export interface ProjectDecisionsView {
  project_id: string;
  mode: string;
  // На уровне режима пользователя — сколько всего и сколько ждут реакции
  surfaced_total: number;
  surfaced_pending: number;
  // По уровням (всегда все три)
  business_count: number;
  architecture_count: number;
  detail_count: number;
  // По статусам
  proposed_count: number;
  accepted_count: number;
  overridden_count: number;
  low_confidence_count: number;
  items: DecisionItemView[];
}

export interface CheckpointSessionView {
  session_id: string;
  project_id: string;
  task_id: string;
  task_title: string;
  artifact_role: string;
  status: CheckpointStatus;
  created_at: string;
  finalized_at: string | null;
  finalized_by: string | null;
  decisions: DecisionItemView[];
}

export interface ProjectCheckpointsView {
  project_id: string;
  pending_count: number;
  items: CheckpointSessionView[];
}

export interface CheckpointAnswerPayload {
  decision_id: string;
  kind: CheckpointAnswerKind;
  selected_option_id?: string | null;
  // v3.1: multi-select. Если задано — используется (selected_option_id игнор)
  selected_option_ids?: string[] | null;
  free_text?: string | null;
}
