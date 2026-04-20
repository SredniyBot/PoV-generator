export type ProjectionName =
  | "shell"
  | "journey"
  | "situation"
  | "timeline"
  | "artifacts"
  | "review"
  | "state"
  | "debug";

export interface ProjectListItemView {
  project_id: string;
  name: string;
  status_label: string;
  updated_at: string;
  has_blockers: boolean;
  current_step_title: string | null;
}

export interface RecipeCatalogItemView {
  recipe_ref: string;
  name: string;
  domain: string;
  stage_gate: string;
  step_count: number;
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
  recipe_ref: string;
  domain_pack_refs: string[];
  workspace_path: string;
  changed_projections: ProjectionName[];
}

export interface ProjectShellView {
  project_id: string;
  name: string;
  business_request: string;
  recipe_ref: string;
  enabled_domain_packs: string[];
  goal: string | null;
  status_label: string;
  updated_at: string;
}

export interface JourneyStepView {
  step_id: string;
  title: string;
  template_ref: string;
  source_kind: string;
  source_ref: string;
  status: string;
  status_summary: string | null;
  latest_task_id: string | null;
  retryable: boolean;
  required: boolean;
  is_current: boolean;
}

export interface ProjectJourneyView {
  project_id: string;
  recipe_ref: string;
  domain_pack_refs: string[];
  recipe_fragment_refs: string[];
  current_step_id: string | null;
  completed_steps: number;
  total_steps: number;
  steps: JourneyStepView[];
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
  readiness: Record<string, unknown>[];
  known_facts: Record<string, unknown>[];
  enabled_domain_packs: Record<string, unknown>[];
  recipe_composition: Record<string, unknown> | null;
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
