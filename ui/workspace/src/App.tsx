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
import type {
  ActionDescriptor,
  ArtifactDetailView,
  ArtifactSummaryView,
  CommandResultView,
  DomainPackCatalogItemView,
  JourneyStepView,
  ProjectCreatedView,
  ProjectDebugView,
  ProjectJourneyView,
  ProjectReviewView,
  ProjectShellView,
  ProjectSituationView,
  ProjectStateView,
  ProjectTimelineView,
  ProjectionName,
  RecipeCatalogItemView,
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
  JourneyStrip,
  cx,
  formatDateTime,
  prettyLabel,
} from "./ui";

const REALTIME_PROJECTIONS: ProjectionName[] = [
  "shell",
  "journey",
  "situation",
  "timeline",
  "artifacts",
  "review",
  "state",
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
    case "recipe_fragment":
      return "Фрагмент";
    case "base_recipe":
      return "Базовый рецепт";
    default:
      return prettyLabel(sourceKind);
  }
}

function labelForRequirement(required: boolean): string {
  return required ? "Обязательный шаг" : "Опциональный шаг";
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
              ) : firstProject ? (
                <Navigate to={`/projects/${firstProject.project_id}/overview`} replace />
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
  const [provider, setProvider] = useStoredState("povgen.provider", "stub");
  const [model, setModel] = useStoredState("povgen.model", "openai/gpt-4.1-mini");
  const [flashProjection, setFlashProjection] = useState<ProjectionName | null>(null);
  const [commandBusy, setCommandBusy] = useState(false);

  const shellQuery = useQuery({
    queryKey: projectionKey(projectId, "shell"),
    queryFn: () => api.getShell(projectId),
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
      runUntilBlocked: () => void commandRequest(() => api.runUntilBlocked(projectId, provider, model)),
      retryTask: (taskId: string) => void commandRequest(() => api.retryTask(projectId, taskId)),
      setGoal: (text: string) => void commandRequest(() => api.setGoal(projectId, text)),
      closeGap: (gapId: string) => void commandRequest(() => api.closeGap(projectId, gapId)),
      setReadiness: (payload) => void commandRequest(() => api.setReadiness(projectId, payload)),
      enableDomainPack: (packRef: string) => void commandRequest(() => api.enableDomainPack(projectId, packRef)),
      busy: commandBusy,
    }),
    [commandBusy, model, projectId, provider],
  );

  const { status: realtimeStatus } = useProjectRealtime({
    projectId,
    projections: REALTIME_PROJECTIONS,
    onProjectionChanged: (projection) => {
      void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, projection) });
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
        actions={
          <CommandBar
            provider={provider}
            model={model}
            onProviderChange={setProvider}
            onModelChange={setModel}
            onRunNext={commandMutations.runNext}
            onRunUntilBlocked={commandMutations.runUntilBlocked}
            pending={commandMutations.busy}
          />
        }
      />
      <WorkspaceTabs projectId={projectId} />
      <Routes>
        <Route
          path="overview"
          element={
            <OverviewPage
              projectId={projectId}
              flashProjection={flashProjection}
              onAction={(action) => handleAction(action, projectId, navigate, commandMutations)}
            />
          }
        />
        <Route path="artifacts" element={<ArtifactsPage projectId={projectId} />} />
        <Route path="artifacts/:artifactId" element={<ArtifactsPage projectId={projectId} />} />
        <Route path="journey" element={<JourneyPage projectId={projectId} />} />
        <Route path="state" element={<StatePage projectId={projectId} actions={commandMutations} />} />
        <Route path="review" element={<ReviewPage projectId={projectId} />} />
        <Route
          path="debug"
          element={<DebugPage projectId={projectId} onRetryTask={commandMutations.retryTask} />}
        />
        <Route path="*" element={<Navigate to="overview" replace />} />
      </Routes>
    </div>
  );
}

function OverviewPage({
  projectId,
  flashProjection,
  onAction,
}: {
  projectId: string;
  flashProjection: ProjectionName | null;
  onAction: (action: ActionDescriptor) => void;
}) {
  const [selectedEvent, setSelectedEvent] = useState<TimelineEntryView | null>(null);
  const [selectedStep, setSelectedStep] = useState<JourneyStepView | null>(null);

  const journeyQuery = useQuery({
    queryKey: projectionKey(projectId, "journey"),
    queryFn: () => api.getJourney(projectId),
  });
  const situationQuery = useQuery({
    queryKey: projectionKey(projectId, "situation"),
    queryFn: () => api.getSituation(projectId),
  });
  const timelineQuery = useQuery({
    queryKey: projectionKey(projectId, "timeline"),
    queryFn: () => api.getTimeline(projectId),
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
    journeyQuery.isLoading ||
    situationQuery.isLoading ||
    timelineQuery.isLoading ||
    artifactsQuery.isLoading ||
    reviewQuery.isLoading ||
    stateQuery.isLoading
  ) {
    return <LoadingPanel title="Сборка overview…" />;
  }

  if (
    !journeyQuery.data ||
    !situationQuery.data ||
    !timelineQuery.data ||
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

  return (
    <>
      <div className="overview-grid">
        <div className="overview-grid__main">
          <JourneyStrip
            steps={journeyQuery.data.steps}
            onOpenStep={setSelectedStep}
            flash={flashProjection === "journey"}
          />
          <SituationPanel
            situation={situationQuery.data}
            onAction={onAction}
            flash={flashProjection === "situation"}
          />
          <TimelineFeed
            entries={timelineQuery.data.entries}
            onOpenEntry={setSelectedEvent}
            recentSequences={recentSequences}
            flash={flashProjection === "timeline"}
          />
        </div>
        <div className="overview-grid__side">
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
          <TimelineEventDetail event={selectedEvent} projectId={projectId} onOpenAction={onAction} />
        ) : null}
      </Drawer>

      <Drawer open={Boolean(selectedStep)} title={selectedStep?.title ?? "Шаг"} onClose={() => setSelectedStep(null)}>
        {selectedStep ? <JourneyStepDetail step={selectedStep} /> : null}
      </Drawer>
    </>
  );
}

function TimelineEventDetail({
  event,
  projectId,
  onOpenAction,
}: {
  event: TimelineEntryView;
  projectId: string;
  onOpenAction: (action: ActionDescriptor) => void;
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
        <Button tone="secondary" onClick={() => navigate(`/projects/${projectId}/journey`)}>
          Открыть путь выполнения
        </Button>
      </div>
    </div>
  );
}

function JourneyStepDetail({ step }: { step: JourneyStepView }) {
  return (
    <div className="detail-stack">
      <div className="detail-callout">
        <StatusPill tone={step.status === "completed" ? "success" : step.is_current ? "active" : "muted"}>
          {prettyLabel(step.status)}
        </StatusPill>
        <span>{step.required ? "Обязательный шаг" : "Опциональный шаг"}</span>
      </div>
      <div className="detail-meta-list">
        <div>
          <span>Шаблон</span>
          <strong>{step.template_ref}</strong>
        </div>
        <div>
          <span>Источник</span>
          <strong>{labelForSourceKind(step.source_kind)}</strong>
        </div>
        <div>
          <span>Источник</span>
          <strong>{step.source_ref}</strong>
        </div>
      </div>
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
          <ArtifactDetailPanel detail={artifactDetailQuery.data} />
        ) : (
          <EmptyState title="Артефакт недоступен" description="Не удалось загрузить детальную карточку артефакта." />
        )}
      </SectionCard>
    </div>
  );
}

function ArtifactDetailPanel({ detail }: { detail: ArtifactDetailView }) {
  const [mode, setMode] = useState<"doc" | "json" | "validations">("doc");
  const html = useMemo(
    () => (detail.markdown_content ? marked.parse(detail.markdown_content) : "<p>Markdown-представление отсутствует.</p>"),
    [detail.markdown_content],
  );

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

function JourneyPage({ projectId }: { projectId: string }) {
  const journeyQuery = useQuery({
    queryKey: projectionKey(projectId, "journey"),
    queryFn: () => api.getJourney(projectId),
  });

  if (journeyQuery.isLoading || !journeyQuery.data) {
    return <LoadingPanel title="Загрузка пути выполнения…" />;
  }

  return (
    <SectionCard
      title="Ход выполнения"
      subtitle={`Завершено ${journeyQuery.data.completed_steps} из ${journeyQuery.data.total_steps} шагов`}
    >
      <div className="journey-table">
        {journeyQuery.data.steps.map((step) => (
          <article key={step.step_id} className={cx("journey-row", step.is_current && "journey-row--current")}>
            <div className="journey-row__title">
              <strong>{step.title}</strong>
              <p>{step.template_ref}</p>
            </div>
            <div className="journey-row__meta">
              <StatusPill tone={step.status === "completed" ? "success" : step.is_current ? "active" : "muted"}>
                {prettyLabel(step.status)}
              </StatusPill>
              <span>{labelForSourceKind(step.source_kind)}</span>
              <span>{labelForRequirement(step.required)}</span>
            </div>
          </article>
        ))}
      </div>
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
    state.enabled_domain_packs.map((item) => String(item.ref ?? item.pack_ref ?? item.identifier ?? "")),
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

      <SectionCard title="Доменные пакеты" subtitle="Доменные расширения, влияющие на собранный маршрут проекта">
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

      <SectionCard title="Состав маршрута" subtitle="Собранный путь проекта и подключённые фрагменты">
        <pre className="code-block">{JSON.stringify(state.recipe_composition ?? {}, null, 2)}</pre>
      </SectionCard>
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
                  <strong>{String(task.recipe_step_id ?? taskId)}</strong>
                  <p>{String(task.template_id ?? "")}@{String(task.template_version ?? "")}</p>
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
    recipe_ref: string;
    request_text: string;
    domain_pack_refs: string[];
  }) => void;
  busy: boolean;
}) {
  const recipesQuery = useQuery({
    queryKey: ["registry", "recipes"],
    queryFn: api.listRecipes,
    enabled: open,
  });
  const packsQuery = useQuery({
    queryKey: ["registry", "domain-packs"],
    queryFn: api.listDomainPacks,
    enabled: open,
  });

  const [name, setName] = useState("");
  const [requestText, setRequestText] = useState("");
  const [recipeRef, setRecipeRef] = useState("");
  const [selectedPacks, setSelectedPacks] = useState<string[]>([]);

  useEffect(() => {
    const firstRecipe = recipesQuery.data?.[0];
    if (firstRecipe && !recipeRef) {
      setRecipeRef(firstRecipe.recipe_ref);
    }
  }, [recipeRef, recipesQuery.data]);

  useEffect(() => {
    if (!open) {
      setName("");
      setRequestText("");
      setSelectedPacks([]);
    }
  }, [open]);

  const togglePack = (packRef: string) => {
    setSelectedPacks((current) =>
      current.includes(packRef) ? current.filter((item) => item !== packRef) : [...current, packRef],
    );
  };

  return (
    <Modal open={open} title="Новый проект" onClose={onClose}>
      <form
        className="form-stack"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit({
            name,
            recipe_ref: recipeRef,
            request_text: requestText,
            domain_pack_refs: selectedPacks,
          });
        }}
      >
        <label className="field field--stacked">
          <span>Название проекта</span>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Например: ТЗ для нового сервиса" />
        </label>
        <label className="field field--stacked">
          <span>Исходный бизнес-запрос</span>
          <textarea
            rows={6}
            value={requestText}
            onChange={(event) => setRequestText(event.target.value)}
            placeholder="Опишите задачу обычным языком. Система сама проведёт вас по процессу."
          />
        </label>
        <label className="field field--stacked">
          <span>Recipe</span>
          <select value={recipeRef} onChange={(event) => setRecipeRef(event.target.value)}>
            {(recipesQuery.data ?? []).map((recipe) => (
              <option key={recipe.recipe_ref} value={recipe.recipe_ref}>
                {recipe.name} · {recipe.step_count} шагов
              </option>
            ))}
          </select>
        </label>

        <div className="field field--stacked">
          <span>Доменные пакеты</span>
          <div className="pack-grid">
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
        </div>

        <div className="modal__footer">
          <Button tone="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button
            tone="primary"
            type="submit"
            busy={busy}
            disabled={!name.trim() || !requestText.trim() || !recipeRef}
          >
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
  if (action.target_view === "journey") {
    navigate(`/projects/${projectId}/journey`);
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

export default App;
