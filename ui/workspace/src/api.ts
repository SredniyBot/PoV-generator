import type {
  ArtifactDetailView,
  ArtifactSummaryView,
  CommandResultView,
  DomainPackCatalogItemView,
  HealthView,
  ProjectCreatedView,
  ProjectDebugView,
  ProjectJourneyView,
  ProjectListItemView,
  ProjectReviewView,
  ProjectShellView,
  ProjectSituationView,
  ProjectStateView,
  ProjectTimelineView,
  ProjectionName,
  RecipeCatalogItemView,
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
    recipe_ref: string;
    request_text: string;
    domain_pack_refs: string[];
  }) =>
    request<ProjectCreatedView>("/api/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  listRecipes: () => request<RecipeCatalogItemView[]>("/api/registry/recipes"),
  listDomainPacks: () => request<DomainPackCatalogItemView[]>("/api/registry/domain-packs"),
  getShell: (projectId: string) => request<ProjectShellView>(`/api/projects/${projectId}/shell`),
  getJourney: (projectId: string) => request<ProjectJourneyView>(`/api/projects/${projectId}/journey`),
  getSituation: (projectId: string) => request<ProjectSituationView>(`/api/projects/${projectId}/situation`),
  getTimeline: (projectId: string) => request<ProjectTimelineView>(`/api/projects/${projectId}/timeline`),
  getArtifacts: (projectId: string) => request<ArtifactSummaryView[]>(`/api/projects/${projectId}/artifacts`),
  getArtifactDetail: (projectId: string, artifactId: string) =>
    request<ArtifactDetailView>(`/api/projects/${projectId}/artifacts/${artifactId}`),
  getReview: (projectId: string) => request<ProjectReviewView>(`/api/projects/${projectId}/review`),
  getState: (projectId: string) => request<ProjectStateView>(`/api/projects/${projectId}/state`),
  getDebug: (projectId: string) => request<ProjectDebugView>(`/api/projects/${projectId}/debug`),
  runNext: (projectId: string, provider: string, model: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/run-next`, {
      method: "POST",
      body: JSON.stringify({ provider, model: model || undefined }),
    }),
  runUntilBlocked: (projectId: string, provider: string, model: string, maxSteps = 20) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/run-until-blocked`, {
      method: "POST",
      body: JSON.stringify({ provider, model: model || undefined, max_steps: maxSteps }),
    }),
  retryTask: (projectId: string, taskId: string, provider: string, model: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/retry-task`, {
      method: "POST",
      body: JSON.stringify({ task_id: taskId, provider, model: model || undefined }),
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
