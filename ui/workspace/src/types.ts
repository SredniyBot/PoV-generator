export type ProjectionName =
  | "shell"
  | "task_graph"
  | "situation"
  | "timeline"
  | "artifacts"
  | "clarifications"
  | "review"
  | "state"
  | "debug"
  | "overview"
  | "methodology";

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
  origin_kind: string;
  origin_ref: string;
  slot_id: string | null;
  depth: number;
  retryable: boolean;
  is_current: boolean;
  blocking_clarification_count: number;
  children: TaskNodeView[];
}

export interface ProjectTaskGraphView {
  project_id: string;
  objective_ref: string;
  current_task_id: string | null;
  completed_leaf_tasks: number;
  total_leaf_tasks: number;
  nodes: TaskNodeView[];
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

export interface ClarificationOptionView {
  option_id: string;
  label: string;
  description: string;
  effect_preview: string;
  confidence: number | null;
}

export interface ClarificationItemView {
  clarification_id: string;
  status: string;
  priority: string;
  title: string;
  question: string;
  description: string;
  reason: string;
  impact: string;
  answer_mode: string;
  options: ClarificationOptionView[];
  recommended_option_id: string | null;
  min_participation_mode: string;
  default_assumption: string | null;
  blocking_scope: string;
  decision_owner_role: string;
  auto_resolved: boolean;
  affected_task_ids: string[];
  related_artifact_ids: string[];
  selected_option_ids: string[];
  free_text: string | null;
  resolution_summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectClarificationsView {
  project_id: string;
  mode: string;
  open_count: number;
  answered_count: number;
  assumed_count: number;
  blocking_count: number;
  items: ClarificationItemView[];
}

export interface ArtifactSummaryView {
  artifact_id: string;
  artifact_role: string;
  title: string;
  created_at: string;
  created_by_task_id: string | null;
  has_markdown: boolean;
}

export interface ArtifactValidationView {
  validation_run_id: string;
  status: string;
  finding_messages: string[];
  created_at: string;
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
