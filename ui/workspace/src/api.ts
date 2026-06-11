import type {
  ArtifactDetailView,
  ArtifactSummaryView,
  AttachmentView,
  CommandResultView,
  DomainPackCatalogItemView,
  HealthView,
  MethodologyPackView,
  MethodologyTraceResponse,
  WorkflowRunView,
  ObjectiveCatalogItemView,
  ProjectCreatedView,
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
    // true → отложить авто-подбор пакетов и разворот графа до finalize-setup
    // (после загрузки вложений), чтобы подбор увидел и запрос, и файлы.
    defer_setup?: boolean;
  }) =>
    request<ProjectCreatedView>("/api/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  // Завершить отложенный setup: подбор пакетов по запросу + вложениям и
  // разворот графа. Вызывается после загрузки входных файлов.
  finalizeProjectSetup: (projectId: string) =>
    request<ProjectCreatedView>(`/api/projects/${projectId}/finalize-setup`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  deleteProject: (projectId: string) =>
    request<{ status: string; project_id: string }>(`/api/projects/${projectId}`, {
      method: "DELETE",
    }),
  listObjectives: () => request<ObjectiveCatalogItemView[]>("/api/registry/objectives"),
  listDomainPacks: () => request<DomainPackCatalogItemView[]>("/api/registry/domain-packs"),
  listMethodologyPacks: () => request<MethodologyPackView[]>("/api/registry/methodology-packs"),
  getShell: (projectId: string) => request<ProjectShellView>(`/api/projects/${projectId}/shell`),
  getTaskGraph: (projectId: string) => request<ProjectTaskGraphView>(`/api/projects/${projectId}/task-graph`),
  // Ф1: граф задач конкретного гейта (objective). ref содержит '@' — кодируем.
  getObjectiveTaskGraph: (projectId: string, objectiveRef: string) =>
    request<ProjectTaskGraphView>(
      `/api/projects/${projectId}/objectives/task-graph?ref=${encodeURIComponent(objectiveRef)}`,
    ),
  // Гейт задачи — для дип-линка «открыть задачу на графе» (выбрать подвкладку).
  getTaskGate: (projectId: string, taskId: string) =>
    request<{ objective_ref: string }>(`/api/projects/${projectId}/tasks/${taskId}/gate`),
  getSituation: (projectId: string) => request<ProjectSituationView>(`/api/projects/${projectId}/situation`),
  getTimeline: (projectId: string) => request<ProjectTimelineView>(`/api/projects/${projectId}/timeline`),
  getArtifacts: (projectId: string) => request<ArtifactSummaryView[]>(`/api/projects/${projectId}/artifacts`),
  // Архив проекта: артефакты, заархивированные откатом + заменённые новой версией.
  getArchivedArtifacts: (projectId: string) =>
    request<ArtifactSummaryView[]>(`/api/projects/${projectId}/artifacts/archive`),
  getArtifactDetail: (projectId: string, artifactId: string) =>
    request<ArtifactDetailView>(`/api/projects/${projectId}/artifacts/${artifactId}`),
  // #2: содержимое одного файла бандла (кода) для просмотра в окне артефакта.
  getBundleFile: (projectId: string, artifactId: string, path: string) =>
    request<{ path: string; binary: boolean; text: string; size_bytes: number; truncated?: boolean }>(
      `/api/projects/${projectId}/artifacts/${artifactId}/bundle/file?path=${encodeURIComponent(path)}`,
    ),
  artifactPdfUrl: (projectId: string, artifactId: string) =>
    `/api/projects/${projectId}/artifacts/${artifactId}/download.pdf`,
  artifactMdUrl: (projectId: string, artifactId: string) =>
    `/api/projects/${projectId}/artifacts/${artifactId}/download.md`,
  projectExportZipUrl: (projectId: string) => `/api/projects/${projectId}/export.zip`,
  // --- attachments (входные файлы) --------------------------------------
  getAttachments: (projectId: string) =>
    request<AttachmentView[]>(`/api/projects/${projectId}/attachments`),
  uploadAttachment: async (
    projectId: string,
    file: File,
    purpose: "input" | "requisite" = "input",
    sync = false,
  ): Promise<{ attachment_id: string; original_filename: string; extraction_status: string }> => {
    const form = new FormData();
    form.append("file", file);
    form.append("purpose", purpose);
    // sync=true → backend извлечёт текст синхронно (нужно при создании проекта,
    // чтобы подбор пакетов в finalize-setup увидел текст файлов).
    if (sync) form.append("sync", "true");
    // FormData задаёт multipart boundary сам — Content-Type не выставляем.
    const response = await fetch(`${API_BASE}/api/projects/${projectId}/attachments`, {
      method: "POST",
      body: form,
    });
    if (!response.ok) {
      throw new Error((await response.text()) || `HTTP ${response.status}`);
    }
    return response.json();
  },
  deleteAttachment: (projectId: string, attachmentId: string) =>
    request<{ status: string; attachment_id: string }>(
      `/api/projects/${projectId}/attachments/${attachmentId}`,
      { method: "DELETE" },
    ),
  attachmentDownloadUrl: (projectId: string, attachmentId: string) =>
    `/api/projects/${projectId}/attachments/${attachmentId}/download`,
  // Онлайн-просмотр оригинала (inline-disposition: PDF рендерится в iframe).
  attachmentViewUrl: (projectId: string, attachmentId: string) =>
    `/api/projects/${projectId}/attachments/${attachmentId}/download?inline=1`,
  // Извлечённый текст вложения (для форматов без браузерного рендера, напр. .docx).
  getAttachmentText: (projectId: string, attachmentId: string) =>
    request<{ attachment_id: string; extraction_status: string; text: string }>(
      `/api/projects/${projectId}/attachments/${attachmentId}/text`,
    ),
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
  // Повтор задачи теперь идёт через runner (асинхронно): endpoint возвращает
  // запись прогона (как run-until-blocked), прогресс — через workflow-runs/active.
  retryTask: (projectId: string, taskId: string, provider: string, model: string) =>
    request<WorkflowRunView>(`/api/projects/${projectId}/commands/retry-task`, {
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
  setClarificationMode: (projectId: string, mode: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/set-clarification-mode`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  setMethodology: (projectId: string, packRef: string) =>
    request<CommandResultView>(`/api/projects/${projectId}/commands/set-methodology`, {
      method: "POST",
      body: JSON.stringify({ pack_ref: packRef }),
    }),
  activateNextObjective: (projectId: string, objectiveRef: string) =>
    request<CommandResultView>(
      `/api/projects/${projectId}/commands/activate-next-objective`,
      {
        method: "POST",
        body: JSON.stringify({ objective_ref: objectiveRef }),
      },
    ),
  // --- Ролбек шага -------------------------------------------------------
  // Превью/история — чистое чтение (не блокируются замком отката).
  getRollbackPreview: (projectId: string, targetTaskId: string) =>
    request<import("./types").RollbackPreviewView>(
      `/api/projects/${projectId}/rollback/preview?target_task_id=${encodeURIComponent(targetTaskId)}`,
    ),
  getRollbackHistory: (projectId: string) =>
    request<import("./types").ProjectRollbackHistoryView>(
      `/api/projects/${projectId}/rollback/history`,
    ),
  // Команда: координатор берёт замок, гасит активный прогон, откатывает.
  rollbackStep: (projectId: string, targetTaskId: string, reason?: string) =>
    request<import("./types").RollbackResultView>(
      `/api/projects/${projectId}/commands/rollback`,
      {
        method: "POST",
        body: JSON.stringify({ target_task_id: targetTaskId, reason: reason || undefined }),
      },
    ),
  getOverview: (projectId: string) =>
    request<import("./types").ProjectOverviewView>(`/api/projects/${projectId}/overview`),
  getStages: (projectId: string) =>
    request<import("./types").ProjectStagesView>(`/api/projects/${projectId}/stages`),
  getRequisites: (projectId: string) =>
    request<import("./types").ProjectRequisitesView>(`/api/projects/${projectId}/requisites`),
  getCapabilityGaps: (projectId: string) =>
    request<import("./types").ProjectGapsView>(`/api/projects/${projectId}/capability-gaps`),
  // Реквизиты v2: структурное разрешение реквизита — данные (value/file/
  // reference) ИЛИ обход (assumption/deferred/not_applicable).
  provideRequisite: (
    projectId: string,
    payload: {
      key: string;
      mode?: string;
      value?: string;
      attachment_id?: string;
      note?: string;
    },
  ) =>
    request<import("./types").ProjectRequisitesView>(
      `/api/projects/${projectId}/requisites/provide`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  // Снять предоставление реквизита (un-provide): данные перестают втекать,
  // блокирующий реквизит снова держит задачу-потребителя.
  unprovideRequisite: (projectId: string, key: string) =>
    request<import("./types").ProjectRequisitesView>(
      `/api/projects/${projectId}/requisites/unprovide`,
      { method: "POST", body: JSON.stringify({ key }) },
    ),
  getMethodologyTrace: (projectId: string, taskId: string) =>
    request<MethodologyTraceResponse>(`/api/projects/${projectId}/tasks/${taskId}/methodology-trace`),
  // --- Harness (агенты-исполнители): наблюдаемость + онбординг + настройки ---
  getHarnessStatus: () =>
    request<import("./types").HarnessReadinessView>("/api/harness/status"),
  getHarnessRuntime: () =>
    request<import("./types").HarnessRuntimeStatusView>("/api/harness/runtime"),
  getHarnessAdapters: () =>
    request<import("./types").HarnessAdaptersView>("/api/harness/adapters"),
  // Связка LLM↔агент: какое LLM-подключение проекта использует агент.
  getHarnessLlm: () =>
    request<{
      configured: boolean;
      provider: string | null;
      provider_type: string | null;
      model: string | null;
    }>("/api/harness/llm"),
  getHarnessConnection: () =>
    request<import("./types").HarnessConnectionView>("/api/harness/connection"),
  setHarnessConnection: (payload: {
    provider: string;
    image?: string | null;
    model?: string | null;
    command?: string | null;
    default_timeout_s?: number | null;
    engine?: "docker" | "host";
    host_security?: "restricted" | "full";
    network?: "none" | "online";
  }) =>
    request<import("./types").HarnessConnectionView>("/api/harness/connection", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  prepareHarnessImage: (image?: string) =>
    request<import("./types").HarnessPrepareView>("/api/harness/prepare", {
      method: "POST",
      body: JSON.stringify(image ? { image } : {}),
    }),
  getHarnessImageProgress: (image: string) =>
    request<import("./types").HarnessPullProgress | null>(
      `/api/harness/image-progress?image=${encodeURIComponent(image)}`,
    ),
  getHarnessImageStatus: (image: string) =>
    request<import("./types").HarnessImageStatusView>(
      `/api/harness/image-status?image=${encodeURIComponent(image)}`,
    ),
  harnessSelfTest: (image?: string) =>
    request<import("./types").HarnessSelfTestView>("/api/harness/self-test", {
      method: "POST",
      body: JSON.stringify(image ? { image } : {}),
    }),
  getHarnessTrace: (projectId: string, taskId: string) =>
    request<import("./types").HarnessTraceResponse>(
      `/api/projects/${projectId}/tasks/${taskId}/harness-trace`,
    ),
  // L6 design extensions
  getArtifactSkeleton: (projectId: string, artifactId: string) =>
    request<import("./types").ArtifactSkeletonView>(
      `/api/projects/${projectId}/artifacts/${artifactId}/skeleton`,
    ),
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

  // --- v3.0 — Decision ledger + checkpoint sessions ----------------------
  getDecisionsRegistry: (
    projectId: string,
    filters?: { level?: string; status?: string; includeDetails?: boolean },
  ) => {
    const qs = new URLSearchParams();
    if (filters?.level) qs.set("level", filters.level);
    if (filters?.status) qs.set("status", filters.status);
    if (filters?.includeDetails === false) qs.set("include_details", "false");
    const qstr = qs.toString() ? `?${qs.toString()}` : "";
    return request<import("./types").ProjectDecisionsView>(
      `/api/projects/${projectId}/decisions${qstr}`,
    );
  },
  getDecisionDetail: (projectId: string, decisionId: string) =>
    request<import("./types").DecisionItemView>(
      `/api/projects/${projectId}/decisions/${decisionId}`,
    ),
  verifyDecision: (
    projectId: string,
    decisionId: string,
    verified: boolean = true,
  ) =>
    request<import("./types").DecisionItemView>(
      `/api/projects/${projectId}/decisions/${decisionId}/verify`,
      {
        method: "POST",
        body: JSON.stringify({ verified }),
      },
    ),
  verifyArtifact: (projectId: string, artifactId: string, verified: boolean = true) =>
    request<ArtifactDetailView>(
      `/api/projects/${projectId}/artifacts/${artifactId}/verify`,
      {
        method: "POST",
        body: JSON.stringify({ verified }),
      },
    ),
  // Ф3: согласование итогового артефакта с заказчиком (тумблер sign-off).
  signOffArtifact: (projectId: string, artifactId: string, signedOff: boolean = true) =>
    request<ArtifactDetailView>(
      `/api/projects/${projectId}/artifacts/${artifactId}/sign-off`,
      {
        method: "POST",
        body: JSON.stringify({ signed_off: signedOff }),
      },
    ),
  getDecisionsForArtifact: (projectId: string, artifactId: string) =>
    request<import("./types").DecisionItemView[]>(
      `/api/projects/${projectId}/artifacts/${artifactId}/decisions`,
    ),
  getCheckpoints: (projectId: string) =>
    request<import("./types").ProjectCheckpointsView>(
      `/api/projects/${projectId}/checkpoints`,
    ),
  getCheckpointDetail: (projectId: string, sessionId: string) =>
    request<import("./types").CheckpointSessionView>(
      `/api/projects/${projectId}/checkpoints/${sessionId}`,
    ),
  submitCheckpointAnswers: (
    projectId: string,
    sessionId: string,
    answers: import("./types").CheckpointAnswerPayload[],
  ) =>
    request<import("./types").CheckpointSessionView>(
      `/api/projects/${projectId}/checkpoints/${sessionId}/answer`,
      {
        method: "POST",
        body: JSON.stringify({ answers }),
      },
    ),
  // Единый bulk-ответ на ВСЕ открытые решения проекта (поверх сессий).
  // Используется единым экраном открытых решений в параллельном режиме.
  answerDecisions: (
    projectId: string,
    answers: import("./types").CheckpointAnswerPayload[],
  ) =>
    request<import("./types").ProjectDecisionsView>(
      `/api/projects/${projectId}/decisions/answer`,
      {
        method: "POST",
        body: JSON.stringify({ answers }),
      },
    ),

  // --- Settings: LLM providers / models / assignments --------------------
  listPurposes: () => request<{ id: string; label: string }[]>("/api/settings/purposes"),
  // Общие настройки приложения (раздел «Общие»). debug → открывает в окне
  // артефакта поля Проверки/Provenance/JSON/Контекст.
  getAppSettings: () => request<{ debug: boolean }>("/api/settings/app"),
  setAppSettings: (payload: { debug?: boolean }) =>
    request<{ debug: boolean }>("/api/settings/app", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
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
  // Лимит контекста модели. tokens=null → сброс к дефолту.
  setModelContextLimit: (modelName: string, tokens: number | null) =>
    request<import("./types").ModelCatalogEntry>("/api/settings/models/context-limit", {
      method: "PUT",
      body: JSON.stringify({ model_name: modelName, context_limit_tokens: tokens }),
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
