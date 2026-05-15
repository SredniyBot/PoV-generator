import type {
  ArtifactDetailView,
  ArtifactSummaryView,
  ClarificationItemView,
  CommandResultView,
  DomainPackCatalogItemView,
  HealthView,
  MethodologyPackView,
  MethodologyTraceResponse,
  WorkflowRunView,
  ObjectiveCatalogItemView,
  ProjectCreatedView,
  ProjectClarificationsView,
  ProjectDebugView,
  ProjectListItemView,
  ProjectReviewView,
  ProjectShellView,
  ProjectSituationView,
  ProjectStateView,
  ProjectTaskGraphView,
  ProjectTimelineView,
  ProjectionName,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<HealthView>("/api/health"),
  listProjects: () => request<ProjectListItemView[]>("/api/projects"),
  createProject: (payload: {
    name: string;
    objective_ref: string;
    request_text: string;
    domain_pack_refs: string[];
  }) =>
    request<ProjectCreatedView>("/api/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  listObjectives: () => request<ObjectiveCatalogItemView[]>("/api/registry/objectives"),
  listDomainPacks: () => request<DomainPackCatalogItemView[]>("/api/registry/domain-packs"),
  listMethodologyPacks: () => request<MethodologyPackView[]>("/api/registry/methodology-packs"),
  getShell: (projectId: string) => request<ProjectShellView>(`/api/projects/${projectId}/shell`),
  getTaskGraph: (projectId: string) => request<ProjectTaskGraphView>(`/api/projects/${projectId}/task-graph`),
  getSituation: (projectId: string) => request<ProjectSituationView>(`/api/projects/${projectId}/situation`),
  getTimeline: (projectId: string) => request<ProjectTimelineView>(`/api/projects/${projectId}/timeline`),
  getClarifications: (projectId: string) => request<ProjectClarificationsView>(`/api/projects/${projectId}/clarifications`),
  getClarificationDetail: (projectId: string, clarificationId: string) =>
    request<ClarificationItemView>(`/api/projects/${projectId}/clarifications/${clarificationId}`),
  getArtifacts: (projectId: string) => request<ArtifactSummaryView[]>(`/api/projects/${projectId}/artifacts`),
  getArtifactDetail: (projectId: string, artifactId: string) =>
    request<ArtifactDetailView>(`/api/projects/${projectId}/artifacts/${artifactId}`),
  artifactPdfUrl: (projectId: string, artifactId: string) =>
    `/api/projects/${projectId}/artifacts/${artifactId}/download.pdf`,
  getReview: (projectId: string) => request<ProjectReviewView>(`/api/projects/${projectId}/review`),
  getState: (projectId: string) => request<ProjectStateView>(`/api/projects/${projectId}/state`),
  getDebug: (projectId: string) => request<ProjectDebugView>(`/api/projects/${projectId}/debug`),
  runNext: (projectId: string, provider: string, model: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/run-next`, {
      method: "POST",
      // Пустые значения не отправляем — backend в этом случае идёт через
      // resolve_for_purpose из settings-store (а не legacy env-path).
      body: JSON.stringify({ provider: provider || undefined, model: model || undefined }),
    }),
  runUntilBlocked: (projectId: string, provider: string, model: string, maxSteps = 1000) =>
    // W4.1 (R1): endpoint асинхронный, возвращает WorkflowRunView (status=pending)
    // сразу. Прогресс читается через getActiveWorkflowRun.
    //
    // maxSteps=1000 — эффективно «без лимита» (sanity ceiling против бесконечной
    // петли планировщика). Раньше дефолт был 3, что в реальной работе превращалось
    // в «нажми Run 5+ раз чтобы пройти весь pipeline». Реальный проект — 15-25
    // leaf-задач × до 2-3 ретраев = ~50-75 шагов максимум.
    request<WorkflowRunView>(`/api/projects/${projectId}/commands/run-until-blocked`, {
      method: "POST",
      body: JSON.stringify({
        provider: provider || undefined,
        model: model || undefined,
        max_steps: maxSteps,
      }),
    }),
  cancelWorkflow: (projectId: string, runId: string) =>
    request<{ status: string; run_id: string }>(`/api/projects/${projectId}/commands/cancel-workflow`, {
      method: "POST",
      body: JSON.stringify({ run_id: runId }),
    }),
  getActiveWorkflowRun: (projectId: string) =>
    request<WorkflowRunView | null>(`/api/projects/${projectId}/workflow-runs/active`),
  getWorkflowRun: (projectId: string, runId: string) =>
    request<WorkflowRunView>(`/api/projects/${projectId}/workflow-runs/${runId}`),
  listWorkflowRuns: (projectId: string, limit = 20) =>
    request<WorkflowRunView[]>(`/api/projects/${projectId}/workflow-runs?limit=${limit}`),
  retryTask: (projectId: string, taskId: string, provider: string, model: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/retry-task`, {
      method: "POST",
      body: JSON.stringify({
        task_id: taskId,
        provider: provider || undefined,
        model: model || undefined,
      }),
    }),
  setGoal: (projectId: string, text: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/set-goal`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  closeGap: (projectId: string, gapId: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/close-gap`, {
      method: "POST",
      body: JSON.stringify({ gap_id: gapId }),
    }),
  setReadiness: (
    projectId: string,
    payload: { dimension: string; status: string; blocking: boolean; confidence: number },
  ) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/set-readiness`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  enableDomainPack: (projectId: string, packRef: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/enable-domain-pack`, {
      method: "POST",
      body: JSON.stringify({ pack_ref: packRef }),
    }),
  answerClarification: (
    projectId: string,
    payload: { clarification_id: string; selected_option_ids: string[]; free_text?: string },
  ) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/answer-clarification`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  acceptAssumption: (projectId: string, clarificationId: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/accept-assumption`, {
      method: "POST",
      body: JSON.stringify({ clarification_id: clarificationId }),
    }),
  setClarificationMode: (projectId: string, mode: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/set-clarification-mode`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  // W5.1: clarification flow operations + audit events
  deferClarification: (projectId: string, clarificationId: string, reason?: string) =>
    request<ClarificationItemView>(`/api/projects/${projectId}/commands/defer-clarification`, {
      method: "POST",
      body: JSON.stringify({ clarification_id: clarificationId, reason }),
    }),
  reopenClarification: (projectId: string, clarificationId: string) =>
    request<ClarificationItemView>(`/api/projects/${projectId}/commands/reopen-clarification`, {
      method: "POST",
      body: JSON.stringify({ clarification_id: clarificationId }),
    }),
  getClarificationEvents: (projectId: string, clarificationId: string) =>
    request<Array<{
      event_id: string;
      request_id: string;
      project_id: string;
      event_type: string;
      payload: Record<string, unknown>;
      actor: string;
      created_at: string;
    }>>(`/api/projects/${projectId}/clarifications/${clarificationId}/events`),
  getNextOpenClarification: (projectId: string, afterId?: string) =>
    request<ClarificationItemView | null>(
      `/api/projects/${projectId}/clarifications/next${afterId ? `?after_id=${encodeURIComponent(afterId)}` : ""}`,
    ),
  setMethodology: (projectId: string, packRef: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/set-methodology`, {
      method: "POST",
      body: JSON.stringify({ pack_ref: packRef }),
    }),
  getOverview: (projectId: string) =>
    request<import("./types").ProjectOverviewView>(`/api/projects/${projectId}/overview`),
  getMethodologyTrace: (projectId: string, taskId: string) =>
    request<MethodologyTraceResponse>(`/api/projects/${projectId}/tasks/${taskId}/methodology-trace`),
  // L6 design extensions
  getArtifactSkeleton: (projectId: string, artifactId: string) =>
    request<import("./types").ArtifactSkeletonView>(
      `/api/projects/${projectId}/artifacts/${artifactId}/skeleton`,
    ),
  getDecisionLog: (projectId: string) =>
    request<import("./types").ProjectDecisionLogView>(`/api/projects/${projectId}/decisions`),
  getArtifactVersions: (projectId: string) =>
    request<import("./types").ProjectArtifactVersionsView>(
      `/api/projects/${projectId}/artifact-versions`,
    ),
  getFailurePins: (projectId: string, artifactId?: string) => {
    const qs = artifactId ? `?artifact_id=${encodeURIComponent(artifactId)}` : "";
    return request<import("./types").ProjectFailurePinsView>(
      `/api/projects/${projectId}/failure-pins${qs}`,
    );
  },

  // --- Settings: LLM providers / models / assignments --------------------
  listPurposes: () => request<{ id: string; label: string }[]>("/api/settings/purposes"),
  listProviders: () => request<import("./types").ProviderConnectionView[]>("/api/settings/providers"),
  createProvider: (payload: {
    provider_type: "openrouter" | "anthropic" | "claude_cli";
    display_name: string;
    api_key?: string;
    extras?: Record<string, string>;
  }) =>
    request<import("./types").ProviderConnectionView>("/api/settings/providers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateProvider: (
    connectionId: string,
    payload: { display_name?: string; api_key?: string; extras?: Record<string, string> },
  ) =>
    request<import("./types").ProviderConnectionView>(
      `/api/settings/providers/${connectionId}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),
  deleteProvider: (connectionId: string) =>
    request<{ status: string }>(`/api/settings/providers/${connectionId}`, {
      method: "DELETE",
    }),
  testProvider: (connectionId: string, model?: string) =>
    request<import("./types").TestResultView>(`/api/settings/providers/${connectionId}/test`, {
      method: "POST",
      body: JSON.stringify(model ? { model } : {}),
    }),
  syncKnownModels: (connectionId: string) =>
    request<{ connection_id: string; added_count: number; added_models: string[] }>(
      `/api/settings/providers/${connectionId}/sync-models`,
      { method: "POST", body: "{}" },
    ),

  listModels: () => request<import("./types").ModelCatalogEntry[]>("/api/settings/models"),
  addCustomModel: (payload: { connection_id: string; model_name: string; priority?: number }) =>
    request<import("./types").ModelRoutingView>("/api/settings/models", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  testModel: (modelName: string) =>
    request<import("./types").TestResultView>(
      `/api/settings/models/${encodeURIComponent(modelName)}/test`,
      { method: "POST", body: "{}" },
    ),
  updateRouting: (routingId: string, payload: { priority?: number; enabled?: boolean }) =>
    request<import("./types").ModelRoutingView>(`/api/settings/routings/${routingId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  deleteRouting: (routingId: string) =>
    request<{ status: string }>(`/api/settings/routings/${routingId}`, {
      method: "DELETE",
    }),

  listAssignments: () =>
    request<{ purpose: string; model_name: string }[]>("/api/settings/assignments"),
  setAssignment: (purpose: string, modelName: string) =>
    request<{ purpose: string; model_name: string }>("/api/settings/assignments", {
      method: "PUT",
      body: JSON.stringify({ purpose, model_name: modelName }),
    }),
  resetAssignmentsToRecommended: () =>
    request<{ purpose: string; model_name: string }[]>(
      "/api/settings/assignments/reset-to-recommended",
      { method: "POST" },
    ),
  // Diagnostics: для каждого purpose показать, что реально будет
  // использовано при следующем LLM-вызове. Подтверждение того, что
  // переключение модели в UI действительно работает.
  getSettingsDiagnostics: () =>
    request<
      Array<{
        purpose: string;
        label: string;
        model_name: string | null;
        resolved: null | {
          provider_type: "openrouter" | "anthropic" | "claude_cli";
          connection_id: string;
          connection_display_name: string;
          model_name: string;
          fallback_routings: Array<{
            connection_display_name: string;
            provider_type: "openrouter" | "anthropic" | "claude_cli";
          }>;
        };
        error: string | null;
      }>
    >("/api/settings/diagnostics"),
};

export function createProjectSocket(projectId: string, projections?: ProjectionName[]): WebSocket {
  const query = projections && projections.length > 0
    ? `?projections=${encodeURIComponent(projections.join(","))}`
    : "";
  const explicitBase = import.meta.env.VITE_WS_BASE_URL as string | undefined;
  if (explicitBase) {
    return new WebSocket(`${explicitBase}/ws/projects/${projectId}${query}`);
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${protocol}//${window.location.host}/ws/projects/${projectId}${query}`);
}
