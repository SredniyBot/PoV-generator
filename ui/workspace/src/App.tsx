import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ExternalLink,
  FileJson2,
  Layers3,
  MessageSquareWarning,
  PencilLine,
  Plus,
  RefreshCcw,
  ShieldAlert,
  Sparkles,
  TerminalSquare,
  Waypoints,
  XCircle,
} from "lucide-react";
import { marked } from "marked";

import { api } from "./api";
import { DecisionLogPage } from "./DecisionLogPage";
import { ProjectOverviewV2 } from "./ProjectOverviewV2";
import { ProjectsHomeDashboard } from "./ProjectsHomeDashboard";
import { SettingsPage } from "./SettingsPage";
import { TaskGraphCanvas } from "./TaskGraphCanvas";
import type {
  ActionDescriptor,
  ArtifactDetailView,
  ArtifactSummaryView,
  ClarificationItemView,
  CommandResultView,
  DomainPackCatalogItemView,
  ObjectiveCatalogItemView,
  ProjectCreatedView,
  ProjectClarificationsView,
  ProjectDebugView,
  ProjectReviewView,
  ProjectOverviewView,
  ProjectShellView,
  ProjectSituationView,
  ProjectStateView,
  ProjectTaskGraphView,
  ProjectTimelineView,
  ProjectionName,
  TaskNodeView,
  TimelineEntryView,
} from "./types";
import { useProjectRealtime } from "./useProjectRealtime";
import {
  ArtifactRail,
  Button,
  CommandBar,
  Drawer,
  EmptyState,
  LoadingPanel,
  Modal,
  ProjectRail,
  SectionCard,
  SituationPanel,
  StatusPill,
  TimelineFeed,
  WorkspaceHeader,
  WorkspaceTabs,
  TaskGraphTree,
  cx,
  formatDateTime,
  prettyLabel,
} from "./ui";

const REALTIME_PROJECTIONS: ProjectionName[] = [
  "shell",
  "task_graph",
  "situation",
  "timeline",
  "artifacts",
  "clarifications",
  "review",
  "state",
  // C6: aggregated L1 / L2 projections — when these fire, MissionControl
  // and MethodologyPage queries get invalidated automatically.
  "overview",
  "methodology",
];

type ToastTone = "success" | "warning" | "danger";

interface ToastItem {
  id: string;
  tone: ToastTone;
  title: string;
  description: string;
}

interface WorkspaceActionApi {
  runNext: () => void;
  runUntilBlocked: () => void;
  retryTask: (taskId: string) => void;
  setGoal: (text: string) => void;
  closeGap: (gapId: string) => void;
  setReadiness: (payload: { dimension: string; status: string; blocking: boolean; confidence: number }) => void;
  enableDomainPack: (packRef: string) => void;
  answerClarification: (payload: { clarification_id: string; selected_option_ids: string[]; free_text?: string }) => void;
  acceptAssumption: (clarificationId: string) => void;
  setClarificationMode: (mode: string) => void;
  busy: boolean;
}

function useStoredState(key: string, initialValue: string): [string, (value: string) => void] {
  const [value, setValue] = useState<string>(() => window.localStorage.getItem(key) ?? initialValue);
  useEffect(() => {
    window.localStorage.setItem(key, value);
  }, [key, value]);
  return [value, setValue];
}

function projectionKey(projectId: string, projection: ProjectionName): readonly unknown[] {
  return [projectId, projection] as const;
}

function toneForSemanticStatus(
  status: string | null | undefined,
): "neutral" | "active" | "success" | "warning" | "danger" | "muted" {
  switch (status) {
    case "success":
    case "passed":
    case "completed":
    case "ready":
      return "success";
    case "warning":
    case "needs_changes":
    case "waived":
    case "partial":
      return "warning";
    case "error":
    case "failed":
    case "blocked":
    case "missing":
      return "danger";
    case "active":
    case "running":
    case "in_progress":
      return "active";
    default:
      return "muted";
  }
}

function labelForSourceKind(sourceKind: string): string {
  switch (sourceKind) {
    case "objective":
      return "Цель";
    case "child":
      return "Подзадача";
    case "domain_pack":
      return "Доменный пакет";
    default:
      return prettyLabel(sourceKind);
  }
}

function toneForCommandStatus(status: string | null | undefined): ToastTone {
  switch (status) {
    case "accepted":
      return "success";
    case "blocked":
    case "warning":
      return "warning";
    default:
      return "danger";
  }
}

function titleForCommandStatus(status: string | null | undefined): string {
  switch (status) {
    case "accepted":
      return "Команда выполнена";
    case "blocked":
      return "Нет доступного следующего шага";
    case "warning":
      return "Команда остановилась с замечанием";
    default:
      return "Команда не выполнена";
  }
}

function useTimelineFreshness(entries: TimelineEntryView[]): number[] {
  const previousTopSequence = useRef<number>(entries[0]?.sequence ?? 0);
  const [recentSequences, setRecentSequences] = useState<number[]>([]);

  useEffect(() => {
    const newest = entries[0]?.sequence ?? 0;
    const previous = previousTopSequence.current;
    if (newest > previous) {
      const fresh = entries.filter((entry) => entry.sequence > previous).map((entry) => entry.sequence);
      setRecentSequences(fresh);
      const timer = window.setTimeout(() => setRecentSequences([]), 1400);
      previousTopSequence.current = newest;
      return () => window.clearTimeout(timer);
    }
    previousTopSequence.current = newest;
    return undefined;
  }, [entries]);

  return recentSequences;
}

function App() {
  return (
    <BrowserRouter>
      <AppFrame />
    </BrowserRouter>
  );
}

function AppFrame() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [createOpen, setCreateOpen] = useState(false);
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

  const notify = (tone: ToastTone, title: string, description: string) => {
    const item: ToastItem = { id: `${Date.now()}-${Math.random()}`, tone, title, description };
    setToasts((current) => [...current, item]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== item.id));
    }, 3200);
  };

  const createProjectMutation = useMutation({
    mutationFn: api.createProject,
    onSuccess: (created: ProjectCreatedView) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      notify("success", "Проект создан", `Создан новый кейс «${created.name}».`);
      setCreateOpen(false);
      navigate(`/projects/${created.project_id}/overview`);
    },
    onError: (error: Error) => {
      notify("danger", "Не удалось создать проект", error.message);
    },
  });

  const selectedProjectId = useMemo(() => {
    const match = location.pathname.match(/\/projects\/([^/]+)/);
    return match?.[1] ?? null;
  }, [location.pathname]);
  const firstProject = projectsQuery.data?.[0] ?? null;

  return (
    <div className="app-shell">
      <ProjectRail
        projects={projectsQuery.data ?? []}
        selectedProjectId={selectedProjectId}
        onCreate={() => setCreateOpen(true)}
      />
      <main className="app-main">
        <Routes>
          <Route
            path="/"
            element={
              projectsQuery.isLoading ? (
                <LoadingPanel title="Загрузка проектов…" />
              ) : projectsQuery.data && projectsQuery.data.length > 0 ? (
                <ProjectsHomeDashboard
                  projects={projectsQuery.data}
                  onCreate={() => setCreateOpen(true)}
                  onOpenProject={(pid) => navigate(`/projects/${pid}/overview`)}
                />
              ) : (
                <LandingEmpty onCreate={() => setCreateOpen(true)} />
              )
            }
          />
          <Route path="/projects/:projectId" element={<Navigate to="overview" replace />} />
          <Route
            path="/projects/:projectId/*"
            element={<WorkspaceRoute onCreate={() => setCreateOpen(true)} notify={notify} />}
          />
        </Routes>
      </main>

      <CreateProjectModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={(payload) => createProjectMutation.mutate(payload)}
        busy={createProjectMutation.isPending}
      />

      <ToastViewport toasts={toasts} />
    </div>
  );
}

function LandingEmpty({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="landing-empty">
      <div className="landing-empty__inner">
        <div className="landing-empty__badge">
          <Sparkles size={16} />
          Workspace готов к запуску
        </div>
        <h1>PoV Generator Workspace</h1>
        <p>
          Интерфейс уже подключён к живому `M9` backend и готов вести проект от сырого запроса до
          артефактов, ревью и технических деталей.
        </p>
        <Button tone="primary" icon={<Plus size={16} />} onClick={onCreate}>
          Создать первый проект
        </Button>
      </div>
    </div>
  );
}

function WorkspaceRoute({
  onCreate,
  notify,
}: {
  onCreate: () => void;
  notify: (tone: ToastTone, title: string, description: string) => void;
}) {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [provider, setProvider] = useStoredState("povgen.provider", "openrouter");
  const [model, setModel] = useStoredState("povgen.model", "deepseek/deepseek-v4-flash");
  const [flashProjection, setFlashProjection] = useState<ProjectionName | null>(null);
  const [commandBusy, setCommandBusy] = useState(false);

  const shellQuery = useQuery({
    queryKey: projectionKey(projectId, "shell"),
    queryFn: () => api.getShell(projectId),
    enabled: Boolean(projectId),
  });
  const headerClarificationsQuery = useQuery({
    queryKey: projectionKey(projectId, "clarifications"),
    queryFn: () => api.getClarifications(projectId),
    enabled: Boolean(projectId),
  });

  const commandRequest = async (promiseFactory: () => Promise<CommandResultView>) => {
    setCommandBusy(true);
    try {
      const result = await promiseFactory();
      for (const projection of result.changed_projections) {
        await queryClient.invalidateQueries({ queryKey: projectionKey(projectId, projection) });
      }
      notify(toneForCommandStatus(result.status), titleForCommandStatus(result.status), result.summary);
    } catch (error) {
      notify("danger", "Команда не выполнена", error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setCommandBusy(false);
    }
  };

  const commandMutations = useMemo<WorkspaceActionApi>(
    () => ({
      runNext: () => void commandRequest(() => api.runNext(projectId, provider, model)),
      runUntilBlocked: () => {
        // W4.1 (R1): endpoint асинхронный, возвращает WorkflowRunView сразу.
        // Не используем commandRequest (он ждёт CommandResultView).
        setCommandBusy(true);
        api.runUntilBlocked(projectId, provider, model)
          .then((run) => {
            notify("success", "Workflow запущен", `Шагов запланировано: ${run.max_steps}. Прогресс под вкладками.`);
            void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
          })
          .catch((error) => {
            notify("danger", "Не удалось запустить workflow", error instanceof Error ? error.message : "Неизвестная ошибка");
          })
          .finally(() => setCommandBusy(false));
      },
      retryTask: (taskId: string) => void commandRequest(() => api.retryTask(projectId, taskId, provider, model)),
      setGoal: (text: string) => void commandRequest(() => api.setGoal(projectId, text)),
      closeGap: (gapId: string) => void commandRequest(() => api.closeGap(projectId, gapId)),
      setReadiness: (payload) => void commandRequest(() => api.setReadiness(projectId, payload)),
      enableDomainPack: (packRef: string) => void commandRequest(() => api.enableDomainPack(projectId, packRef)),
      answerClarification: (payload) => void commandRequest(() => api.answerClarification(projectId, payload)),
      acceptAssumption: (clarificationId: string) => void commandRequest(() => api.acceptAssumption(projectId, clarificationId)),
      setClarificationMode: (mode: string) => void commandRequest(() => api.setClarificationMode(projectId, mode)),
      busy: commandBusy,
    }),
    [commandBusy, model, projectId, provider],
  );

  const { status: realtimeStatus } = useProjectRealtime({
    projectId,
    projections: REALTIME_PROJECTIONS,
    onProjectionChanged: (projection) => {
      void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, projection) });
      // "methodology" event also invalidates the registry-wide list query.
      if (projection === "methodology") {
        void queryClient.invalidateQueries({ queryKey: ["methodology-packs"] });
      }
      // W4.1 (R1): workflow_runs мутируются runner'ом между шагами, mtime
      // БД меняется → realtime_token broadcasts на ВСЕ projections.
      // Инвалидируем активный run и список — UI подхватит прогресс.
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-runs"] });
      setFlashProjection(projection);
      window.setTimeout(() => setFlashProjection(null), 1200);
    },
  });

  if (shellQuery.isLoading) {
    return <LoadingPanel title="Загрузка проекта…" />;
  }

  if (shellQuery.isError || !shellQuery.data) {
    return (
      <SectionCard title="Проект недоступен" tone="danger">
        <EmptyState
          icon={<XCircle size={18} />}
          title="Не удалось открыть проект"
          description="Выберите другой кейс слева или создайте новый."
          action={
            <div className="inline-actions">
              <Button tone="secondary" onClick={() => navigate("/")}>
                К списку проектов
              </Button>
              <Button tone="primary" onClick={onCreate}>
                Новый проект
              </Button>
            </div>
          }
        />
      </SectionCard>
    );
  }

  return (
    <div className="workspace-route">
      <WorkspaceHeader
        shell={shellQuery.data}
        connectionStatus={realtimeStatus}
        clarificationMode={headerClarificationsQuery.data?.mode}
        onClarificationModeChange={commandMutations.setClarificationMode}
        modePending={commandMutations.busy}
        openClarificationCount={headerClarificationsQuery.data?.open_count}
        blockingClarificationCount={headerClarificationsQuery.data?.blocking_count}
        onOpenClarifications={() => navigate(`/projects/${projectId}/clarifications`)}
        actions={<CommandBar projectId={projectId} />}
      />
      <WorkspaceTabs projectId={projectId} />
      <WorkflowRunProgressPanel projectId={projectId} />
      <Routes>
        <Route
          path="overview"
          element={
            <ProjectOverviewV2
              projectId={projectId}
              onOpenClarifications={() => navigate(`/projects/${projectId}/clarifications`)}
              onOpenDecisionLog={() => navigate(`/projects/${projectId}/decisions`)}
              onOpenArtifactFull={(artifactId) =>
                navigate(`/projects/${projectId}/artifacts/${artifactId}`)
              }
              onContinue={commandMutations.runUntilBlocked}
              onRetryTask={commandMutations.retryTask}
            />
          }
        />
        {/* Legacy «mission control» / «активность» оставлены до L6-8 как fallback */}
        <Route
          path="mission"
          element={
            <MissionControlPage
              projectId={projectId}
              flashProjection={flashProjection}
              commands={commandMutations}
            />
          }
        />
        <Route
          path="activity"
          element={
            <OverviewPage
              projectId={projectId}
              flashProjection={flashProjection}
              onAction={(action) => handleAction(action, projectId, navigate, commandMutations)}
              commands={commandMutations}
            />
          }
        />
        <Route path="artifacts" element={<ArtifactsPage projectId={projectId} />} />
        <Route path="artifacts/:artifactId" element={<ArtifactsPage projectId={projectId} />} />
        <Route path="task-graph" element={<TaskGraphPage projectId={projectId} />} />
        <Route
          path="clarifications"
          element={<ClarificationsPage projectId={projectId} commands={commandMutations} />}
        />
        <Route path="decisions" element={<DecisionLogPage projectId={projectId} />} />
        <Route path="methodology" element={<MethodologyPage projectId={projectId} />} />
        <Route
          path="settings"
          element={
            <SettingsPage
              projectId={projectId}
              panels={{
                state: <StatePage projectId={projectId} actions={commandMutations} />,
                review: <ReviewPage projectId={projectId} />,
                debug: <DebugPage projectId={projectId} onRetryTask={commandMutations.retryTask} />,
              }}
            />
          }
        />
        {/* Legacy aliases — старые закладки переживут редизайн */}
        <Route path="state" element={<Navigate to={`/projects/${projectId}/settings?tab=state`} replace />} />
        <Route path="review" element={<Navigate to={`/projects/${projectId}/settings?tab=review`} replace />} />
        <Route path="debug" element={<Navigate to={`/projects/${projectId}/settings?tab=debug`} replace />} />
        <Route path="*" element={<Navigate to="overview" replace />} />
      </Routes>
    </div>
  );
}


// ---- W4.1 (R1): WorkflowRunProgressPanel ---------------------------------

function WorkflowRunProgressPanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const activeQuery = useQuery({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => api.getActiveWorkflowRun(projectId),
    // Полл каждые 1.5 сек на случай если WS broadcast пропустим (например
    // если token не сменился из-за внешнего write). Дёшево — endpoint
    // отвечает за < 5 ms.
    refetchInterval: 1500,
  });
  const recentQuery = useQuery({
    queryKey: [projectId, "workflow-runs"],
    queryFn: () => api.listWorkflowRuns(projectId, 5),
    refetchInterval: 5_000,
  });
  const [stickyRunId, setStickyRunId] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const cancelMutation = useMutation({
    mutationFn: (runId: string) => api.cancelWorkflow(projectId, runId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
    },
  });

  const active = activeQuery.data ?? null;
  // Когда run заканчивается, active становится null — но мы хотим
  // показать терминал ещё немного, пока пользователь не закроет.
  const recent = recentQuery.data ?? [];
  const sticky = stickyRunId ? recent.find((r) => r.run_id === stickyRunId) ?? null : null;
  const display = active ?? sticky ?? recent[0] ?? null;

  // Если новый active появился — запомнить его run_id как sticky
  // (чтобы после завершения он не пропадал моментально).
  if (active && stickyRunId !== active.run_id) {
    setStickyRunId(active.run_id);
  }

  if (!display) return null;

  const isActive = display.status === "pending" || display.status === "running";
  const progressPct =
    display.max_steps > 0
      ? Math.min(100, Math.round((display.current_step / display.max_steps) * 100))
      : 0;
  const statusLabel = labelForRunStatus(display.status);
  const statusTone = toneForRunStatus(display.status);

  return (
    <div className={cx("workflow-run", `workflow-run--${display.status}`)}>
      <div className="workflow-run__head">
        <div className="workflow-run__title">
          <StatusPill tone={statusTone}>{statusLabel}</StatusPill>
          <span className="workflow-run__summary">{display.last_step_summary || "—"}</span>
        </div>
        <div className="workflow-run__actions">
          {isActive ? (
            <Button
              tone="secondary"
              onClick={() => cancelMutation.mutate(display.run_id)}
              disabled={cancelMutation.isPending || display.cancel_requested}
            >
              {display.cancel_requested ? "Останавливаем..." : "Прервать"}
            </Button>
          ) : (
            <Button tone="secondary" onClick={() => setStickyRunId(null)}>
              Скрыть
            </Button>
          )}
          {display.steps.length > 0 && (
            <Button tone="secondary" onClick={() => setShowHistory((v) => !v)}>
              {showHistory ? "Свернуть" : `Шаги (${display.steps.length})`}
            </Button>
          )}
        </div>
      </div>
      <div className="workflow-run__bar">
        <div className="workflow-run__bar-track">
          <div
            className={cx(
              "workflow-run__bar-fill",
              isActive && "workflow-run__bar-fill--pulse",
            )}
            style={{ width: `${progressPct}%` }}
          />
        </div>
        <span className="workflow-run__counter">
          {display.current_step}/{display.max_steps}
          {display.stop_reason ? ` · ${labelForStopReason(display.stop_reason)}` : ""}
        </span>
      </div>
      {showHistory && (
        <ul className="workflow-run__steps">
          {display.steps.slice().reverse().map((step) => (
            <li key={step.sequence}>
              <span className="workflow-run__step-seq">#{step.sequence}</span>
              <span className="workflow-run__step-title">{step.selected_step_id || step.task_key || "(unknown)"}</span>
              <span
                className={cx(
                  "workflow-run__step-status",
                  `workflow-run__step-status--${step.validation_status ?? step.planning_outcome}`,
                )}
              >
                {step.error_message || step.validation_status || step.planning_outcome}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function labelForRunStatus(status: string): string {
  switch (status) {
    case "pending": return "Подготовка";
    case "running": return "Идёт workflow";
    case "completed": return "Завершено";
    case "failed": return "Ошибка";
    case "cancelled": return "Прервано";
    default: return prettyLabel(status);
  }
}

function toneForRunStatus(status: string): "neutral" | "active" | "success" | "warning" | "danger" | "muted" {
  switch (status) {
    case "pending":
    case "running": return "active";
    case "completed": return "success";
    case "failed": return "danger";
    case "cancelled": return "warning";
    default: return "neutral";
  }
}

function labelForStopReason(reason: string): string {
  switch (reason) {
    case "objective_completed": return "цель достигнута";
    case "planner_blocked": return "планировщик заблокирован";
    case "validation_failed": return "проверка не прошла";
    case "max_steps_reached": return "лимит шагов";
    case "execution_error": return "ошибка исполнения";
    case "cancelled_by_user": return "прервано пользователем";
    default: return reason;
  }
}


function MissionControlPage({
  projectId,
  flashProjection,
  commands,
}: {
  projectId: string;
  flashProjection: ProjectionName | null;
  commands: WorkspaceActionApi;
}) {
  const navigate = useNavigate();
  const overviewQuery = useQuery({
    queryKey: projectionKey(projectId, "overview"),
    queryFn: () => api.getOverview(projectId),
    refetchInterval: 30_000,
  });

  if (overviewQuery.isLoading) {
    return <LoadingPanel title="Загружаем mission control…" />;
  }
  if (overviewQuery.isError || !overviewQuery.data) {
    return (
      <SectionCard title="Mission Control недоступен" tone="danger">
        <EmptyState
          title="Не удалось загрузить агрегированный обзор"
          description={String((overviewQuery.error as Error)?.message ?? "Повторите обновление страницы.")}
        />
      </SectionCard>
    );
  }

  const overview = overviewQuery.data;
  const progress = overview.objective_progress;
  const hasArtifactGoal = progress.artifacts_required > 0;
  const hasGateGoal = progress.gates_required > 0;
  const artifactsPct = hasArtifactGoal
    ? Math.min(100, Math.round((progress.artifacts_ready / progress.artifacts_required) * 100))
    : 0;
  const gatesPct = hasGateGoal
    ? Math.min(100, Math.round((progress.gates_passed / progress.gates_required) * 100))
    : 0;
  const flashOverview = flashProjection === "situation" || flashProjection === "clarifications";

  return (
    <div className={cx("mission-control", flashOverview && "mission-control--flash")}>
      <SectionCard title="Где мы сейчас">
        <div className="mc-stage">
          <div className="mc-stage__row">
            <span className="mc-stage__label">Стадия</span>
            <span className="mc-stage__value">{overview.stage_summary || "не определена"}</span>
          </div>
          <div className="mc-stage__row">
            <span className="mc-stage__label">Сейчас</span>
            <span className="mc-stage__value">{overview.current_activity || "система ожидает следующего шага"}</span>
          </div>
          <div className="mc-stage__row">
            <span className="mc-stage__label">Режим участия</span>
            <span className="mc-stage__value">{prettyLabel(overview.clarification_mode)}</span>
          </div>
        </div>
      </SectionCard>

      {(hasArtifactGoal || hasGateGoal) && (
        <SectionCard title="Прогресс по цели">
          <div className="mc-progress">
            {hasArtifactGoal && (
              <div className="mc-progress__row">
                <div className="mc-progress__label">
                  <span>Артефакты</span>
                  <span>
                    {progress.artifacts_ready}/{progress.artifacts_required}
                  </span>
                </div>
                <div className="mc-progress__bar">
                  <div className="mc-progress__bar-fill" style={{ width: `${artifactsPct}%` }} />
                </div>
              </div>
            )}
            {hasGateGoal && (
              <div className="mc-progress__row">
                <div className="mc-progress__label">
                  <span>Gates</span>
                  <span>
                    {progress.gates_passed}/{progress.gates_required}
                  </span>
                </div>
                <div className="mc-progress__bar">
                  <div className="mc-progress__bar-fill" style={{ width: `${gatesPct}%` }} />
                </div>
              </div>
            )}
          </div>
        </SectionCard>
      )}

      {overview.critical_clarifications.length > 0 && (
        <SectionCard title={`Критичные уточнения (${overview.critical_clarifications.length})`} tone="warning">
          <ul className="mc-list">
            {overview.critical_clarifications.slice(0, 5).map((item) => (
              <li key={item.clarification_id} className="mc-list__row">
                <button
                  type="button"
                  className="mc-list__link"
                  onClick={() => navigate(`/projects/${projectId}/activity?clarification=${item.clarification_id}`)}
                >
                  <span className="mc-list__title">{item.title}</span>
                  <span className={cx("mc-pill", `mc-pill--${item.priority}`)}>{item.priority}</span>
                  <span className="mc-list__meta">
                    {prettyLabel(item.source_type)} · {prettyLabel(item.blocking_scope)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </SectionCard>
      )}

      {overview.key_artifacts.length > 0 && (
        <SectionCard title="Ключевые артефакты">
          <ul className="mc-list">
            {overview.key_artifacts.slice(0, 5).map((item) => (
              <li key={item.artifact_id} className="mc-list__row">
                <Link
                  to={`/projects/${projectId}/artifacts/${item.artifact_id}`}
                  className="mc-list__link"
                >
                  <span className="mc-list__title">{item.title}</span>
                  <span className="mc-list__meta">{prettyLabel(item.artifact_role)}</span>
                  <span className="mc-list__meta">{formatDateTime(item.created_at)}</span>
                </Link>
              </li>
            ))}
          </ul>
        </SectionCard>
      )}

      <SectionCard title="Контекст рассуждения">
        <div className="mc-stage">
          <div className="mc-stage__row">
            <span className="mc-stage__label">Методология</span>
            <span className="mc-stage__value">
              {overview.active_methodology ? (
                <Link to={`/projects/${projectId}/methodology`}>{overview.active_methodology}</Link>
              ) : (
                "не назначена"
              )}
            </span>
          </div>
          <div className="mc-stage__row">
            <span className="mc-stage__label">Domain packs</span>
            <span className="mc-stage__value">
              {overview.active_domain_packs.length > 0 ? overview.active_domain_packs.join(", ") : "нет активных"}
            </span>
          </div>
        </div>
      </SectionCard>

      <div className="mc-footer">
        <Button tone="secondary" onClick={commands.runNext} disabled={commands.busy}>
          Запустить следующий шаг
        </Button>
        <Link to={`/projects/${projectId}/activity`} className="mc-footer__link">
          Полный экран активности →
        </Link>
      </div>
    </div>
  );
}


// ---- L2 ClarificationsPage (W5.2) ---------------------------------------

type ClarFilter = "open" | "answered" | "assumed" | "deferred" | "all";

function ClarificationsPage({
  projectId,
  commands,
}: {
  projectId: string;
  commands: WorkspaceActionApi;
}) {
  const [filter, setFilter] = useState<ClarFilter>("open");
  const [wizardId, setWizardId] = useState<string | null>(null);
  const clarQuery = useQuery({
    queryKey: projectionKey(projectId, "clarifications"),
    queryFn: () => api.getClarifications(projectId),
  });

  if (clarQuery.isLoading || !clarQuery.data) {
    return <LoadingPanel title="Загружаем вопросы…" />;
  }

  const view = clarQuery.data;
  const counts = {
    open: view.open_count,
    answered: view.answered_count,
    assumed: view.assumed_count,
    deferred: view.items.filter((i) => i.status === "deferred").length,
    blocking: view.blocking_count,
    // V1: «решено автоматически» = всё, что система закрыла без явного
    // действия пользователя (assumed_auto / deferred_auto). UI'у нужно,
    // чтобы менеджер на autopilot всё равно видел масштаб того, что
    // система делает за него.
    auto_resolved: view.items.filter((i) => i.auto_resolved).length,
  };
  const filtered = view.items.filter((item) => {
    if (filter === "all") return true;
    return item.status === filter;
  });

  return (
    <div className="clar-page">
      <SectionCard title="Вопросы к менеджеру">
        <div className="clar-hero">
          <ClarCounter label="Открытых" value={counts.open} tone="active" emphasis />
          <ClarCounter label="Блокирующих" value={counts.blocking} tone={counts.blocking > 0 ? "danger" : "muted"} />
          <ClarCounter label="🤖 Авто-решений" value={counts.auto_resolved} tone="active" />
          <ClarCounter label="Отвечено" value={counts.answered} tone="success" />
          <ClarCounter label="Допущений" value={counts.assumed} tone="muted" />
          <ClarCounter label="Отложено" value={counts.deferred} tone="warning" />
        </div>
        <div className="clar-toolbar">
          <div className="segmented">
            {(["open", "answered", "assumed", "deferred", "all"] as ClarFilter[]).map((f) => (
              <button
                key={f}
                type="button"
                className={cx("segmented__item", filter === f && "segmented__item--active")}
                onClick={() => setFilter(f)}
              >
                {labelForClarFilter(f)} ({f === "all" ? view.items.length : counts[f as keyof typeof counts] ?? 0})
              </button>
            ))}
          </div>
          {counts.open > 0 ? (
            <Button
              tone="primary"
              onClick={() => {
                const firstOpen = view.items.find((i) => i.status === "open");
                if (firstOpen) setWizardId(firstOpen.clarification_id);
              }}
            >
              Пройти все по очереди ({counts.open})
            </Button>
          ) : null}
        </div>
        <ul className="clar-list">
          {filtered.length === 0 ? (
            <li className="clar-list__empty">
              <EmptyState
                title={filter === "open" ? "Открытых вопросов нет" : "Нет записей в этой категории"}
                description={
                  filter === "open"
                    ? "Когда система решит спросить — карточка появится здесь и в шапке проекта."
                    : "Переключи фильтр выше, чтобы увидеть остальные."
                }
              />
            </li>
          ) : (
            filtered.map((item) => (
              <li key={item.clarification_id}>
                <ClarRowCard
                  item={item}
                  onOpen={() => setWizardId(item.clarification_id)}
                />
              </li>
            ))
          )}
        </ul>
      </SectionCard>
      {wizardId ? (
        <ClarificationWizardModal
          projectId={projectId}
          startId={wizardId}
          onClose={() => setWizardId(null)}
          commands={commands}
        />
      ) : null}
    </div>
  );
}

function ClarCounter({
  label,
  value,
  tone,
  emphasis,
}: {
  label: string;
  value: number;
  tone: "active" | "success" | "warning" | "danger" | "muted";
  emphasis?: boolean;
}) {
  return (
    <div className={cx("clar-counter", `clar-counter--${tone}`, emphasis && "clar-counter--emphasis")}>
      <span className="clar-counter__value">{value}</span>
      <span className="clar-counter__label">{label}</span>
    </div>
  );
}

function ClarRowCard({
  item,
  onOpen,
}: {
  item: ClarificationItemView;
  onOpen: () => void;
}) {
  const blocking = item.blocking_scope !== "none";
  return (
    <button type="button" className={cx("clar-row", blocking && "clar-row--blocking", item.auto_resolved && "clar-row--auto")} onClick={onOpen}>
      <div className="clar-row__head">
        <StatusPill tone={toneForClarificationPriority(item.priority)}>{prettyLabel(item.priority)}</StatusPill>
        <span className={cx("clar-role", `clar-role--${item.decision_owner_role}`)}>
          {prettyDecisionOwnerRole(item.decision_owner_role)}
        </span>
        <span className={cx("clar-blocking", `clar-blocking--${item.blocking_scope}`)}>
          {labelForBlockingScope(item.blocking_scope)}
        </span>
        {item.auto_resolved ? (
          <span className="clar-auto-badge" title="Закрыто системой автоматически (autopilot/допущение)">
            🤖 авто
          </span>
        ) : null}
        <span className="clar-row__mode">
          мин. режим: <strong>{labelForEngagementMode(item.min_participation_mode)}</strong>
        </span>
        <span className="clar-row__status">{labelForClarStatus(item.status)}</span>
      </div>
      <div className="clar-row__body">
        <span className="clar-row__question">{item.question}</span>
        {item.resolution_summary ? (
          <span className="clar-row__answer">→ {item.resolution_summary}</span>
        ) : null}
      </div>
      <div className="clar-row__meta">
        <span>{formatDateTime(item.updated_at)}</span>
        <span>{item.affected_task_ids.length} задач · {item.related_artifact_ids.length} артефактов</span>
      </div>
    </button>
  );
}

function labelForClarFilter(filter: ClarFilter): string {
  switch (filter) {
    case "open": return "Открытые";
    case "answered": return "Отвечено";
    case "assumed": return "Допущения";
    case "deferred": return "Отложено";
    case "all": return "Все";
  }
}

function labelForBlockingScope(scope: string): string {
  switch (scope) {
    case "objective": return "🔒 блокирует цель";
    case "subtree": return "⚠ блокирует ветку";
    case "task": return "⛔ блокирует задачу";
    case "none": return "не блокирует";
    default: return scope;
  }
}

function labelForEngagementMode(mode: string): string {
  switch (mode) {
    case "autopilot": return "автопилот";
    case "balanced": return "сбалансированный";
    case "control": return "контроль";
    case "expert": return "эксперт";
    default: return mode;
  }
}

function labelForClarStatus(status: string): string {
  switch (status) {
    case "open": return "Ожидает ответа";
    case "answered": return "Отвечено";
    case "assumed": return "Принято допущение";
    case "deferred": return "Отложено";
    case "cancelled": return "Закрыто";
    default: return prettyLabel(status);
  }
}

// ---- Wizard-модалка с auto-advance (W5.2) ------------------------------

function ClarificationWizardModal({
  projectId,
  startId,
  onClose,
  commands,
}: {
  projectId: string;
  startId: string;
  onClose: () => void;
  commands: WorkspaceActionApi;
}) {
  const [currentId, setCurrentId] = useState(startId);
  const [historyVisible, setHistoryVisible] = useState(false);
  const queryClient = useQueryClient();

  const detailQuery = useQuery({
    queryKey: [projectId, "clarification-detail", currentId],
    queryFn: () => api.getClarificationDetail(projectId, currentId),
    enabled: Boolean(currentId),
  });
  const eventsQuery = useQuery({
    queryKey: [projectId, "clarification-events", currentId],
    queryFn: () => api.getClarificationEvents(projectId, currentId),
    enabled: Boolean(currentId) && historyVisible,
  });
  const deferMutation = useMutation({
    mutationFn: (reason: string | undefined) => api.deferClarification(projectId, currentId, reason),
    onSuccess: () => advanceOrClose(),
  });
  const reopenMutation = useMutation({
    mutationFn: () => api.reopenClarification(projectId, currentId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: [projectId, "clarification-detail", currentId] });
      await queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "clarifications") });
    },
  });
  const answerMutation = useMutation({
    mutationFn: (payload: { clarification_id: string; selected_option_ids: string[]; free_text?: string }) =>
      api.answerClarification(projectId, payload),
    onSuccess: () => advanceOrClose(),
  });
  const acceptMutation = useMutation({
    mutationFn: () => api.acceptAssumption(projectId, currentId),
    onSuccess: () => advanceOrClose(),
  });

  const advanceOrClose = async () => {
    await queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "clarifications") });
    const next = await api.getNextOpenClarification(projectId, currentId);
    if (next) {
      setCurrentId(next.clarification_id);
      setHistoryVisible(false);
    } else {
      onClose();
    }
  };

  if (detailQuery.isLoading || !detailQuery.data) {
    return (
      <Modal open onClose={onClose} title="Вопрос">
        <LoadingPanel title="Загружаем вопрос…" />
      </Modal>
    );
  }
  const detail = detailQuery.data;
  const events = eventsQuery.data ?? [];
  const isOpen = detail.status === "open";
  const isAnswered = detail.status === "answered" || detail.status === "assumed";
  const isDeferred = detail.status === "deferred";
  const busy =
    answerMutation.isPending ||
    acceptMutation.isPending ||
    deferMutation.isPending ||
    reopenMutation.isPending;

  return (
    <Modal open onClose={onClose} title="Вопрос к менеджеру">
      <div className="clar-wizard">
        <div className="clar-wizard__chips">
          <StatusPill tone={toneForClarificationPriority(detail.priority)}>
            {prettyLabel(detail.priority)}
          </StatusPill>
          <span className={cx("clar-role", `clar-role--${detail.decision_owner_role}`)}>
            {prettyDecisionOwnerRole(detail.decision_owner_role)}
          </span>
          <span className={cx("clar-blocking", `clar-blocking--${detail.blocking_scope}`)}>
            {labelForBlockingScope(detail.blocking_scope)}
          </span>
          <span className="clar-wizard__mode">
            мин. режим: <strong>{labelForEngagementMode(detail.min_participation_mode)}</strong>
          </span>
          <span className="clar-wizard__status">{labelForClarStatus(detail.status)}</span>
        </div>
        <div className="clar-wizard__question">
          <h3>{detail.question}</h3>
          <p>{clarificationDescription(detail)}</p>
        </div>

        {isOpen ? (
          <ClarificationAnswerForm
            clarification={detail}
            onAnswer={(payload) => answerMutation.mutate(payload)}
            onAcceptAssumption={() => acceptMutation.mutate()}
            pending={busy}
            variant="modal"
          />
        ) : (
          <div className="clarification-resolution">
            <span>{isAnswered ? "Ответ" : isDeferred ? "Причина" : "Резолюция"}</span>
            <p>{detail.resolution_summary || detail.default_assumption || "—"}</p>
          </div>
        )}

        <div className="clar-wizard__actions">
          {isOpen ? (
            <Button
              tone="secondary"
              onClick={() => deferMutation.mutate("Пропущено пользователем.")}
              disabled={busy}
            >
              Отложить
            </Button>
          ) : null}
          {(isAnswered || isDeferred) ? (
            <Button
              tone="secondary"
              onClick={() => reopenMutation.mutate()}
              disabled={busy}
            >
              Переответить
            </Button>
          ) : null}
          <Button
            tone="secondary"
            onClick={() => setHistoryVisible((v) => !v)}
          >
            {historyVisible ? "Скрыть историю" : "История"}
          </Button>
          <div className="clar-wizard__nav">
            <Button
              tone="secondary"
              onClick={async () => {
                const next = await api.getNextOpenClarification(projectId, currentId);
                if (next) setCurrentId(next.clarification_id);
                else onClose();
              }}
              disabled={busy}
            >
              Дальше →
            </Button>
          </div>
        </div>

        {historyVisible && (
          <div className="clar-wizard__history">
            <h4>История вопроса</h4>
            {events.length === 0 ? (
              <p className="clar-wizard__history-empty">События не зафиксированы.</p>
            ) : (
              <ol>
                {events.map((evt) => (
                  <li key={evt.event_id}>
                    <span className="clar-wizard__history-type">{labelForEventType(evt.event_type)}</span>
                    <span className="clar-wizard__history-time">{formatDateTime(evt.created_at)}</span>
                    {Object.keys(evt.payload).length > 0 ? (
                      <pre>{JSON.stringify(evt.payload, null, 2)}</pre>
                    ) : null}
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}

function labelForEventType(type: string): string {
  switch (type) {
    case "created": return "Создан";
    case "assumed_auto": return "Принято авто-допущение";
    case "answered": return "Получен ответ";
    case "assumed": return "Принято допущение";
    case "deferred": return "Отложено";
    case "reopened": return "Возобновлён";
    default: return type;
  }
}


// ---- L2 MethodologyPage --------------------------------------------------

function MethodologyPage({ projectId }: { projectId: string }) {
  const packsQuery = useQuery({
    queryKey: ["methodology-packs"],
    queryFn: () => api.listMethodologyPacks(),
    staleTime: 60_000,
  });
  const overviewQuery = useQuery({
    queryKey: projectionKey(projectId, "overview"),
    queryFn: () => api.getOverview(projectId),
  });
  const queryClient = useQueryClient();
  const setMethodologyMutation = useMutation({
    mutationFn: (packRef: string) => api.setMethodology(projectId, packRef),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "overview") });
    },
  });

  if (packsQuery.isLoading || overviewQuery.isLoading) {
    return <LoadingPanel title="Загружаем карту методологий…" />;
  }
  if (packsQuery.isError || !packsQuery.data) {
    return (
      <SectionCard title="Методология недоступна" tone="danger">
        <EmptyState
          title="Не удалось получить список methodology packs"
          description={String((packsQuery.error as Error)?.message ?? "")}
        />
      </SectionCard>
    );
  }

  const packs = packsQuery.data;
  const activeRef = overviewQuery.data?.active_methodology ?? null;

  return (
    <div className="methodology-page">
      <SectionCard title="Активная методология">
        {activeRef ? (
          <p className="methodology-page__active">
            Сейчас на проект наложен пакет <strong>{activeRef}</strong>. Стадии ниже применяются ко всем
            leaf-задачам через runtime wrapper.
          </p>
        ) : (
          <p className="methodology-page__active methodology-page__active--empty">
            На проект не наложен ни один methodology pack.
          </p>
        )}
      </SectionCard>

      {packs.map((pack) => (
        <MethodologyPackCard
          key={pack.pack_ref}
          pack={pack}
          isActive={pack.pack_ref === activeRef}
          canSwitch={packs.length > 1 || pack.pack_ref !== activeRef}
          pending={setMethodologyMutation.isPending}
          onSwitch={() => setMethodologyMutation.mutate(pack.pack_ref)}
        />
      ))}
    </div>
  );
}

function MethodologyPackCard({
  pack,
  isActive,
  canSwitch,
  pending,
  onSwitch,
}: {
  pack: import("./types").MethodologyPackView;
  isActive: boolean;
  canSwitch: boolean;
  pending: boolean;
  onSwitch: () => void;
}) {
  const requiredSet = new Set(pack.required_stages);
  return (
    <SectionCard
      title={`${pack.title} — ${pack.pack_ref}`}
      subtitle={pack.description}
      tone={isActive ? "accent" : undefined}
      actions={
        canSwitch && !isActive ? (
          <Button tone="secondary" onClick={onSwitch} disabled={pending}>
            Сделать активной
          </Button>
        ) : isActive ? (
          <StatusPill tone="success">Активна</StatusPill>
        ) : null
      }
    >
      <div className="methodology-pack">
        <div className="methodology-pack__meta">
          <span>Режим: <strong>{pack.stage_execution_mode}</strong></span>
          <span>Статус: <strong>{pack.status}</strong></span>
        </div>
        <ol className="methodology-pack__stages">
          {pack.stages.map((stage) => {
            const required = requiredSet.has(stage.id);
            return (
              <li key={stage.id} className="methodology-stage">
                <div className="methodology-stage__header">
                  <span className="methodology-stage__title">{stage.title}</span>
                  <StatusPill tone={required ? "active" : "muted"}>
                    {required ? "required" : "optional"}
                  </StatusPill>
                  <span className="methodology-stage__id">{stage.id}</span>
                </div>
                {stage.description ? (
                  <p className="methodology-stage__description">{stage.description}</p>
                ) : null}
                {stage.produces.length > 0 && (
                  <div className="methodology-stage__produces">
                    <span className="methodology-stage__label">Поля:</span>
                    <ul>
                      {stage.produces.map((p) => (
                        <li key={p.field}>
                          <code>{p.field}</code> : <em>{p.type}</em>{p.required ? " *" : ""}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {stage.rules.length > 0 && (
                  <div className="methodology-stage__rules">
                    <span className="methodology-stage__label">Правила:</span>
                    <ul>
                      {stage.rules.map((r) => (
                        <li key={r.id}>
                          <code>{r.id}</code>
                          {r.if ? <> : <code className="methodology-stage__expr">{r.if}</code></> : null}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      </div>
    </SectionCard>
  );
}


// ---- L3 ReasoningPanel (показ reasoning_artifact для задачи) -------------

function ReasoningPanel({
  projectId,
  taskId,
}: {
  projectId: string;
  taskId: string;
}) {
  const [provenanceOpen, setProvenanceOpen] = useState(false);
  const traceQuery = useQuery({
    queryKey: [projectId, "methodology-trace", taskId],
    queryFn: () => api.getMethodologyTrace(projectId, taskId),
  });

  if (traceQuery.isLoading) {
    return <SectionCard title="Рассуждение"><LoadingPanel title="Грузим reasoning…" /></SectionCard>;
  }
  if (traceQuery.isError || !traceQuery.data) {
    return null;
  }
  const data = traceQuery.data;
  if (!data.reasoning || !data.reasoning.stages || data.reasoning.stages.length === 0) {
    return (
      <SectionCard title="Рассуждение" subtitle={data.message ?? undefined}>
        <EmptyState
          title="Для этой задачи нет reasoning_artifact"
          description="Возможно, задача выполнена до подключения активной методологии или wrapper'у не хватило входов."
        />
      </SectionCard>
    );
  }
  const trace = data.trace;
  const firedByStage: Record<string, import("./types").MethodologyTraceRuleOutcome[]> = {};
  for (const rule of trace?.rules_evaluated ?? []) {
    if (!rule.fired) continue;
    (firedByStage[rule.stage_id] ??= []).push(rule);
  }

  return (
    <>
      <SectionCard
        title="Рассуждение"
        subtitle={`Методология ${data.reasoning.methodology_pack_ref}${data.reasoning.complexity ? ` · сложность ${data.reasoning.complexity}` : ""}`}
        actions={
          <Button tone="secondary" onClick={() => setProvenanceOpen(true)}>
            Откуда это
          </Button>
        }
      >
        <div className="reasoning-panel">
          {data.reasoning.stages.map((stage) => (
            <ReasoningStageCard
              key={stage.stage_id}
              stage={stage}
              firedRules={firedByStage[stage.stage_id] ?? []}
            />
          ))}
        </div>
      </SectionCard>
      <Modal
        open={provenanceOpen}
        onClose={() => setProvenanceOpen(false)}
        title="Provenance / откуда это"
      >
        <ProvenanceViewer data={data} />
      </Modal>
    </>
  );
}

// ---- L4 ProvenanceViewer ------------------------------------------------

function ProvenanceViewer({ data }: { data: import("./types").MethodologyTraceResponse }) {
  const trace = data.trace;
  const execution = data.execution ?? null;
  return (
    <div className="provenance">
      <section className="provenance__section">
        <h4>Контекст методологии</h4>
        <dl className="provenance__grid">
          <ProvenanceField label="Methodology pack" value={trace?.methodology_pack_ref ?? data.reasoning?.methodology_pack_ref} />
          <ProvenanceField label="Режим стадий" value={trace?.stage_execution_mode ?? "—"} />
          <ProvenanceField label="Сложность" value={trace?.complexity ?? data.reasoning?.complexity ?? "—"} />
        </dl>
      </section>

      {trace && trace.stages_executed && trace.stages_executed.length > 0 ? (
        <section className="provenance__section">
          <h4>Пройденные стадии</h4>
          <ol className="provenance__steps">
            {trace.stages_executed.map((stageId) => (
              <li key={stageId}>
                <code>{stageId}</code>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {trace && trace.rules_evaluated && trace.rules_evaluated.length > 0 ? (
        <section className="provenance__section">
          <h4>Проверенные правила</h4>
          <table className="provenance__table">
            <thead>
              <tr>
                <th>Стадия</th>
                <th>Правило</th>
                <th>Сработало</th>
                <th>Кандидат</th>
              </tr>
            </thead>
            <tbody>
              {trace.rules_evaluated.map((rule) => (
                <tr key={`${rule.stage_id}.${rule.rule_id}`}>
                  <td><code>{rule.stage_id}</code></td>
                  <td><code>{rule.rule_id}</code></td>
                  <td>
                    {rule.fired ? (
                      <StatusPill tone="warning">да</StatusPill>
                    ) : (
                      <StatusPill tone="muted">нет</StatusPill>
                    )}
                  </td>
                  <td>{rule.candidate_id ? <code>{rule.candidate_id.slice(0, 8)}</code> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {trace && trace.candidates_emitted && trace.candidates_emitted.length > 0 ? (
        <section className="provenance__section">
          <h4>Сгенерированные кандидаты уточнений</h4>
          <ul className="provenance__list">
            {trace.candidates_emitted.map((c) => (
              <li key={c.candidate_id}>
                <code>{c.candidate_id.slice(0, 8)}</code> —
                <StatusPill tone={c.severity === "critical" ? "danger" : c.severity === "high" ? "warning" : "muted"}>
                  {c.severity}
                </StatusPill>
                · {prettyLabel(c.blocking_scope)}
                <div className="provenance__source">{c.source_id}</div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="provenance__section">
        <h4>Исполнитель</h4>
        {execution ? (
          <dl className="provenance__grid">
            <ProvenanceField label="Execution run" value={execution.execution_run_id} mono />
            <ProvenanceField label="Provider" value={execution.provider} />
            <ProvenanceField label="Model" value={execution.model} />
            <ProvenanceField label="Статус" value={execution.status} />
            <ProvenanceField label="Context manifest" value={execution.context_manifest_id} mono />
            <ProvenanceField label="Время" value={execution.created_at ? formatDateTime(execution.created_at) : null} />
          </dl>
        ) : (
          <p className="provenance__empty">Execution run не зафиксирован для этой задачи.</p>
        )}
      </section>

      <section className="provenance__section">
        <h4>Артефакты</h4>
        <dl className="provenance__grid">
          <ProvenanceField label="Trace artifact" value={data.trace_artifact_id ?? null} mono />
          <ProvenanceField label="Reasoning artifact" value={data.reasoning_artifact_id ?? null} mono />
        </dl>
      </section>
    </div>
  );
}

function ProvenanceField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | null | undefined;
  mono?: boolean;
}) {
  return (
    <div className="provenance__field">
      <dt>{label}</dt>
      <dd className={mono ? "provenance__mono" : undefined}>{value ? value : <span className="provenance__null">—</span>}</dd>
    </div>
  );
}

function ReasoningStageCard({
  stage,
  firedRules,
}: {
  stage: import("./types").MethodologyReasoningStageView;
  firedRules: import("./types").MethodologyTraceRuleOutcome[];
}) {
  return (
    <div className="reasoning-stage">
      <div className="reasoning-stage__header">
        <span className="reasoning-stage__title">{stage.title || prettyLabel(stage.stage_id)}</span>
        <span className="reasoning-stage__id">{stage.stage_id}</span>
      </div>
      <ReasoningStageBody outputs={stage.outputs} />
      {firedRules.length > 0 && (
        <div className="reasoning-stage__fired">
          {firedRules.map((rule) => (
            <span key={rule.rule_id} className="reasoning-stage__rule">
              ⚡ правило <code>{rule.rule_id}</code> сработало
              {rule.candidate_id ? <> → уточнение <code>{rule.candidate_id.slice(0, 8)}</code></> : null}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ReasoningStageBody({ outputs }: { outputs: Record<string, unknown> }) {
  const entries = Object.entries(outputs ?? {}).filter(([key]) => !key.startsWith("_"));
  if (entries.length === 0) {
    return <p className="reasoning-stage__empty">Стадия ничего не зафиксировала.</p>;
  }
  return (
    <dl className="reasoning-stage__fields">
      {entries.map(([key, value]) => (
        <div key={key} className="reasoning-stage__field">
          <dt>{prettyLabel(key)}</dt>
          <dd>
            <ReasoningValue field={key} value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ReasoningValue({ field, value }: { field: string; value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="reasoning-stage__null">не зафиксировано</span>;
  }
  if (field === "options" && Array.isArray(value)) {
    return (
      <ul className="reasoning-options">
        {value.map((opt, idx) => {
          const item = (opt ?? {}) as Record<string, unknown>;
          return (
            <li key={idx} className="reasoning-option">
              <div className="reasoning-option__header">
                <strong>{String(item.label ?? `Вариант ${idx + 1}`)}</strong>
                {typeof item.confidence === "number" ? (
                  <span className="reasoning-option__confidence">
                    confidence {item.confidence.toFixed(2)}
                  </span>
                ) : null}
              </div>
              {typeof item.rationale === "string" ? <p>{item.rationale}</p> : null}
              {typeof item.tradeoffs === "string" ? <p className="reasoning-option__tradeoffs">{item.tradeoffs}</p> : null}
            </li>
          );
        })}
      </ul>
    );
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <span>{String(value)}</span>;
  }
  return <pre className="reasoning-stage__json">{JSON.stringify(value, null, 2)}</pre>;
}


function OverviewPage({
  projectId,
  flashProjection,
  onAction,
  commands,
}: {
  projectId: string;
  flashProjection: ProjectionName | null;
  onAction: (action: ActionDescriptor) => void;
  commands: WorkspaceActionApi;
}) {
  const [selectedEvent, setSelectedEvent] = useState<TimelineEntryView | null>(null);
  const [selectedTask, setSelectedTask] = useState<TaskNodeView | null>(null);
  const [selectedClarification, setSelectedClarification] = useState<ClarificationItemView | null>(null);

  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
  });
  const situationQuery = useQuery({
    queryKey: projectionKey(projectId, "situation"),
    queryFn: () => api.getSituation(projectId),
  });
  const timelineQuery = useQuery({
    queryKey: projectionKey(projectId, "timeline"),
    queryFn: () => api.getTimeline(projectId),
  });
  const clarificationsQuery = useQuery({
    queryKey: projectionKey(projectId, "clarifications"),
    queryFn: () => api.getClarifications(projectId),
  });
  const artifactsQuery = useQuery({
    queryKey: projectionKey(projectId, "artifacts"),
    queryFn: () => api.getArtifacts(projectId),
  });
  const reviewQuery = useQuery({
    queryKey: projectionKey(projectId, "review"),
    queryFn: () => api.getReview(projectId),
  });
  const stateQuery = useQuery({
    queryKey: projectionKey(projectId, "state"),
    queryFn: () => api.getState(projectId),
  });

  const recentSequences = useTimelineFreshness(timelineQuery.data?.entries ?? []);

  if (
    taskGraphQuery.isLoading ||
    situationQuery.isLoading ||
    timelineQuery.isLoading ||
    clarificationsQuery.isLoading ||
    artifactsQuery.isLoading ||
    reviewQuery.isLoading ||
    stateQuery.isLoading
  ) {
    return <LoadingPanel title="Сборка overview…" />;
  }

  if (
    !taskGraphQuery.data ||
    !situationQuery.data ||
    !timelineQuery.data ||
    !clarificationsQuery.data ||
    !artifactsQuery.data ||
    !reviewQuery.data ||
    !stateQuery.data
  ) {
    return (
      <SectionCard title="Не удалось собрать overview" tone="danger">
        <EmptyState title="Часть проектных проекций недоступна" description="Повторите обновление страницы." />
      </SectionCard>
    );
  }

  const primaryClarification = pickPrimaryClarification(clarificationsQuery.data.items);
  const taskList = flattenTaskNodes(taskGraphQuery.data.nodes);

  const openClarification = (clarification: ClarificationItemView) => {
    setSelectedClarification(clarification);
  };

  const openAction = (action: ActionDescriptor) => {
    if (action.target_view === "clarification" && action.target_id) {
      const clarification = clarificationsQuery.data.items.find((item) => item.clarification_id === action.target_id);
      if (clarification) {
        openClarification(clarification);
        return;
      }
    }
    onAction(action);
  };

  return (
    <>
      <ProjectCockpit
        situation={situationQuery.data}
        taskGraph={taskGraphQuery.data}
        artifacts={artifactsQuery.data}
        review={reviewQuery.data}
        clarifications={clarificationsQuery.data}
        onAction={openAction}
        onRunUntilBlocked={commands.runUntilBlocked}
        pending={commands.busy}
        flash={flashProjection === "situation" || flashProjection === "task_graph"}
      />

      {primaryClarification ? (
        <BlockingClarificationPanel
          clarification={primaryClarification}
          onOpenAnswer={() => openClarification(primaryClarification)}
          flash={flashProjection === "clarifications"}
        />
      ) : null}

      <div className="overview-grid">
        <div className="overview-grid__main">
          <TimelineFeed
            entries={timelineQuery.data.entries}
            onOpenEntry={(entry) => {
              if (entry.detail_view === "clarification" && entry.entity_id) {
                const clarification = clarificationsQuery.data.items.find((item) => item.clarification_id === entry.entity_id);
                if (clarification) {
                  openClarification(clarification);
                  return;
                }
              }
              setSelectedEvent(entry);
            }}
            recentSequences={recentSequences}
            flash={flashProjection === "timeline"}
          />
          <WorkMapSummary tasks={taskList} onOpenTask={setSelectedTask} flash={flashProjection === "task_graph"} />
        </div>
        <div className="overview-grid__side">
          <AttentionPanel
            situation={situationQuery.data}
            clarifications={clarificationsQuery.data}
            onAction={openAction}
            onOpenClarification={openClarification}
            onRetryTask={commands.retryTask}
            retryTaskId={retryTaskIdForSituation(situationQuery.data)}
          />
          <ClarificationCenter
            clarifications={clarificationsQuery.data}
            highlightedClarificationId={primaryClarification?.clarification_id}
            onOpenClarification={openClarification}
            onAcceptAssumption={commands.acceptAssumption}
            pending={commands.busy}
            flash={flashProjection === "clarifications"}
          />
          <ArtifactRail
            projectId={projectId}
            artifacts={artifactsQuery.data}
            review={reviewQuery.data}
            state={stateQuery.data}
            flashArtifacts={flashProjection === "artifacts" || flashProjection === "review" || flashProjection === "state"}
          />
        </div>
      </div>

      <Drawer
        open={Boolean(selectedEvent)}
        title={selectedEvent?.title ?? "Событие"}
        onClose={() => setSelectedEvent(null)}
      >
          {selectedEvent ? (
            <TimelineEventDetail
              event={selectedEvent}
              projectId={projectId}
              onOpenAction={onAction}
              onRetryTask={commands.retryTask}
            />
          ) : null}
        </Drawer>

        <Drawer open={Boolean(selectedTask)} title={selectedTask?.title ?? "Задача"} onClose={() => setSelectedTask(null)}>
          {selectedTask ? <TaskNodeDetail task={selectedTask} onRetryTask={commands.retryTask} projectId={projectId} /> : null}
        </Drawer>

        <Modal
          open={Boolean(selectedClarification)}
          title="Ответ на уточнение"
          onClose={() => setSelectedClarification(null)}
        >
          {selectedClarification ? (
            <ClarificationDetailPanel
              clarification={selectedClarification}
              onAnswer={(payload) => {
                commands.answerClarification(payload);
                setSelectedClarification(null);
              }}
              onAcceptAssumption={(clarificationId) => {
                commands.acceptAssumption(clarificationId);
                setSelectedClarification(null);
              }}
              pending={commands.busy}
            />
          ) : null}
        </Modal>
      </>
    );
  }

type ProgressStageStatus = "done" | "active" | "waiting" | "blocked";

interface ProgressStageView {
  id: string;
  label: string;
  description: string;
  status: ProgressStageStatus;
}

const PROGRESS_STAGE_DEFINITIONS = [
  {
    id: "intake",
    label: "Разбор запроса",
    description: "Факты, цель, ограничения",
    artifactRoles: ["request_fact_sheet", "goal_hypothesis", "constraint_inventory", "normalized_request"],
  },
  {
    id: "framing",
    label: "Формализация",
    description: "Границы, стейкхолдеры, варианты",
    artifactRoles: ["business_outcome_model", "scope_boundary_matrix", "stakeholder_map", "solution_option_inventory"],
  },
  {
    id: "domain",
    label: "Доменная проработка",
    description: "ML, интеграции, ИБ, интерфейс",
    artifactRoles: [
      "predictive_problem_definition",
      "data_landscape_assessment",
      "security_compliance_constraints",
      "integration_operating_model",
      "ui_requirements_outline",
    ],
  },
  {
    id: "spec",
    label: "Сборка ТЗ",
    description: "Черновик требований",
    artifactRoles: ["requirements_spec"],
  },
  {
    id: "quality",
    label: "Проверка качества",
    description: "Ревью и замечания",
    artifactRoles: ["review_report"],
  },
] as const;

function pickPrimaryClarification(items: ClarificationItemView[]): ClarificationItemView | null {
  const candidates = items
    .filter((item) => item.status === "open" && item.blocking_scope !== "none")
    .slice()
    .sort((left, right) => {
      const scopeWeight = (scope: string) => ({ objective: 0, subtree: 1, task: 2, none: 3 })[scope] ?? 4;
      const priorityWeight = (priority: string) => ({ critical: 0, high: 1, medium: 2, low: 3 })[priority] ?? 4;
      return scopeWeight(left.blocking_scope) - scopeWeight(right.blocking_scope) || priorityWeight(left.priority) - priorityWeight(right.priority);
    });
  return candidates[0] ?? null;
}

function buildProgressStages(
  artifacts: ArtifactSummaryView[],
  review: ProjectReviewView,
  situation: ProjectSituationView,
): ProgressStageView[] {
  const artifactRoles = new Set(artifacts.map((artifact) => artifact.artifact_role));
  if (review.status !== "missing") {
    artifactRoles.add("review_report");
  }
  const firstOpenIndex = PROGRESS_STAGE_DEFINITIONS.findIndex((stage) => !stage.artifactRoles.some((role) => artifactRoles.has(role)));
  return PROGRESS_STAGE_DEFINITIONS.map((stage, index) => {
    const done = stage.artifactRoles.some((role) => artifactRoles.has(role));
    let status: ProgressStageStatus = "waiting";
    if (done) {
      status = "done";
    } else if (index === Math.max(firstOpenIndex, 0)) {
      status = situation.blocking ? "blocked" : "active";
    }
    return {
      id: stage.id,
      label: stage.label,
      description: stage.description,
      status,
    };
  });
}

function progressTone(status: ProgressStageStatus): "neutral" | "active" | "success" | "warning" | "danger" | "muted" {
  if (status === "done") return "success";
  if (status === "active") return "active";
  if (status === "blocked") return "warning";
  return "muted";
}

function ProjectCockpit({
  situation,
  taskGraph,
  artifacts,
  review,
  clarifications,
  onAction,
  onRunUntilBlocked,
  pending,
  flash,
}: {
  situation: ProjectSituationView;
  taskGraph: ProjectTaskGraphView;
  artifacts: ArtifactSummaryView[];
  review: ProjectReviewView;
  clarifications: ProjectClarificationsView;
  onAction: (action: ActionDescriptor) => void;
  onRunUntilBlocked: () => void;
  pending: boolean;
  flash?: boolean;
}) {
  const stages = buildProgressStages(artifacts, review, situation);
  const completed = taskGraph.completed_leaf_tasks;
  const total = taskGraph.total_leaf_tasks || 1;
  const progress = Math.round((completed / total) * 100);
  const primaryAction = situation.primary_action;
  const canContinue = !situation.blocking;

  return (
    <section className={cx("project-cockpit", situation.blocking && "project-cockpit--blocked", flash && "live-flash")}>
      <div className="project-cockpit__main">
        <div className="project-cockpit__status">
          <StatusPill tone={situation.blocking ? "danger" : situation.status_label === "Готово" ? "success" : "active"}>
            {situation.status_label}
          </StatusPill>
          <span>{situation.blocking ? "Работа остановлена до решения" : "Система может продолжать работу"}</span>
        </div>
        <h2>{situation.headline}</h2>
        <p>{situation.summary}</p>
        <div className="project-cockpit__actions">
          {primaryAction ? (
            <Button tone={situation.blocking ? "danger" : "primary"} icon={situation.blocking ? <AlertTriangle size={16} /> : <Sparkles size={16} />} onClick={() => onAction(primaryAction)}>
              {primaryAction.label}
            </Button>
          ) : null}
          {canContinue ? (
            <Button tone={primaryAction ? "secondary" : "primary"} icon={<Sparkles size={16} />} onClick={onRunUntilBlocked} busy={pending}>
              Выполнить до остановки
            </Button>
          ) : null}
        </div>
      </div>
      <div className="project-cockpit__progress">
        <div className="progress-meter">
          <div className="progress-meter__head">
            <span>Прогресс работ</span>
            <strong>{progress}%</strong>
          </div>
          <div className="progress-meter__bar" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </div>
          <div className="progress-meter__meta">
            <span>{completed} из {taskGraph.total_leaf_tasks} листовых задач завершено</span>
            <span>{artifacts.length} артефактов</span>
            <span>{clarifications.open_count} открытых вопросов</span>
          </div>
        </div>
        <div className="stage-strip" aria-label="Смысловые стадии проекта">
          {stages.map((stage) => (
            <div key={stage.id} className={cx("stage-chip", `stage-chip--${stage.status}`)}>
              <div className="stage-chip__dot" />
              <div>
                <strong>{stage.label}</strong>
                <span>{stage.description}</span>
              </div>
              <StatusPill tone={progressTone(stage.status)}>{prettyLabel(stage.status)}</StatusPill>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function BlockingClarificationPanel({
  clarification,
  onOpenAnswer,
  flash,
}: {
  clarification: ClarificationItemView;
  onOpenAnswer: () => void;
  flash?: boolean;
}) {
  const choices = answerOptions(clarification);
  return (
    <section className={cx("blocking-question", flash && "live-flash")}>
      <div className="blocking-question__intro">
        <StatusPill tone={toneForClarificationPriority(clarification.priority)}>Нужно ваше решение</StatusPill>
        <h2>{clarification.question}</h2>
        <p>{clarificationDescription(clarification)}</p>
      </div>
      <div className="blocking-question__answer-preview">
        <span>Варианты ответа</span>
        {clarification.answer_mode === "multiple" ? <p className="answer-mode-hint">Можно выбрать несколько вариантов.</p> : null}
        <div className="answer-preview-list">
          {choices.slice(0, 3).map((choice) => (
            <div key={choice.option_id} className="answer-preview-item">
              <strong>
                {choice.label}
                {confidenceLabel(choice.confidence) ? <small>{confidenceLabel(choice.confidence)}</small> : null}
              </strong>
              <p>{choice.description}</p>
            </div>
          ))}
        </div>
        <Button tone="primary" icon={<MessageSquareWarning size={16} />} onClick={onOpenAnswer}>
          Ответить в окне
        </Button>
      </div>
      <details className="context-details">
        <summary>Почему система остановилась</summary>
        <div className="context-details__body">
          <p>{clarification.reason}</p>
          <span>Область блокировки: {prettyLabel(clarification.blocking_scope)}</span>
        </div>
      </details>
    </section>
  );
}

function AttentionPanel({
  situation,
  clarifications,
  onAction,
  onOpenClarification,
  onRetryTask,
  retryTaskId,
}: {
  situation: ProjectSituationView;
  clarifications: ProjectClarificationsView;
  onAction: (action: ActionDescriptor) => void;
  onOpenClarification: (clarification: ClarificationItemView) => void;
  onRetryTask: (taskId: string) => void;
  retryTaskId: string | null;
}) {
  const openQuestions = clarifications.items.filter((item) => item.status === "open");
  const hasAttention = situation.blockers.length > 0 || openQuestions.length > 0;
  return (
    <SectionCard
      title="Требует внимания"
      subtitle={hasAttention ? "Решения, которые могут влиять на движение проекта" : "Нет блокирующих действий пользователя"}
      tone={hasAttention ? "warning" : "default"}
    >
      {!hasAttention ? (
        <EmptyState title="Ничего срочного" description="Система может продолжать работу без вашего участия." icon={<CheckCircle2 size={18} />} />
      ) : (
        <div className="attention-list">
          {situation.blockers.slice(0, 3).map((blocker) => (
            <button
              key={`${blocker.kind}-${blocker.related_id ?? blocker.summary}`}
              type="button"
              className="attention-item"
              onClick={() => {
                const clarification = openQuestions.find((item) => item.clarification_id === blocker.related_id);
                if (clarification) {
                  onOpenClarification(clarification);
                  return;
                }
                if (retryTaskId && (blocker.kind === "task_failure" || blocker.kind === "execution_failure")) {
                  onRetryTask(retryTaskId);
                  return;
                }
                onAction({
                  kind: `open_${blocker.detail_view}`,
                  label: blocker.title,
                  description: blocker.summary,
                  target_view: blocker.detail_view,
                  target_id: blocker.related_id,
                  command_name: null,
                  blocking: true,
                });
              }}
            >
              <AlertTriangle size={16} />
              <div>
                <strong>{blocker.title}</strong>
                <p>{blocker.summary}</p>
              </div>
              <ChevronRight size={14} />
            </button>
          ))}
          {openQuestions
            .filter((item) => !situation.blockers.some((blocker) => blocker.related_id === item.clarification_id))
            .slice(0, 3)
            .map((item) => (
              <button key={item.clarification_id} type="button" className="attention-item" onClick={() => onOpenClarification(item)}>
                <MessageSquareWarning size={16} />
                <div>
                  <strong>{item.title}</strong>
                  <p>{item.question}</p>
                </div>
                <ChevronRight size={14} />
              </button>
            ))}
          {situation.primary_action ? (
            <Button tone={situation.blocking ? "danger" : "secondary"} onClick={() => onAction(situation.primary_action!)}>
              {situation.primary_action.label}
            </Button>
          ) : null}
        </div>
      )}
    </SectionCard>
  );
}

function WorkMapSummary({
  tasks,
  onOpenTask,
  flash,
}: {
  tasks: TaskNodeView[];
  onOpenTask: (task: TaskNodeView) => void;
  flash?: boolean;
}) {
  const leafTasks = tasks.filter((task) => task.template_type === "leaf");
  const failed = leafTasks.filter((task) => task.status === "failed");
  const ready = leafTasks.filter((task) => task.status === "ready" || task.is_current);
  const waitingForInput = leafTasks.filter((task) => task.status === "blocked" && task.blocking_clarification_count > 0);
  const waiting = leafTasks.filter(
    (task) => task.status === "blocked" && task.blocking_clarification_count === 0,
  );
  const done = leafTasks.filter((task) => task.status === "completed");
  const visibleTasks = [...failed, ...waitingForInput, ...ready, ...waiting].slice(0, 7);

  return (
    <SectionCard
      title="Карта работ"
      subtitle="Это не порядок выполнения, а состояние зависимостей: задачи могут стартовать нелинейно"
      className={cx("work-map-card", flash && "live-flash")}
      actions={<Link className="text-link" to="../task-graph">Открыть полный граф</Link>}
    >
      <div className="work-map-metrics">
        <div>
          <span>Готово</span>
          <strong>{done.length}</strong>
        </div>
        <div>
          <span>Можно запускать</span>
          <strong>{ready.length}</strong>
        </div>
        <div>
          <span>Ждет ответа</span>
          <strong>{waitingForInput.length}</strong>
        </div>
        <div>
          <span>Ошибки</span>
          <strong>{failed.length}</strong>
        </div>
      </div>
      {visibleTasks.length === 0 ? (
        <EmptyState title="Нет активных задач" description="Завершенные работы видны в истории и полном графе." />
      ) : (
        <div className="work-list">
          {visibleTasks.map((task) => (
            <button key={task.task_id} type="button" className="work-list__item" onClick={() => onOpenTask(task)}>
              <StatusPill tone={taskStatusTone(task)}>{taskStatusLabel(task)}</StatusPill>
              <div>
                <strong>{task.title}</strong>
                <p>{task.status_summary ?? task.template_ref}</p>
              </div>
              <ChevronRight size={14} />
            </button>
          ))}
        </div>
      )}
    </SectionCard>
  );
}

function taskStatusTone(task: TaskNodeView): "neutral" | "active" | "success" | "warning" | "danger" | "muted" {
  if (task.status === "failed") return "danger";
  if (task.blocking_clarification_count > 0) return "warning";
  if (task.status === "ready" || task.is_current) return "active";
  if (task.status === "completed") return "success";
  return "muted";
}

function taskStatusLabel(task: TaskNodeView): string {
  if (task.status === "failed") return "Ошибка";
  if (task.blocking_clarification_count > 0) return "Ждет ответа";
  if (task.status === "ready" || task.is_current) return "Можно запускать";
  return prettyLabel(task.status);
}

function toneForClarificationPriority(priority: string): "neutral" | "active" | "success" | "warning" | "danger" | "muted" {
  switch (priority) {
    case "critical":
      return "danger";
    case "high":
      return "warning";
    case "medium":
      return "active";
    case "low":
      return "muted";
    default:
      return "neutral";
  }
}

function prettyDecisionOwnerRole(role: string): string {
  // Человекочитаемый ярлык владельца решения (W1.2). Должен умещаться в одном
  // слове, чтобы аккуратно сидеть в chip'е рядом с приоритетом.
  switch (role) {
    case "business":
      return "Бизнес";
    case "client":
      return "Заказчик";
    case "methodologist":
      return "Методология";
    case "architect":
      return "Архитектура";
    case "data_owner":
      return "Данные";
    case "security":
      return "ИБ";
    default:
      return prettyLabel(role);
  }
}

function clarificationModeLabel(mode: string): string {
  const labels: Record<string, string> = {
    autopilot: "Автопилот",
    balanced: "Сбалансированный",
    control: "Контроль",
    expert: "Экспертный",
  };
  return labels[mode] ?? mode;
}

interface AnswerChoice {
  option_id: string;
  label: string;
  description: string;
  effect_preview: string;
  confidence?: number | null;
  synthetic?: boolean;
}

function clarificationDescription(clarification: ClarificationItemView): string {
  return (
    clarification.description ||
    [clarification.reason, clarification.impact].filter(Boolean).join(" ") ||
    "Система обнаружила неопределенность, которая может повлиять на дальнейшую работу проекта."
  );
}

function answerOptions(clarification: ClarificationItemView): AnswerChoice[] {
  if (clarification.options.length > 0) {
    return clarification.options;
  }
  return [
    {
      option_id: "synthetic:include_in_current_project",
      label: "Да, учитывать в текущем проекте",
      description: "Эта информация важна для текущего PoC/PoV и должна повлиять на дальнейшую работу.",
      effect_preview: "Ответ будет сохранен как решение проекта и учтен в следующих задачах.",
      confidence: 0.55,
      synthetic: true,
    },
    {
      option_id: "synthetic:use_working_assumption",
      label: "Продолжить с рабочим допущением",
      description: clarification.default_assumption || "Система зафиксирует допущение и продолжит работу без дополнительной детализации.",
      effect_preview: "Допущение попадет в историю проекта.",
      confidence: clarification.default_assumption ? 0.45 : 0.35,
      synthetic: true,
    },
  ];
}

function confidenceLabel(confidence: number | null | undefined): string | null {
  if (confidence === null || confidence === undefined) {
    return null;
  }
  return `Уверенность ${Math.round(confidence * 100)}%`;
}

function ClarificationCenter({
  clarifications,
  highlightedClarificationId,
  onOpenClarification,
  onAcceptAssumption,
  pending,
  flash,
}: {
  clarifications: ProjectClarificationsView;
  highlightedClarificationId?: string;
  onOpenClarification: (clarification: ClarificationItemView) => void;
  onAcceptAssumption: (clarificationId: string) => void;
  pending: boolean;
  flash?: boolean;
}) {
  const visibleItems = clarifications.items
    .filter((item) => (item.status === "open" || item.status === "assumed") && item.clarification_id !== highlightedClarificationId)
    .slice()
    .sort((left, right) => {
      const statusWeight = (status: string) => (status === "open" ? 0 : 1);
      const priorityWeight = (priority: string) => ({ critical: 0, high: 1, medium: 2, low: 3 })[priority] ?? 4;
      return statusWeight(left.status) - statusWeight(right.status) || priorityWeight(left.priority) - priorityWeight(right.priority);
    });

  if (visibleItems.length === 0 && clarifications.mode === "balanced") {
    return null;
  }

  return (
    <SectionCard
      title="Уточнения"
      subtitle={
        clarifications.open_count > 0
          ? `${clarifications.open_count} открытых вопросов, ${clarifications.blocking_count} блокируют работу`
          : "Открытых вопросов нет"
      }
      className={cx(flash && "live-flash")}
    >
      {visibleItems.length === 0 ? (
        <EmptyState title="Вопросов нет" description="Система продолжает работу без участия пользователя." />
      ) : (
        <div className="clarification-list">
          {visibleItems.map((item) => (
            <div key={item.clarification_id} className={cx("clarification-card", item.status === "open" && "clarification-card--open")}>
              <div className="clarification-card__head">
                <div>
                  <span className="clarification-card__eyebrow">
                    {item.status === "open" ? "Открытый вопрос" : "Рабочее допущение"}
                  </span>
                  <strong>{item.question}</strong>
                  <p>{clarificationDescription(item)}</p>
                </div>
                <StatusPill tone={item.status === "open" ? toneForClarificationPriority(item.priority) : "muted"}>
                  {item.status === "open" ? prettyLabel(item.priority) : prettyLabel(item.status)}
                </StatusPill>
              </div>
              <div className="clarification-card__meta">
                <span className={cx("clar-role", `clar-role--${item.decision_owner_role}`)}>
                  {prettyDecisionOwnerRole(item.decision_owner_role)}
                </span>
                <span>{item.blocking_scope === "none" ? "Не блокирует работу" : "Может влиять на дальнейшие шаги"}</span>
                <span>{formatDateTime(item.updated_at)}</span>
              </div>
              {item.default_assumption ? (
                <div className="clarification-assumption">
                  <span>Предложенное допущение</span>
                  <p>{item.default_assumption}</p>
                </div>
              ) : null}
              <div className="inline-actions">
                <Button tone="primary" icon={<MessageSquareWarning size={16} />} onClick={() => onOpenClarification(item)}>
                  {item.status === "open" ? "Ответить" : "Открыть"}
                </Button>
                {item.status === "open" && item.default_assumption ? (
                  <Button tone="secondary" onClick={() => onAcceptAssumption(item.clarification_id)} disabled={pending}>
                    Принять допущение
                  </Button>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
}

function ClarificationDetailPanel({
  clarification,
  onAnswer,
  onAcceptAssumption,
  pending,
}: {
  clarification: ClarificationItemView;
  onAnswer: (payload: { clarification_id: string; selected_option_ids: string[]; free_text?: string }) => void;
  onAcceptAssumption: (clarificationId: string) => void;
  pending: boolean;
}) {
  return (
    <div className="detail-stack clarification-detail">
      <div className="detail-callout">
        <StatusPill tone={toneForClarificationPriority(clarification.priority)}>
          {clarification.status === "open" ? "Нужно решение" : prettyLabel(clarification.status)}
        </StatusPill>
        <span className={cx("clar-role", `clar-role--${clarification.decision_owner_role}`)}>
          {prettyDecisionOwnerRole(clarification.decision_owner_role)}
        </span>
        <span>{formatDateTime(clarification.updated_at)}</span>
      </div>
      <div className="clarification-detail__question">
        <span>Вопрос</span>
        <h3>{clarification.question}</h3>
        <p>{clarificationDescription(clarification)}</p>
      </div>

      {clarification.status === "open" ? (
        <ClarificationAnswerForm
          clarification={clarification}
          onAnswer={onAnswer}
          onAcceptAssumption={onAcceptAssumption}
          pending={pending}
          variant="modal"
        />
      ) : (
        <div className="clarification-resolution">
          <span>Решение</span>
          <p>{clarification.resolution_summary || clarification.default_assumption || "Уточнение закрыто."}</p>
        </div>
      )}
    </div>
  );
}

function ClarificationAnswerForm({
  clarification,
  onAnswer,
  onAcceptAssumption,
  pending,
  variant,
}: {
  clarification: ClarificationItemView;
  onAnswer: (payload: { clarification_id: string; selected_option_ids: string[]; free_text?: string }) => void;
  onAcceptAssumption: (clarificationId: string) => void;
  pending: boolean;
  variant: "focus" | "modal";
}) {
  const choices = answerOptions(clarification);
  const canSelectMultiple = clarification.answer_mode === "multiple";
  const [selectedOptionIds, setSelectedOptionIds] = useState<string[]>(
    clarification.recommended_option_id ? [clarification.recommended_option_id] : [choices[0]?.option_id ?? ""].filter(Boolean),
  );
  const [freeText, setFreeText] = useState("");
  const [customAnswerSelected, setCustomAnswerSelected] = useState(false);

  useEffect(() => {
    setSelectedOptionIds(clarification.recommended_option_id ? [clarification.recommended_option_id] : [choices[0]?.option_id ?? ""].filter(Boolean));
    setFreeText("");
    setCustomAnswerSelected(false);
  }, [clarification.clarification_id, clarification.recommended_option_id]);

  const toggleOption = (optionId: string) => {
    setCustomAnswerSelected(false);
    setSelectedOptionIds((current) => {
      if (canSelectMultiple) {
        return current.includes(optionId) ? current.filter((item) => item !== optionId) : [...current, optionId];
      }
      return [optionId];
    });
  };
  const selectCustomAnswer = () => {
    setCustomAnswerSelected(true);
    setSelectedOptionIds([]);
  };
  const canAnswer =
    clarification.status === "open" &&
    (customAnswerSelected ? freeText.trim().length > 0 : selectedOptionIds.length > 0);
  const submitAnswer = () => {
    if (customAnswerSelected) {
      onAnswer({
        clarification_id: clarification.clarification_id,
        selected_option_ids: [],
        free_text: freeText.trim(),
      });
      return;
    }
    const backendOptionIds = selectedOptionIds.filter((optionId) =>
      clarification.options.some((option) => option.option_id === optionId),
    );
    const syntheticLabels = selectedOptionIds
      .filter((optionId) => !clarification.options.some((option) => option.option_id === optionId))
      .map((optionId) => choices.find((option) => option.option_id === optionId)?.label)
      .filter((label): label is string => Boolean(label));
    const mergedFreeText = syntheticLabels.join("; ");
    onAnswer({
      clarification_id: clarification.clarification_id,
      selected_option_ids: backendOptionIds,
      free_text: mergedFreeText || undefined,
    });
  };

  return (
    <div className={cx("clarification-answer", `clarification-answer--${variant}`)}>
      {clarification.default_assumption ? (
        <div className="clarification-assumption clarification-assumption--detail">
          <span>Предложенное допущение</span>
          <p>{clarification.default_assumption}</p>
        </div>
      ) : null}

      <div className="field-stack">
        <div className="answer-section-title">
          <span>Возможные варианты ответа</span>
          {canSelectMultiple ? <p>Можно выбрать несколько вариантов, если они одновременно применимы.</p> : <p>Выберите наиболее подходящий вариант или уточните ответ в комментарии.</p>}
        </div>
        <div className="choice-list">
          {choices.map((option) => {
              const selected = selectedOptionIds.includes(option.option_id);
              const confidence = confidenceLabel(option.confidence);
              return (
                <button
                  key={option.option_id}
                  type="button"
                  className={cx("choice-card", selected && "choice-card--selected")}
                  onClick={() => toggleOption(option.option_id)}
                >
                  <strong>
                    {option.label}
                    {confidence ? <small>{confidence}</small> : null}
                  </strong>
                  {option.description ? <span>{option.description}</span> : null}
                  {option.effect_preview ? <p>{option.effect_preview}</p> : null}
                </button>
              );
            })}
        </div>
        <label className={cx("choice-card", "choice-card--input", customAnswerSelected && "choice-card--selected")}>
          <strong>Свой ответ</strong>
          <span>Выберите этот вариант, если предложенные ответы не подходят или требуют замены.</span>
          <input
            value={freeText}
            onFocus={selectCustomAnswer}
            onChange={(event) => {
              setFreeText(event.target.value);
              selectCustomAnswer();
            }}
            placeholder="Напишите ответ обычным языком…"
          />
        </label>
        <div className="inline-actions">
          <Button
            tone="primary"
            disabled={!canAnswer || pending}
            busy={pending}
            onClick={submitAnswer}
          >
            Сохранить ответ
          </Button>
          {clarification.default_assumption ? (
            <Button tone="secondary" disabled={pending} onClick={() => onAcceptAssumption(clarification.clarification_id)}>
              Принять допущение
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function TimelineEventDetail({
  event,
  projectId,
  onOpenAction,
  onRetryTask,
}: {
  event: TimelineEntryView;
  projectId: string;
  onOpenAction: (action: ActionDescriptor) => void;
  onRetryTask: (taskId: string) => void;
}) {
  const navigate = useNavigate();
  const detailAction: ActionDescriptor = {
    kind: `open_${event.detail_view}`,
    label: "Открыть связанную сущность",
    description: "Перейти в соответствующий раздел проекта.",
    target_view: event.detail_view,
    target_id: event.entity_type === "artifact" ? event.entity_id : null,
    command_name: null,
    blocking: false,
  };
  return (
    <div className="detail-stack">
      <div className="detail-callout">
        <StatusPill tone={toneForSemanticStatus(event.status)}>
          {prettyLabel(event.status)}
        </StatusPill>
        <span>{formatDateTime(event.created_at)}</span>
      </div>
      <p>{event.summary}</p>
      <div className="detail-meta-list">
        <div>
          <span>Тип события</span>
          <strong>{prettyLabel(event.kind)}</strong>
        </div>
        <div>
          <span>Связанная сущность</span>
          <strong>{prettyLabel(event.entity_type)}</strong>
        </div>
        <div>
          <span>Экран деталей</span>
          <strong>{prettyLabel(event.detail_view)}</strong>
        </div>
      </div>
      <div className="inline-actions">
        <Button tone="primary" onClick={() => onOpenAction(detailAction)}>
          Открыть связанный экран
        </Button>
        {event.kind === "task_failed" && event.entity_id ? (
          <Button tone="secondary" icon={<RefreshCcw size={16} />} onClick={() => onRetryTask(event.entity_id!)}>
            Повторить шаг
          </Button>
        ) : null}
        <Button tone="secondary" onClick={() => navigate(`/projects/${projectId}/task-graph`)}>
          Открыть граф задач
        </Button>
      </div>
    </div>
  );
}

function TaskNodeDetail({
  task,
  onRetryTask,
  projectId,
}: {
  task: TaskNodeView;
  onRetryTask: (taskId: string) => void;
  projectId?: string;
}) {
  return (
    <div className="detail-stack">
      <div className="detail-callout">
        <StatusPill
          tone={
            task.status === "completed"
              ? "success"
              : task.status === "failed" || task.status === "blocked"
                ? "danger"
                : task.is_current
                  ? "active"
                  : "muted"
          }
        >
          {prettyLabel(task.status)}
        </StatusPill>
        <span>{prettyLabel(task.template_type)}</span>
      </div>
      {task.status_summary ? <p>{task.status_summary}</p> : null}
        <div className="detail-meta-list">
        <div>
          <span>Шаблон</span>
          <strong>{task.template_ref}</strong>
        </div>
        <div>
          <span>Источник</span>
          <strong>{labelForSourceKind(task.origin_kind)}</strong>
        </div>
          <div>
            <span>Ref источника</span>
            <strong>{task.origin_ref}</strong>
          </div>
          <div>
            <span>Слот</span>
            <strong>{task.slot_id ?? "—"}</strong>
          </div>
        </div>
        {task.retryable ? (
          <div className="inline-actions">
            <Button tone="secondary" icon={<RefreshCcw size={16} />} onClick={() => onRetryTask(task.task_id)}>
              Повторить шаг
            </Button>
          </div>
        ) : null}
        {projectId && task.template_type === "leaf" && task.status === "completed" ? (
          <ReasoningPanel projectId={projectId} taskId={task.task_id} />
        ) : null}
      </div>
    );
  }

function ArtifactsPage({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const { artifactId } = useParams();
  const artifactsQuery = useQuery({
    queryKey: projectionKey(projectId, "artifacts"),
    queryFn: () => api.getArtifacts(projectId),
  });
  const artifactDetailQuery = useQuery({
    queryKey: [projectId, "artifact-detail", artifactId],
    queryFn: () => api.getArtifactDetail(projectId, artifactId!),
    enabled: Boolean(artifactId),
  });

  if (artifactsQuery.isLoading) {
    return <LoadingPanel title="Загрузка артефактов…" />;
  }

  const artifacts = artifactsQuery.data ?? [];

  return (
    <div className={cx("artifacts-layout", artifactId && "artifacts-layout--focused")}>
      <SectionCard title="Артефакты проекта" subtitle="Документы и промежуточные результаты workflow">
        {artifacts.length === 0 ? (
          <EmptyState title="Артефакты отсутствуют" description="Запустите workflow, чтобы получить первые результаты." />
        ) : (
          <div className="artifact-list">
            {artifacts.map((artifact) => (
              <button
                key={artifact.artifact_id}
                type="button"
                className={cx("artifact-list__item", artifactId === artifact.artifact_id && "artifact-list__item--active")}
                onClick={() => navigate(`/projects/${projectId}/artifacts/${artifact.artifact_id}`)}
              >
                <div>
                  <strong>{artifact.title}</strong>
                  <p>{prettyLabel(artifact.artifact_role)}</p>
                </div>
                <div className="artifact-list__meta">
                  <span>{formatDateTime(artifact.created_at)}</span>
                  <ChevronRight size={14} />
                </div>
              </button>
            ))}
          </div>
        )}
      </SectionCard>

      <SectionCard
        title={artifactDetailQuery.data?.title ?? "Выберите артефакт"}
        subtitle={artifactDetailQuery.data?.description ?? "Читабельный документ и структурированные данные"}
      >
        {!artifactId ? (
          <EmptyState
            title="Выберите артефакт"
            description="Откройте один из артефактов слева, чтобы посмотреть документ, JSON и результаты проверок."
            icon={<FileJson2 size={18} />}
          />
        ) : artifactDetailQuery.isLoading ? (
          <div className="skeleton-stack">
            <div className="skeleton skeleton--line skeleton--lg" />
            <div className="skeleton skeleton--line" />
            <div className="skeleton skeleton--line skeleton--sm" />
          </div>
        ) : artifactDetailQuery.data ? (
          <ArtifactDetailPanel detail={artifactDetailQuery.data} projectId={projectId} />
        ) : (
          <EmptyState title="Артефакт недоступен" description="Не удалось загрузить детальную карточку артефакта." />
        )}
      </SectionCard>
    </div>
  );
}

function ArtifactDetailPanel({ detail, projectId }: { detail: ArtifactDetailView; projectId: string }) {
  const [mode, setMode] = useState<"doc" | "json" | "validations">("doc");
  const [provenanceOpen, setProvenanceOpen] = useState(false);
  const html = useMemo(
    () => (detail.markdown_content ? marked.parse(detail.markdown_content) : "<p>Markdown-представление отсутствует.</p>"),
    [detail.markdown_content],
  );
  const traceQuery = useQuery({
    queryKey: [projectId, "methodology-trace", detail.created_by_task_id],
    queryFn: () => api.getMethodologyTrace(projectId, detail.created_by_task_id!),
    enabled: provenanceOpen && Boolean(detail.created_by_task_id),
  });

  return (
    <div className="artifact-detail">
      <div className="segmented">
        <button className={cx("segmented__item", mode === "doc" && "segmented__item--active")} onClick={() => setMode("doc")} type="button">
          Документ
        </button>
        <button className={cx("segmented__item", mode === "json" && "segmented__item--active")} onClick={() => setMode("json")} type="button">
          JSON
        </button>
        <button
          className={cx("segmented__item", mode === "validations" && "segmented__item--active")}
          onClick={() => setMode("validations")}
          type="button"
        >
          Проверки
        </button>
        {detail.created_by_task_id ? (
          <button
            className="segmented__item"
            onClick={() => setProvenanceOpen(true)}
            type="button"
            title="Откуда пришёл этот артефакт"
          >
            Provenance
          </button>
        ) : null}
      </div>
      <div className="detail-meta-list detail-meta-list--artifact">
        <div>
          <span>Роль</span>
          <strong>{prettyLabel(detail.artifact_role)}</strong>
        </div>
        <div>
          <span>Создан</span>
          <strong>{formatDateTime(detail.created_at)}</strong>
        </div>
        <div>
          <span>Задача</span>
          <strong>{detail.created_by_task_id ?? "—"}</strong>
        </div>
      </div>
      <Modal open={provenanceOpen} onClose={() => setProvenanceOpen(false)} title="Provenance / откуда это">
        {traceQuery.isLoading ? (
          <LoadingPanel title="Грузим provenance…" />
        ) : traceQuery.data ? (
          <ProvenanceViewer data={traceQuery.data} />
        ) : (
          <EmptyState title="Provenance недоступен" description="Не удалось получить methodology-trace для задачи-производителя." />
        )}
      </Modal>
      {mode === "doc" ? (
        <article className="document-surface" dangerouslySetInnerHTML={{ __html: html }} />
      ) : null}
      {mode === "json" ? <pre className="code-block">{detail.json_content}</pre> : null}
      {mode === "validations" ? (
        <div className="validation-list">
          {detail.validations.length === 0 ? (
            <EmptyState title="Проверок пока нет" description="Проверки появятся после выполнения валидационных шагов." />
          ) : (
            detail.validations.map((validation) => (
              <article key={validation.validation_run_id} className="validation-card">
                <div className="validation-card__head">
                  <StatusPill tone={toneForSemanticStatus(validation.status)}>
                    {prettyLabel(validation.status)}
                  </StatusPill>
                  <span>{formatDateTime(validation.created_at)}</span>
                </div>
                {validation.finding_messages.map((message) => (
                  <p key={message}>{message}</p>
                ))}
              </article>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

function flattenTaskNodes(nodes: TaskNodeView[]): TaskNodeView[] {
  return nodes.flatMap((node) => [node, ...flattenTaskNodes(node.children)]);
}

function TaskGraphPage({ projectId }: { projectId: string }) {
  // W4.2 (G1): canvas-based task graph через ReactFlow + dagre.
  // Кликнул на узел → открывается drawer с тем же TaskNodeDetail,
  // что и на L2 Activity, плюс панель «Рассуждение» внутри.
  const [provider] = useStoredState("povgen.provider", "openrouter");
  const [model] = useStoredState("povgen.model", "deepseek/deepseek-v4-flash");
  const [selectedTask, setSelectedTask] = useState<TaskNodeView | null>(null);
  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
  });
  const retryMutation = useMutation({
    mutationFn: (taskId: string) => api.retryTask(projectId, taskId, provider, model),
  });

  if (taskGraphQuery.isLoading || !taskGraphQuery.data) {
    return <LoadingPanel title="Загрузка графа задач…" />;
  }

  const data = taskGraphQuery.data;
  return (
    <SectionCard
      title="Граф задач"
      subtitle={`Завершено ${data.completed_leaf_tasks} из ${data.total_leaf_tasks} листовых задач`}
    >
      <TaskGraphCanvas tree={data.nodes} onSelectNode={setSelectedTask} />
      <Drawer
        open={Boolean(selectedTask)}
        title={selectedTask?.title ?? "Задача"}
        onClose={() => setSelectedTask(null)}
      >
        {selectedTask ? (
          <TaskNodeDetail
            task={selectedTask}
            projectId={projectId}
            onRetryTask={(taskId) => retryMutation.mutate(taskId)}
          />
        ) : null}
      </Drawer>
    </SectionCard>
  );
}

function StatePage({
  projectId,
  actions,
}: {
  projectId: string;
  actions: WorkspaceActionApi;
}) {
  const stateQuery = useQuery({
    queryKey: projectionKey(projectId, "state"),
    queryFn: () => api.getState(projectId),
  });
  const packsQuery = useQuery({
    queryKey: ["registry", "domain-packs"],
    queryFn: api.listDomainPacks,
  });
  const [goalDraft, setGoalDraft] = useState("");

  useEffect(() => {
    if (stateQuery.data?.goal) {
      setGoalDraft(stateQuery.data.goal);
    }
  }, [stateQuery.data?.goal]);

  if (stateQuery.isLoading || !stateQuery.data) {
    return <LoadingPanel title="Загрузка состояния проекта…" />;
  }

  const state = stateQuery.data;
  const enabledPackRefs = new Set(
    state.active_domain_packs.map((item) => String(item.ref ?? item.pack_ref ?? item.identifier ?? "")),
  );
  const availablePacks = (packsQuery.data ?? []).filter((pack) => !enabledPackRefs.has(pack.pack_ref));

  return (
    <div className="state-layout">
      <SectionCard title="Цель и ключевое состояние" subtitle="Ручные действия оператора по состоянию проекта">
        <div className="field-stack">
          <label className="field field--stacked">
            <span>Цель проекта</span>
            <textarea
              rows={4}
              value={goalDraft}
              onChange={(event) => setGoalDraft(event.target.value)}
              placeholder="Зафиксируйте цель проекта на понятном языке."
            />
          </label>
          <div className="inline-actions">
            <Button tone="primary" icon={<PencilLine size={16} />} onClick={() => actions.setGoal(goalDraft)}>
              Сохранить цель
            </Button>
          </div>
        </div>
      </SectionCard>

      <SectionCard
        title="Уточнения, допущения и решения"
        subtitle="Что система спросила, что приняла как рабочее допущение и какие решения уже зафиксированы"
      >
        <div className="state-mini-grid">
          <div className="mini-metric">
            <span>Режим уточнений</span>
            <strong>{clarificationModeLabel(state.clarification_mode)}</strong>
          </div>
          <div className="mini-metric">
            <span>Допущения</span>
            <strong>{state.assumptions.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Решения</span>
            <strong>{state.decisions.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Факты</span>
            <strong>{state.known_facts.length}</strong>
          </div>
        </div>
        <div className="state-record-grid">
          <StateRecordList
            title="Принятые допущения"
            items={state.assumptions}
            emptyTitle="Допущений пока нет"
            emptyDescription="Они появятся, когда система безопасно продолжит работу без вопроса или оператор подтвердит допущение."
          />
          <StateRecordList
            title="Решения пользователя"
            items={state.decisions}
            emptyTitle="Решений пока нет"
            emptyDescription="Здесь будут ответы на уточнения и другие значимые решения по проекту."
          />
        </div>
      </SectionCard>

      <SectionCard title="Активные gaps" subtitle="Незакрытые пробелы в понимании проекта">
        {state.active_gaps.length === 0 ? (
          <EmptyState title="Активных gaps нет" description="Состояние проекта сейчас выглядит чистым." />
        ) : (
          <div className="entity-list">
            {state.active_gaps.map((gap) => {
              const identifier = String(gap.identifier ?? gap.gap_id ?? "gap");
              return (
                <article key={identifier} className="entity-card">
                  <div className="entity-card__head">
                    <div>
                      <strong>{String(gap.title ?? identifier)}</strong>
                      <p>{String(gap.description ?? "")}</p>
                    </div>
                    <StatusPill tone={String(gap.severity ?? "medium") === "high" ? "danger" : "warning"}>
                      {prettyLabel(String(gap.severity ?? "medium"))}
                    </StatusPill>
                  </div>
                  <div className="entity-card__actions">
                    <Button tone="secondary" onClick={() => actions.closeGap(identifier)}>
                      Закрыть gap
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </SectionCard>

      <SectionCard title="Оси готовности" subtitle="Готовность двигаться дальше по разным осям">
        <div className="entity-list">
          {state.readiness.map((item) => (
            <ReadinessEditor
              key={String(item.dimension ?? item.identifier ?? Math.random())}
              item={item}
              onSave={actions.setReadiness}
            />
          ))}
        </div>
      </SectionCard>

      <SectionCard title="Доменные пакеты" subtitle="Доменные расширения, которые добавляют задачи в нужные слоты графа">
        {availablePacks.length === 0 ? (
          <EmptyState title="Новых пакетов нет" description="Все доступные доменные пакеты уже подключены или отсутствуют." />
        ) : (
          <div className="entity-list">
            {availablePacks.map((pack) => (
              <article key={pack.pack_ref} className="entity-card">
                <div className="entity-card__head">
                  <div>
                    <strong>{pack.name}</strong>
                    <p>{pack.description}</p>
                  </div>
                  <StatusPill tone="active">{pack.domain}</StatusPill>
                </div>
                <div className="entity-card__actions">
                  <Button tone="primary" onClick={() => actions.enableDomainPack(pack.pack_ref)}>
                    Подключить пакет
                  </Button>
                </div>
              </article>
            ))}
          </div>
        )}
      </SectionCard>

      <SectionCard title="Корень графа задач" subtitle="Техническая привязка текущего состояния к runtime-графу">
        <pre className="code-block">{JSON.stringify({ root_task_id: state.root_task_id }, null, 2)}</pre>
      </SectionCard>
    </div>
  );
}

function StateRecordList({
  title,
  items,
  emptyTitle,
  emptyDescription,
}: {
  title: string;
  items: Record<string, unknown>[];
  emptyTitle: string;
  emptyDescription: string;
}) {
  return (
    <div className="state-record-list">
      <h3>{title}</h3>
      {items.length === 0 ? (
        <EmptyState title={emptyTitle} description={emptyDescription} />
      ) : (
        <div className="entity-list">
          {items.map((item, index) => {
            const identifier = String(item.identifier ?? `record-${index}`);
            return (
              <article key={identifier} className="entity-card">
                <div className="entity-card__head">
                  <div>
                    <strong>{identifier}</strong>
                    <p>{String(item.statement ?? item.description ?? "")}</p>
                  </div>
                  <StatusPill tone="muted">{String(item.source ?? "system")}</StatusPill>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ReadinessEditor({
  item,
  onSave,
}: {
  item: Record<string, unknown>;
  onSave: (payload: { dimension: string; status: string; blocking: boolean; confidence: number }) => void;
}) {
  const [status, setStatus] = useState(String(item.status ?? "missing"));
  const [blocking, setBlocking] = useState(Boolean(item.blocking));
  const [confidence, setConfidence] = useState(String(item.confidence ?? 1));
  const dimension = String(item.dimension ?? "unknown");

  return (
    <article className="entity-card entity-card--readiness">
      <div className="entity-card__head">
        <div>
          <strong>{prettyLabel(dimension)}</strong>
          <p>Текущее состояние: {String(item.status ?? "missing")}</p>
        </div>
        <StatusPill tone={toneForSemanticStatus(status)}>{prettyLabel(status)}</StatusPill>
      </div>
      <div className="readiness-editor">
        <label className="field">
          <span>Статус</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="missing">missing</option>
            <option value="ready">ready</option>
            <option value="waived">waived</option>
            <option value="partial">partial</option>
          </select>
        </label>
        <label className="field">
          <span>Уверенность</span>
          <input value={confidence} onChange={(event) => setConfidence(event.target.value)} />
        </label>
        <label className="checkbox-field">
          <input checked={blocking} onChange={(event) => setBlocking(event.target.checked)} type="checkbox" />
          <span>Блокирующая ось готовности</span>
        </label>
        <Button
          tone="secondary"
          onClick={() =>
            onSave({
              dimension,
              status,
              blocking,
              confidence: Number.parseFloat(confidence) || 1,
            })
          }
        >
          Сохранить
        </Button>
      </div>
    </article>
  );
}

function ReviewPage({ projectId }: { projectId: string }) {
  const reviewQuery = useQuery({
    queryKey: projectionKey(projectId, "review"),
    queryFn: () => api.getReview(projectId),
  });

  if (reviewQuery.isLoading || !reviewQuery.data) {
    return <LoadingPanel title="Загрузка ревью…" />;
  }

  const review = reviewQuery.data;
  return (
    <div className="review-layout">
      <SectionCard
        title="Итоги ревью"
        subtitle="Ключевой экран для принятия решения по качеству результата"
        tone={review.status === "needs_changes" ? "warning" : review.status === "passed" ? "accent" : "default"}
      >
        {review.status === "missing" ? (
          <EmptyState title="Ревью пока не выполнено" description="Отчёт ревью появится после review-шага workflow." />
        ) : (
          <div className="review-summary">
            <div className="review-summary__head">
              <StatusPill tone={review.status === "passed" ? "success" : "warning"}>{prettyLabel(review.status)}</StatusPill>
              <span>{review.updated_at ? formatDateTime(review.updated_at) : "—"}</span>
            </div>
            <h3>{review.summary ?? "Сводка ревью отсутствует."}</h3>
            {review.strengths.length > 0 ? (
              <div className="check-list">
                <h4>Сильные стороны</h4>
                {review.strengths.map((strength) => (
                  <div key={strength} className="check-list__item">
                    <CheckCircle2 size={16} />
                    <span>{strength}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        )}
      </SectionCard>

      <SectionCard title="Замечания" subtitle="Найденные проблемы и причины блокировки">
        {review.issues.length === 0 ? (
          <EmptyState title="Замечаний нет" description="На текущем ревью блокирующие findings не обнаружены." />
        ) : (
          <div className="issue-list">
            {review.issues.map((issue, index) => (
              <article key={`${issue.message}-${index}`} className="issue-card">
                <div className="issue-card__head">
                  <StatusPill tone={issue.severity === "high" ? "danger" : "warning"}>
                    {prettyLabel(issue.severity)}
                  </StatusPill>
                </div>
                <p>{issue.message}</p>
              </article>
            ))}
          </div>
        )}
      </SectionCard>

      <SectionCard title="Рекомендации" subtitle="Что система рекомендует сделать дальше">
        {review.recommendations.length === 0 ? (
          <EmptyState title="Рекомендации отсутствуют" description="Дополнительные рекомендации не сформированы." />
        ) : (
          <div className="recommendations-list">
            {review.recommendations.map((recommendation) => (
              <div key={recommendation} className="recommendation-item">
                <ChevronRight size={16} />
                <span>{recommendation}</span>
              </div>
            ))}
          </div>
        )}
      </SectionCard>
    </div>
  );
}

function DebugPage({ projectId, onRetryTask }: { projectId: string; onRetryTask: (taskId: string) => void }) {
  const debugQuery = useQuery({
    queryKey: projectionKey(projectId, "debug"),
    queryFn: () => api.getDebug(projectId),
  });

  if (debugQuery.isLoading || !debugQuery.data) {
    return <LoadingPanel title="Загрузка технических деталей…" />;
  }

  const debug = debugQuery.data;
  return (
    <div className="debug-layout">
      <SectionCard title="Сводка runtime" subtitle="Жизненный цикл задач, исполнения, проверки и трассировки">
        <div className="state-mini-grid">
          <div className="mini-metric">
            <span>Tasks</span>
            <strong>{debug.tasks.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Task events</span>
            <strong>{debug.task_events.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Исполнения</span>
            <strong>{debug.execution_runs.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Проверки</span>
            <strong>{debug.validation_runs.length}</strong>
          </div>
        </div>
      </SectionCard>

      <SectionCard title="Tasks" subtitle="Текущие и завершённые задачи">
        <div className="debug-table">
          {debug.tasks.map((task) => {
            const taskId = String(task.task_id ?? "task");
            const status = String(task.status ?? "unknown");
            return (
              <article key={taskId} className="debug-row">
                <div>
                  <strong>{String(task.title ?? task.task_key ?? taskId)}</strong>
                  <p>{String(task.template_ref ?? "")}</p>
                </div>
                <div className="debug-row__actions">
                  <StatusPill tone={toneForSemanticStatus(status)}>
                    {prettyLabel(status)}
                  </StatusPill>
                  {status === "failed" ? (
                    <Button tone="secondary" onClick={() => onRetryTask(taskId)}>
                      Повторить
                    </Button>
                  ) : null}
                </div>
              </article>
            );
          })}
        </div>
      </SectionCard>

      <JsonSection title="История планирования" icon={<Waypoints size={16} />} payload={debug.planning_history} />
      <JsonSection title="Исполнения" icon={<Sparkles size={16} />} payload={debug.execution_runs} />
      <JsonSection title="Трассировки исполнения" icon={<TerminalSquare size={16} />} payload={debug.execution_traces} />
      <JsonSection title="Контекстные манифесты" icon={<Layers3 size={16} />} payload={debug.context_manifests} />
      <JsonSection title="Проверки" icon={<ShieldAlert size={16} />} payload={debug.validation_runs} />
      <JsonSection title="Эскалации" icon={<AlertTriangle size={16} />} payload={debug.escalations} />
    </div>
  );
}

function JsonSection({ title, icon, payload }: { title: string; icon: ReactNode; payload: unknown }) {
  const [expanded, setExpanded] = useState(false);
  const itemCount = Array.isArray(payload)
    ? payload.length
    : payload && typeof payload === "object"
      ? Object.keys(payload as Record<string, unknown>).length
      : 0;
  return (
    <SectionCard
      title={title}
      subtitle={itemCount > 0 ? `Элементов: ${itemCount}` : "Данных пока нет"}
      actions={
        <div className="inline-actions">
          <span className="section-card__icon">{icon}</span>
          <Button tone="ghost" onClick={() => setExpanded((current) => !current)}>
            {expanded ? "Свернуть" : "Развернуть"}
          </Button>
        </div>
      }
    >
      {expanded ? (
        <pre className="code-block">{JSON.stringify(payload, null, 2)}</pre>
      ) : (
        <div className="json-preview">
          <p>Раздел свернут, чтобы не перегружать экран техническими деталями.</p>
        </div>
      )}
    </SectionCard>
  );
}

function CreateProjectModal({
  open,
  onClose,
  onSubmit,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (payload: {
    name: string;
    objective_ref: string;
    request_text: string;
    domain_pack_refs: string[];
  }) => void;
  busy: boolean;
}) {
  // L6-3: paste/upload first (P6). Большая textarea — главный объект формы.
  // Название, цель, доменные пакеты — за «Дополнительно». Если пользователь
  // не ввёл название — система генерирует из первой строки или даты.
  const objectivesQuery = useQuery({
    queryKey: ["registry", "objectives"],
    queryFn: api.listObjectives,
    enabled: open,
  });
  const packsQuery = useQuery({
    queryKey: ["registry", "domain-packs"],
    queryFn: api.listDomainPacks,
    enabled: open,
  });

  const [name, setName] = useState("");
  const [requestText, setRequestText] = useState("");
  const [objectiveRef, setObjectiveRef] = useState("");
  const [selectedPacks, setSelectedPacks] = useState<string[]>([]);
  const [manualPackOverride, setManualPackOverride] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    const firstObjective = objectivesQuery.data?.[0];
    if (firstObjective && !objectiveRef) {
      setObjectiveRef(firstObjective.objective_ref);
    }
  }, [objectiveRef, objectivesQuery.data]);

  useEffect(() => {
    if (!open) {
      setName("");
      setRequestText("");
      setObjectiveRef("");
      setSelectedPacks([]);
      setManualPackOverride(false);
      setAdvancedOpen(false);
      setDragOver(false);
    }
  }, [open]);

  const togglePack = (packRef: string) => {
    setManualPackOverride(true);
    setSelectedPacks((current) =>
      current.includes(packRef) ? current.filter((item) => item !== packRef) : [...current, packRef],
    );
  };

  const handleFileChosen = async (file: File | null | undefined) => {
    if (!file) return;
    try {
      const text = await file.text();
      // Append (а не replace), чтобы пользователь мог накопить материал.
      setRequestText((current) =>
        current.trim()
          ? `${current.trim()}\n\n--- ${file.name} ---\n${text}`
          : text,
      );
    } catch (error) {
      // на крайний случай — игнорируем; пользователь увидит что текст не вставился
      console.error("file read failed", error);
    }
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOver(false);
    const file = event.dataTransfer?.files?.[0];
    if (file) void handleFileChosen(file);
  };

  const handleAppendPaste = async () => {
    try {
      const clipboardText = await navigator.clipboard?.readText?.();
      if (clipboardText) {
        setRequestText((current) =>
          current.trim() ? `${current.trim()}\n\n${clipboardText}` : clipboardText,
        );
      }
    } catch {
      // clipboard API недоступен — игнорируем
    }
  };

  const deriveName = (): string => {
    if (name.trim()) return name.trim();
    const firstLine = requestText.split(/\r?\n/).map((s) => s.trim()).find(Boolean);
    if (firstLine) return firstLine.slice(0, 80);
    return `Проект ${new Date().toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" })}`;
  };

  const requestCharCount = requestText.trim().length;
  const canSubmit = requestText.trim().length > 0 && Boolean(objectiveRef);

  return (
    <Modal open={open} title="Новый проект" onClose={onClose}>
      <form
        className="form-stack create-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!canSubmit) return;
          onSubmit({
            name: deriveName(),
            objective_ref: objectiveRef,
            request_text: requestText,
            domain_pack_refs: selectedPacks,
          });
        }}
      >
        <div className="create-form__intro">
          <p className="create-form__lead">
            Вставьте сюда всё, что есть про задачу: бриф, письмо, описание системы,
            ответы заказчика, протокол встречи. Можно несколько кусков подряд.
          </p>
        </div>

        <div
          className={cx("create-form__paste-zone", dragOver && "create-form__paste-zone--drag")}
          onDragOver={(event) => {
            event.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          <textarea
            className="create-form__textarea"
            rows={14}
            value={requestText}
            onChange={(event) => setRequestText(event.target.value)}
            placeholder={
              "Например:\n\nК нам пришёл запрос на CRM-интеграцию для отдела продаж...\n\nИз письма заказчика: «нужно подключить нашу систему к Salesforce, чтобы менеджеры видели свои сделки в одном окне».\n\nИз встречи: упомянули миграцию ~50k клиентов, MVP к сентябрю, бюджет на интеграцию."
            }
          />
          <div className="create-form__paste-actions">
            <label className="create-form__file-button">
              <input
                type="file"
                accept=".txt,.md,.rst,.log,text/*"
                onChange={(event) => void handleFileChosen(event.target.files?.[0] ?? null)}
                hidden
              />
              <span>📎 Загрузить файл</span>
            </label>
            <button
              type="button"
              className="create-form__paste-button"
              onClick={handleAppendPaste}
              title="Вставить из буфера обмена (добавит в конец)"
            >
              📋 Вставить
            </button>
            <span className="create-form__counter">
              {requestCharCount > 0 ? `${requestCharCount} символов` : "пока пусто"}
            </span>
          </div>
        </div>

        <button
          type="button"
          className="create-form__advanced-toggle"
          onClick={() => setAdvancedOpen((v) => !v)}
          aria-expanded={advancedOpen}
        >
          {advancedOpen ? "▾ Скрыть дополнительные настройки" : "▸ Дополнительные настройки"}
        </button>

        {advancedOpen && (
          <div className="create-form__advanced">
            <label className="field field--stacked">
              <span>Название проекта</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={deriveName()}
              />
              <small className="field__hint">
                Если не заполнить — система возьмёт первую строку запроса или дату.
              </small>
            </label>

            <label className="field field--stacked">
              <span>Цель обработки</span>
              <select value={objectiveRef} onChange={(event) => setObjectiveRef(event.target.value)}>
                {(objectivesQuery.data ?? []).map((objective) => (
                  <option key={objective.objective_ref} value={objective.objective_ref}>
                    {objective.title} · {objective.required_artifact_count} артефакта
                  </option>
                ))}
              </select>
            </label>

            <div className="field field--stacked">
              <span>Доменные пакеты</span>
              <small className="field__hint">
                По умолчанию система подберёт сама по тексту запроса. Ручной выбор —
                как переопределение.
              </small>
              <div className="inline-actions">
                <Button
                  tone={manualPackOverride ? "secondary" : "ghost"}
                  onClick={() => {
                    setManualPackOverride((current) => {
                      const next = !current;
                      if (!next) setSelectedPacks([]);
                      return next;
                    });
                  }}
                >
                  {manualPackOverride ? "Скрыть ручной выбор" : "Выбрать вручную"}
                </Button>
                {manualPackOverride && selectedPacks.length > 0 ? (
                  <StatusPill tone="active">Выбрано: {selectedPacks.length}</StatusPill>
                ) : null}
              </div>
              {manualPackOverride ? (
                <div className="pack-grid pack-grid--modal">
                  {(packsQuery.data ?? []).map((pack) => {
                    const active = selectedPacks.includes(pack.pack_ref);
                    return (
                      <button
                        key={pack.pack_ref}
                        type="button"
                        className={cx("pack-card", active && "pack-card--active")}
                        onClick={() => togglePack(pack.pack_ref)}
                      >
                        <div className="pack-card__head">
                          <strong>{pack.name}</strong>
                          <StatusPill tone={active ? "success" : "muted"}>{pack.domain}</StatusPill>
                        </div>
                        <p>{pack.description}</p>
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>
          </div>
        )}

        <div className="modal__footer">
          <Button tone="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button tone="primary" type="submit" busy={busy} disabled={!canSubmit}>
            Создать проект
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ToastViewport({ toasts }: { toasts: ToastItem[] }) {
  return (
    <div className="toast-viewport" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={cx("toast", `toast--${toast.tone}`)}>
          <strong>{toast.title}</strong>
          <p>{toast.description}</p>
        </div>
      ))}
    </div>
  );
}

function handleAction(
  action: ActionDescriptor,
  projectId: string,
  navigate: ReturnType<typeof useNavigate>,
  commands: WorkspaceActionApi,
) {
  if (action.command_name === "run-next" || action.kind === "run_next") {
    commands.runNext();
    return;
  }
  if (action.command_name === "run-until-blocked" || action.kind === "run_until_blocked") {
    commands.runUntilBlocked();
    return;
  }
  if (action.target_view === "review") {
    navigate(`/projects/${projectId}/review`);
    return;
  }
  if (action.target_view === "artifact" && action.target_id) {
    navigate(`/projects/${projectId}/artifacts/${action.target_id}`);
    return;
  }
  if (action.target_view === "task_graph") {
    navigate(`/projects/${projectId}/task-graph`);
    return;
  }
  if (action.target_view === "state") {
    navigate(`/projects/${projectId}/state`);
    return;
  }
  if (action.target_view === "debug") {
    navigate(`/projects/${projectId}/debug`);
    return;
  }
}

function retryTaskIdForSituation(situation: ProjectSituationView): string | null {
  const blocker = situation.blockers[0];
  if (!blocker || !blocker.related_id) {
    return null;
  }
  if (blocker.kind !== "task_failure" && blocker.kind !== "execution_failure") {
    return null;
  }
  return blocker.related_id;
}

export default App;
