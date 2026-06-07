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
  useSearchParams,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ClipboardPaste,
  Download,
  FileJson2,
  FileText,
  Layers3,
  Loader2,
  Paperclip,
  Plus,
  RefreshCcw,
  ShieldAlert,
  Sparkles,
  TerminalSquare,
  Trash2,
  Undo2,
  Waypoints,
  X,
  XCircle,
} from "lucide-react";
import { marked } from "marked";
import mermaid from "mermaid";

// Stage 6: рендеринг Mermaid-диаграмм внутри артефактных markdown'ов.
// Инициализация один раз на модуль. `startOnLoad: false` — рендерим явно
// через mermaid.run в useEffect ниже. Тема — dark, чтобы совпадать с
// тёмной палитрой UI.
mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "loose" });

function preprocessMarkdownForMermaid(markdown: string): string {
  // Заменяет fenced ```mermaid блоки на <div class="mermaid-host">… с
  // HTML-экранированным телом. После marked.parse эти div'ы остаются как
  // есть (raw HTML inline-блоки в Markdown сохраняются), и mermaid.run в
  // useEffect превращает их в SVG-диаграммы.
  const re = /```mermaid\s*\n([\s\S]*?)\n```/g;
  return markdown.replace(re, (_match, code: string) => {
    const escaped = code
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    return `<div class="mermaid-host">${escaped}</div>`;
  });
}

import { api } from "./api";
import ErrorBoundary from "./ErrorBoundary";
import { activeRunRefetchInterval } from "./realtime";
import {
  CheckpointSessionPage,
  CheckpointsListPage,
  DecisionsRegistryPage,
  PendingDecisionsPage,
} from "./DecisionsPage";
import { LlmSettingsPage } from "./LlmSettingsPage";
import { ProjectOverviewV2 } from "./ProjectOverviewV2";
import { RequisitesPage } from "./RequisitesPage";
import { ProjectsHomeDashboard } from "./ProjectsHomeDashboard";
import { TaskGraphCanvas } from "./TaskGraphCanvas";
import type {
  ArtifactDetailView,
  ArtifactSummaryView,
  AttachmentView,
  CommandResultView,
  DomainPackCatalogItemView,
  ObjectiveCatalogItemView,
  ProjectCreatedView,
  ProjectDebugView,
  ProjectShellView,
  ProjectionName,
  RollbackPreviewView,
  RollbackResultView,
  TaskNodeView,
} from "./types";
import { useProjectRealtime } from "./useProjectRealtime";
import {
  Button,
  Drawer,
  EmptyState,
  LoadingPanel,
  Modal,
  ProjectRail,
  SectionCard,
  StatusPill,
  WorkspaceHeader,
  WorkspaceTabs,
  cx,
  formatDateTime,
  prettyLabel,
} from "./ui";
import { StageStatusBar, shortStageLabel } from "./StageStatusBar";
import { runStatusVisual, stepStatusVisual, taskStatusVisual } from "./workflowStatus";

const REALTIME_PROJECTIONS: ProjectionName[] = [
  "shell",
  "task_graph",
  "situation",
  "timeline",
  "artifacts",
  "attachments",
  "review",
  "state",
  // C6: aggregated L1 / L2 projections — when these fire, MissionControl
  // and MethodologyPage queries get invalidated automatically.
  "overview",
  "methodology",
  // Степпер этапов (gate stepper) над вкладками — постоянный статус-слой.
  "stages",
  // Прогресс workflow-ранов теперь первоклассная realtime-проекция: запись
  // runner'а меняет realtime_token, WS присылает projection_changed, и
  // run-запросы инвалидируются по пушу — вместо отдельного HTTP-поллинга.
  "workflow_runs",
];

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

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
  setClarificationMode: (mode: string) => void;
  activateNextObjective: (objectiveRef: string) => void;
  notify: (tone: ToastTone, title: string, description: string) => void;
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
  // Меняется при явном закрытии/успехе → форма перемонтируется и очищается.
  // При оптимистичном закрытии на submit ключ НЕ меняем: если создание упадёт,
  // форма откроется снова с сохранённым вводом.
  const [createFormKey, setCreateFormKey] = useState(0);
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
    mutationFn: async (payload: {
      name: string;
      objective_ref: string;
      request_text: string;
      domain_pack_refs: string[];
      files: File[];
    }) => {
      const { files, ...createPayload } = payload;
      const created = await api.createProject(createPayload);
      // Файлы грузим после создания проекта (project_id уже есть). Сбой
      // загрузки одного файла не валит создание проекта.
      const failed: string[] = [];
      for (const file of files) {
        try {
          await api.uploadAttachment(created.project_id, file);
        } catch {
          failed.push(file.name);
        }
      }
      return { created, attachedCount: files.length - failed.length, failed };
    },
    onSuccess: ({ created, attachedCount, failed }) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      const filesNote = attachedCount > 0 ? ` Приложено файлов: ${attachedCount}.` : "";
      notify("success", "Проект создан", `Создан новый кейс «${created.name}».${filesNote}`);
      if (failed.length > 0) {
        notify("danger", "Часть файлов не загрузилась", failed.join(", "));
      }
      // Форма уже закрыта оптимистично на submit; сбрасываем её для следующего раза.
      setCreateFormKey((key) => key + 1);
      navigate(`/projects/${created.project_id}/overview`);
    },
    onError: (error: Error) => {
      // Создание шло в фоне с закрытой формой — возвращаем форму с сохранённым
      // вводом (ключ не меняли), чтобы пользователь не потерял текст запроса.
      notify("danger", "Не удалось создать проект", error.message);
      setCreateOpen(true);
    },
  });

  const selectedProjectId = useMemo(() => {
    const match = location.pathname.match(/\/projects\/([^/]+)/);
    return match?.[1] ?? null;
  }, [location.pathname]);
  const firstProject = projectsQuery.data?.[0] ?? null;

  const deleteProjectMutation = useMutation({
    mutationFn: (project: { project_id: string; name: string }) => api.deleteProject(project.project_id),
    onSuccess: (_result, project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      notify("success", "Проект удалён", `Кейс «${project.name}» удалён без возможности восстановления.`);
      // Если удалили открытый сейчас проект — уходим на главную, чтобы не
      // остаться на «мёртвом» URL (его запросы вернут 404).
      if (selectedProjectId === project.project_id) {
        navigate("/");
      }
    },
    onError: (error: Error) => {
      notify("danger", "Не удалось удалить проект", error.message);
    },
  });

  return (
    <div className="app-shell">
      <ProjectRail
        projects={projectsQuery.data ?? []}
        selectedProjectId={selectedProjectId}
        onCreate={() => setCreateOpen(true)}
        onDeleteProject={(project) => deleteProjectMutation.mutate(project)}
        deletingProjectId={deleteProjectMutation.isPending ? deleteProjectMutation.variables?.project_id ?? null : null}
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
          <Route path="/settings" element={<LlmSettingsPage />} />
          <Route path="/projects/:projectId" element={<Navigate to="overview" replace />} />
          <Route
            path="/projects/:projectId/*"
            element={
              // key={projectId} — при смене проекта boundary перемонтируется
              // и сбрасывает ошибку. Краш одного проекта не гасит оболочку
              // (рельса проектов и навигация остаются доступны).
              <ErrorBoundary
                key={selectedProjectId ?? "workspace"}
                title="Не удалось открыть проект"
                onReset={() => navigate("/")}
              >
                <WorkspaceRoute onCreate={() => setCreateOpen(true)} notify={notify} />
              </ErrorBoundary>
            }
          />
        </Routes>
      </main>

      <CreateProjectModal
        key={createFormKey}
        open={createOpen}
        onClose={() => {
          // Явное закрытие (Отмена/фон) сбрасывает черновик.
          setCreateOpen(false);
          setCreateFormKey((key) => key + 1);
        }}
        onSubmit={(payload) => {
          // Оптимистично закрываем форму сразу: создание (включая LLM-подбор
          // доменных пакетов) идёт в фоне, прогресс — в индикаторе ниже.
          setCreateOpen(false);
          createProjectMutation.mutate(payload);
        }}
        busy={createProjectMutation.isPending}
      />

      {createProjectMutation.isPending ? (
        <div className="create-progress" role="status" aria-live="polite">
          <Loader2 size={16} className="spin" />
          <span>
            Создаётся проект
            {createProjectMutation.variables?.name ? ` «${createProjectMutation.variables.name}»` : ""}…
          </span>
        </div>
      ) : null}

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
  // ВАЖНО: ни provider, ни model больше НЕ передаются из UI.
  // Каждый запуск пайплайна использует системные настройки `/settings`
  // — провайдер и модель резолвятся на сервере через resolve_for_purpose
  // → ModelAssignment → ModelRouting → ProviderConnection.
  //
  // Раньше localStorage хранил provider="openrouter" и
  // model="deepseek/deepseek-v4-flash" дефолтно, и эти значения уходили
  // в API → backend шёл legacy env-path → ошибки про отсутствующие ключи
  // или несуществующие модели.
  //
  // Сейчас оба значения — пустые константы. Стейлые ключи в браузерном
  // localStorage больше не читаются, удаляем их на mount (миграция —
  // ниже, в `useEffect`).
  const provider = "";
  const setProvider = (_: string) => {};
  const model = "";
  const setModel = (_: string) => {};
  useEffect(() => {
    // Очистка стейлых дефолтов: если в localStorage лежит "deepseek/..."
    // или "openrouter" — не нужно, settings-store теперь источник истины.
    try {
      window.localStorage.removeItem("povgen.provider");
      window.localStorage.removeItem("povgen.model");
    } catch {
      /* ignore */
    }
  }, []);
  const [commandBusy, setCommandBusy] = useState(false);

  const shellQuery = useQuery({
    queryKey: projectionKey(projectId, "shell"),
    queryFn: () => api.getShell(projectId),
    enabled: Boolean(projectId),
  });
  // Активный run — общий ключ с WorkflowRunProgressPanel (react-query
  // дедуплицирует). Нужен, чтобы поллинг checkpoints был условным.
  const headerActiveRunQuery = useQuery({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => api.getActiveWorkflowRun(projectId),
    enabled: Boolean(projectId),
    refetchInterval: activeRunRefetchInterval,
  });
  const runActive =
    headerActiveRunQuery.data?.status === "running" ||
    headerActiveRunQuery.data?.status === "pending";
  // v3.0: pending-checkpoint бэйдж в header'е.
  const headerCheckpointsQuery = useQuery({
    queryKey: ["checkpoints-list", projectId],
    queryFn: () => api.getCheckpoints(projectId),
    enabled: Boolean(projectId),
    // Чекпоинты создаются ТОЛЬКО во время run'а, и WS (projection_changed:
    // workflow_runs) уже инвалидирует этот ключ на каждой записи runner'а.
    // Поэтому поллинг — лишь страховка к WS И ТОЛЬКО пока run активен; на
    // простое новых чекпоинтов не появляется → polling off, ноль холостого
    // трафика (тот же принцип, что и для workflow-run-active).
    refetchInterval: runActive ? 5000 : false,
  });
  // v3.1: clarification_mode теперь хранится в Layer A state-снапшоте.
  // Используем его как источник истины для селектора режима в шапке.
  const headerStateQuery = useQuery({
    queryKey: projectionKey(projectId, "state"),
    queryFn: () => api.getState(projectId),
    enabled: Boolean(projectId),
  });
  // Артефакты — чтобы ссылка «Входные артефакты» вела на входной артефакт
  // (роль input.request), а не просто в раздел. Ключ совпадает с разделом
  // артефактов → общий кэш.
  const artifactsQuery = useQuery({
    queryKey: projectionKey(projectId, "artifacts"),
    queryFn: () => api.getArtifacts(projectId),
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
      setClarificationMode: (mode: string) => void commandRequest(() => api.setClarificationMode(projectId, mode)),
      activateNextObjective: (objectiveRef: string) => {
        // Активируем следующий этап и СРАЗУ запускаем его пайплайн
        // (run-until-blocked) — пользователю не нужно отдельно жать «Продолжить».
        setCommandBusy(true);
        api
          .activateNextObjective(projectId, objectiveRef)
          .then(async (result) => {
            for (const projection of result.changed_projections) {
              await queryClient.invalidateQueries({ queryKey: projectionKey(projectId, projection) });
            }
            notify(
              toneForCommandStatus(result.status),
              titleForCommandStatus(result.status),
              result.summary,
            );
            const run = await api.runUntilBlocked(projectId, provider, model);
            notify("success", "Этап запущен", `Шагов запланировано: ${run.max_steps}.`);
            void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
          })
          .catch((error) =>
            notify(
              "danger",
              "Не удалось перейти на следующий этап",
              error instanceof Error ? error.message : "Неизвестная ошибка",
            ),
          )
          .finally(() => setCommandBusy(false));
      },
      notify,
      busy: commandBusy,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      // workflow_runs — первоклассная realtime-проекция. Runner пишет в БД
      // между шагами → realtime_token меняется → приходит этот projection_changed
      // → инвалидируем активный run и список. Прогресс едет по WS-пушу, без
      // отдельного HTTP-поллинга. Привязано именно к этой проекции (а не на
      // каждое событие), чтобы не дёргать run-запросы лишний раз.
      if (projection === "workflow_runs") {
        void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
        void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-runs"] });
        // Граф задач тоже двигается по ходу прогона (start / смена статуса /
        // новый шаг). Без явной инвалидации пульс активной задачи и статусы на
        // графе отставали от прогресса прогона (полл бил только по run).
        void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "task_graph") });
        // Решения и checkpoint'ы меняются по ходу run'а (identification/
        // extraction/финализация сессий) — это тоже двигает realtime_token,
        // поэтому обновляем их в реальном времени, без перезагрузки сайта.
        // Префиксная инвалидация покрывает варианты ключей с фильтрами.
        void queryClient.invalidateQueries({ queryKey: ["decisions", projectId] });
        void queryClient.invalidateQueries({ queryKey: ["pending-decisions", projectId] });
        void queryClient.invalidateQueries({ queryKey: ["checkpoints-list", projectId] });
      }
    },
    onResync: () => {
      // Реконнект: WS был оборван — пропущенные projection_changed уже не
      // придут, поэтому форсированно подтягиваем всё (дыра рассинхрона).
      REALTIME_PROJECTIONS.forEach((p) =>
        queryClient.invalidateQueries({ queryKey: projectionKey(projectId, p) }),
      );
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-runs"] });
      void queryClient.invalidateQueries({ queryKey: ["decisions", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["pending-decisions", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["checkpoints-list", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["methodology-packs"] });
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
        runStatus={<HeaderRunStatus projectId={projectId} />}
      />
      {/* Степпер этапов + живой прогон (тикер + лента) переехали в шапку
          вкладки «Проект» (ProjectOverviewV2.workflowSlot) — над вкладками
          их больше нет. */}
      <WorkspaceTabs
        projectId={projectId}
        pendingDecisionsCount={headerCheckpointsQuery.data?.pending_count}
      />
      <Routes>
        <Route
          path="overview"
          element={
            <ProjectOverviewV2
              projectId={projectId}
              onOpenDecisions={() => navigate(`/projects/${projectId}/decisions`)}
              onOpenArtifactFull={(artifactId) =>
                navigate(`/projects/${projectId}/artifacts/${artifactId}`)
              }
              onContinue={commandMutations.runUntilBlocked}
              onRetryTask={commandMutations.retryTask}
              clarificationMode={headerStateQuery.data?.clarification_mode}
              onClarificationModeChange={commandMutations.setClarificationMode}
              modePending={commandMutations.busy}
              domainPacks={shellQuery.data?.active_domain_packs ?? []}
              onOpenInputArtifacts={() => {
                const input = (artifactsQuery.data ?? []).find(
                  (a) => a.artifact_role === "input.request",
                );
                navigate(
                  input
                    ? `/projects/${projectId}/artifacts/${input.artifact_id}`
                    : `/projects/${projectId}/artifacts`,
                );
              }}
              workflowSlot={
                <StageStatusBar
                  projectId={projectId}
                  onActivateNextObjective={commandMutations.activateNextObjective}
                  activating={commandMutations.busy}
                  onRetryTask={commandMutations.retryTask}
                >
                  <RunActivitySection
                    projectId={projectId}
                    onRetryTask={commandMutations.retryTask}
                  />
                </StageStatusBar>
              }
            />
          }
        />
        {/* v3.1: legacy /mission и /activity удалены — обе страницы
            были построены вокруг ClarificationRequest, который ушёл
            в Decision (v3.0 реестр). */}
        <Route path="artifacts" element={<ArtifactsPage projectId={projectId} />} />
        <Route path="artifacts/:artifactId" element={<ArtifactsPage projectId={projectId} />} />
        <Route path="task-graph" element={<TaskGraphPage projectId={projectId} />} />
        {/* v3.1: legacy /clarifications и /decision-log удалены — Decision
            (v3.0 реестр) полностью покрывает эти сценарии. */}
        {/* «Решения» — раздел с двумя под-вкладками (см. DecisionsSubNav):
            • decisions/pending — «Открытые» (bulk-ответ, цель бейджа)
            • decisions — «Реестр» (справочник + фильтры + PDF) */}
        <Route path="decisions" element={<DecisionsRegistryPage projectId={projectId} />} />
        <Route path="decisions/pending" element={<PendingDecisionsPage projectId={projectId} />} />
        <Route path="requisites" element={<RequisitesPage projectId={projectId} />} />
        {/* Старый плоский URL — редирект, чтобы bookmarks не ломались. */}
        <Route
          path="pending-decisions"
          element={<Navigate to={`/projects/${projectId}/decisions/pending`} replace />}
        />
        <Route path="checkpoints" element={<CheckpointsListPage projectId={projectId} />} />
        <Route
          path="checkpoints/:sessionId"
          element={<CheckpointSessionPage projectId={projectId} />}
        />
        <Route path="methodology" element={<MethodologyPage projectId={projectId} />} />
        {/* Диагностические страницы — прямой доступ по URL.
            Вкладки в WorkspaceTabs больше нет: эти разделы (Состояние /
            Замечания / Технические детали) не являются настройками,
            а ссылка «⚙ Настройки» на их объединение путала с root-level
            страницей `/settings` (LLM-провайдеры). */}
        <Route path="state" element={<StatePage projectId={projectId} actions={commandMutations} />} />
        <Route path="review" element={<ReviewPage projectId={projectId} />} />
        <Route path="debug" element={<DebugPage projectId={projectId} onRetryTask={commandMutations.retryTask} />} />
        {/* Старый объединённый settings-таб больше не используется;
            редиректим на Обзор. Если пользователь сохранил bookmark с
            ?tab=state/review/debug — перенаправляем на прямой URL. */}
        <Route path="settings" element={<SettingsTabRedirect projectId={projectId} />} />
        <Route path="*" element={<Navigate to="overview" replace />} />
      </Routes>
    </div>
  );
}


// ---- RunActivitySection (live run ticker + step feed) --------------------

function SettingsTabRedirect({ projectId }: { projectId: string }) {
  // Старый объединённый settings-таб удалён; bookmarks вида
  // `/projects/:id/settings?tab=state` редиректятся на прямой URL
  // `/projects/:id/state`. Без `?tab=` — на Обзор.
  const [searchParams] = useSearchParams();
  const tab = searchParams.get("tab");
  if (tab === "state") return <Navigate to={`/projects/${projectId}/state`} replace />;
  if (tab === "review") return <Navigate to={`/projects/${projectId}/review`} replace />;
  if (tab === "debug") return <Navigate to={`/projects/${projectId}/debug`} replace />;
  return <Navigate to={`/projects/${projectId}/overview`} replace />;
}


// HeaderRunStatus — компактный статус прогона в шапке проекта (виден на всех
// вкладках): пилюля честного статуса + текущий шаг + «N в работе». Заменяет
// прежний status_label проекта и чип CommandBar. Тяжёлый workflow-блок
// (дорожка этапов + живая лента) остаётся во вкладке «Проект».
function HeaderRunStatus({ projectId }: { projectId: string }) {
  const activeQuery = useQuery({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => api.getActiveWorkflowRun(projectId),
    refetchInterval: activeRunRefetchInterval,
  });
  const recentQuery = useQuery({
    queryKey: [projectId, "workflow-runs"],
    queryFn: () => api.listWorkflowRuns(projectId, 100),
  });
  const active = activeQuery.data ?? null;
  const display = active ?? recentQuery.data?.[0] ?? null;
  // Граф нужен только для счётчика «N в работе» и только во время прогона.
  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
    enabled: Boolean(active),
  });
  if (!display) return null;
  const viz = runStatusVisual(display.status, display.stop_reason);
  const summary = cleanStepSummary(display.last_step_summary);
  let inProgress = 0;
  if (active && taskGraphQuery.data) {
    const walk = (nodes: TaskNodeView[]) => {
      for (const n of nodes) {
        if (n.status === "in_progress") inProgress += 1;
        if (n.children?.length) walk(n.children);
      }
    };
    walk(taskGraphQuery.data.nodes);
  }
  return (
    <span className="header-run">
      <StatusPill tone={viz.tone}>{viz.label}</StatusPill>
      {summary ? <span className="header-run__summary">{summary}</span> : null}
      {inProgress > 0 ? (
        <span className="header-run__count">
          <Loader2 size={12} className="spin" /> {inProgress} в работе
        </span>
      ) : null}
    </span>
  );
}

// RunActivitySection — живая активность прогона внутри StageStatusBar: честный
// статус прогона + ЕДИНАЯ лента времени (сверху — что идёт сейчас с
// секундомером, ниже — завершённые шаги). Один и тот же шаг проживает на
// глазах: «идёт» → «готово/ошибка». Упавший шаг чинится прямо в ленте.
function RunActivitySection({
  projectId,
  onRetryTask,
}: {
  projectId: string;
  onRetryTask?: (taskId: string) => void;
}) {
  const activeQuery = useQuery({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => api.getActiveWorkflowRun(projectId),
    // Прогресс инвалидируется по WS (projection_changed: workflow_runs).
    // Этот полл — тонкая страховка ТОЛЬКО пока run идёт; на простое — off.
    refetchInterval: activeRunRefetchInterval,
  });
  const recentQuery = useQuery({
    queryKey: [projectId, "workflow-runs"],
    // Берём всю историю прогонов: лента шагов должна восстанавливаться с начала
    // проекта, а не показывать только последний прогон после перезагрузки.
    queryFn: () => api.listWorkflowRuns(projectId, 100),
    // Без поллинга: список инвалидируется WS-пушем при изменении ранов.
  });
  // Граф задач — нужен, чтобы показать «сейчас выполняется» (status=in_progress).
  // Запись о шаге в `run.steps` появляется только ПОСЛЕ завершения; поэтому
  // в active-режиме без графа мы не увидим, какая задача крутится прямо сейчас.
  // Без поллинга: task_graph — WS-проекция, инвалидируется пушем на каждой
  // записи runner'а (старт/смена статуса задачи меняют realtime_token).
  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
  });
  const [stickyRunId, setStickyRunId] = useState<string | null>(null);
  // Лента шагов всегда открыта (по запросу) — сворачивания больше нет.
  const navigate = useNavigate();

  const active = activeQuery.data ?? null;
  // Когда run заканчивается, active становится null — но мы хотим
  // показать терминал ещё немного, пока пользователь не закроет.
  const recent = recentQuery.data ?? [];
  const sticky = stickyRunId ? recent.find((r) => r.run_id === stickyRunId) ?? null : null;
  const display = active ?? sticky ?? recent[0] ?? null;

  // Локальная блокировка кнопки «Повторить» в ленте: после нажатия задача
  // помечается «в процессе ретрая», кнопка гаснет — нельзя дёрнуть дважды
  // и видно, что нажатие сработало (задача 2). Снимается при любом обновлении
  // ленты (ретрай зарегистрирован → новый шаг/смена статуса).
  const [retryingIds, setRetryingIds] = useState<Set<string>>(new Set());

  // Полная лента: шаги ВСЕХ прогонов проекта, упорядоченные по времени (с начала
  // проекта). Раньше показывались шаги лишь одного прогона (display.steps),
  // из-за чего после перезагрузки лента «теряла» всё, кроме последнего прогона.
  const allSteps = useMemo(() => {
    const steps = recent.flatMap((run) =>
      run.steps.map((s) => ({ step: s, runId: run.run_id })),
    );
    // Новые сверху: в живой ленте только что завершённый шаг встаёт сразу под
    // блоком «идёт сейчас» — естественный поток активности.
    steps.sort(
      (a, b) =>
        new Date(b.step.started_at).getTime() - new Date(a.step.started_at).getTime(),
    );
    return steps;
  }, [recent]);

  // Лента обновилась (ретрай зарегистрирован) → снимаем локальную блокировку
  // кнопок «Повторить».
  useEffect(() => {
    setRetryingIds((prev) => (prev.size ? new Set() : prev));
  }, [allSteps]);

  // task_id → человеческое имя задачи (в ленте показываем имя, а не id).
  const titleById = useMemo(() => {
    const map = new Map<string, string>();
    const walk = (nodes: TaskNodeView[]) => {
      for (const n of nodes) {
        map.set(n.task_id, n.title);
        if (n.children?.length) walk(n.children);
      }
    };
    if (taskGraphQuery.data) walk(taskGraphQuery.data.nodes);
    return map;
  }, [taskGraphQuery.data]);

  // Если новый active появился — запомнить его run_id как sticky
  // (чтобы после завершения он не пропадал моментально).
  if (active && stickyRunId !== active.run_id) {
    setStickyRunId(active.run_id);
  }

  if (!display) return null;

  const isActive = display.status === "pending" || display.status === "running";

  // Сейчас в работе: leaf-задачи проекта со статусом in_progress. Список
  // расплющиваем из графа задач рекурсивно. Когда runner запустил задачу
  // (`transition_task("start")`), её status в БД становится in_progress;
  // запись в `run.steps` появится только после завершения. Поэтому без
  // task_graph узнать «что крутится прямо сейчас» нельзя.
  const inProgressTasks = (() => {
    const taskGraph = taskGraphQuery.data;
    if (!taskGraph) return [] as TaskNodeView[];
    const out: TaskNodeView[] = [];
    const walk = (nodes: TaskNodeView[]) => {
      for (const n of nodes) {
        if (n.status === "in_progress") out.push(n);
        if (n.children?.length) walk(n.children);
      }
    };
    walk(taskGraph.nodes);
    return out;
  })();

  // Завершённые шаги, схлопнутые ПО ЗАДАЧЕ: одна строка на задачу (последняя
  // попытка по времени завершения). Это убирает дубли и устаревшие статусы —
  // например, когда задача сначала заблокировалась на решениях, а после ответа
  // выполнилась заново: это одна и та же задача, а не две плашки.
  const finishTime = (s: (typeof allSteps)[number]["step"]) =>
    s.finished_at ? new Date(s.finished_at).getTime() : new Date(s.started_at).getTime();
  const liveIds = new Set(inProgressTasks.map((t) => t.task_id));
  const completedSteps = (() => {
    const byTask = new Map<string, (typeof allSteps)[number]>();
    for (const entry of allSteps) {
      // Шаги без task_id не схлопываем (у каждого свой уникальный ключ).
      const key = entry.step.task_id ?? `nokey-${entry.runId}-${entry.step.sequence}`;
      const prev = byTask.get(key);
      if (!prev || finishTime(entry.step) > finishTime(prev.step)) byTask.set(key, entry);
    }
    return [...byTask.values()]
      // Задачи, которые СЕЙЧАС в работе, живут в блоке «В работе» — не дублируем.
      .filter(({ step }) => !(step.task_id && liveIds.has(step.task_id)))
      // Сортировка по времени ЗАВЕРШЕНИЯ: закончилась позже — выше в списке.
      .sort((a, b) => finishTime(b.step) - finishTime(a.step));
  })();

  const hasContent = inProgressTasks.length > 0 || completedSteps.length > 0;

  return (
    <div className={cx("workflow-run", `workflow-run--${display.status}`)}>
      {/* Заголовок прогона (статус + «N в работе») живёт в шапке проекта
          (HeaderRunStatus). Здесь — два блока: «В работе» и «Выполнено». */}

      {/* «В работе» — отдельный блок, который НЕ скроллится (всегда виден
          целиком). Каждая задача кликабельна → переход к ней на графе. */}
      {inProgressTasks.length > 0 ? (
        <div className="workflow-run__live-block">
          <div className="workflow-run__section-label">В работе</div>
          <ul className="workflow-run__live">
            {inProgressTasks.map((t) => (
              <li key={`live-${t.task_id}`} className="workflow-run__row workflow-run__row--live">
                <span className="workflow-run__row-dot workflow-run__row-dot--live" />
                <button
                  type="button"
                  className="workflow-run__row-title workflow-run__row-title--link"
                  title="Показать на графе задач"
                  onClick={() =>
                    navigate(`/projects/${projectId}/task-graph?focus=${t.task_id}`)
                  }
                >
                  {t.title}
                </button>
                <span className="workflow-run__row-status workflow-run__row-status--active">идёт</span>
                <InProgressTimer startedAtIso={t.updated_at} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* «Выполнено» — задачи по времени завершения (позже завершённые сверху).
          Без внутреннего скролла: скроллится страница. */}
      {completedSteps.length > 0 ? (
        <div className="workflow-run__done-block">
          <div className="workflow-run__section-label">Выполнено</div>
          <ul className="workflow-run__timeline">
            {completedSteps.map(({ step, runId }) => {
              const durationSec = step.finished_at && step.started_at
                ? Math.max(0, Math.round(
                    (new Date(step.finished_at).getTime() - new Date(step.started_at).getTime()) / 1000,
                  ))
                : null;
              const viz = stepStatusVisual(step.validation_status, step.planning_outcome);
              const name =
                (step.task_id ? titleById.get(step.task_id) : null) ||
                step.task_key ||
                step.selected_step_id ||
                "(неизвестная задача)";
              const isFailed = step.validation_status === "failed";
              return (
                <li key={`${runId}-${step.sequence}`} className="workflow-run__row">
                  <span className={`workflow-run__row-dot workflow-run__row-dot--${viz.tone}`} />
                  {step.task_id ? (
                    <button
                      type="button"
                      className="workflow-run__row-title workflow-run__row-title--link"
                      title="Показать на графе задач"
                      onClick={() =>
                        navigate(`/projects/${projectId}/task-graph?focus=${step.task_id}`)
                      }
                    >
                      {name}
                    </button>
                  ) : (
                    <span className="workflow-run__row-title">{name}</span>
                  )}
                  <span className={`workflow-run__row-status workflow-run__row-status--${viz.tone}`}>
                    {viz.label}
                  </span>
                  {durationSec !== null ? (
                    <span className="workflow-run__row-duration">{formatElapsedHMS(durationSec)}</span>
                  ) : null}
                  {isFailed && step.task_id && onRetryTask ? (
                    <button
                      type="button"
                      className="workflow-run__row-retry"
                      disabled={retryingIds.has(step.task_id)}
                      onClick={() => {
                        const id = step.task_id!;
                        setRetryingIds((prev) => new Set(prev).add(id));
                        onRetryTask(id);
                      }}
                    >
                      {retryingIds.has(step.task_id) ? "Повторяю…" : "Повторить"}
                    </button>
                  ) : null}
                  {step.error_message ? (
                    <span className="workflow-run__row-error" title={step.error_message}>
                      {step.error_message.length > 120
                        ? step.error_message.slice(0, 120) + "…"
                        : step.error_message}
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {!hasContent && isActive ? (
        <div className="workflow-run__empty">Ожидаем первый шаг…</div>
      ) : null}
    </div>
  );
}

// Чистим «Шаг N/M:» префикс, который backend кладёт в last_step_summary.
// В UI индикатор N/M теперь не показываем; для пользователя важна суть
// сообщения, а не его порядковый номер.
function cleanStepSummary(summary: string): string {
  if (!summary) return summary;
  return summary
    .replace(/^Шаг\s*\d+\s*\/\s*\d+\s*:\s*/i, "")
    // Статус «Прервано» уже виден в пилюле — не дублируем его в тексте сводки.
    .replace(/^Прервано пользователем[:.]?\s*/i, "");
}

/**
 * Унифицированный формат «прошедшего времени» в часах/минутах/секундах.
 *
 * Правила:
 *   < 60 секунд → "45с"
 *   < 1 часа    → "1м 23с"
 *   ≥ 1 часа    → "2ч 05м 07с"
 *
 * Используется и для in-progress секундомера (live tick раз в секунду),
 * и для финального длительности завершённого шага. Раньше эти два места
 * использовали разные форматы (финал — голые секунды, "83с"), что сбивало
 * пользователя. Один helper — одна семантика.
 */
function formatElapsedHMS(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) {
    return `${h}ч ${String(m).padStart(2, "0")}м ${String(sec).padStart(2, "0")}с`;
  }
  if (m > 0) {
    return `${m}м ${String(sec).padStart(2, "0")}с`;
  }
  return `${sec}с`;
}

// Real-time секундомер для in-progress задачи. Обновляется раз в секунду;
// считает время от updated_at задачи (= момент перехода в in_progress)
// до текущего тика.
function InProgressTimer({ startedAtIso }: { startedAtIso: string }) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const tick = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);
  if (!startedAtIso) return null;
  const startMs = new Date(startedAtIso).getTime();
  if (Number.isNaN(startMs)) return null;
  const elapsedSec = Math.max(0, Math.floor((nowMs - startMs) / 1000));
  return (
    <span className="workflow-run__inprogress-timer">
      {formatElapsedHMS(elapsedSec)}
    </span>
  );
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
  // Упрощённый вид reasoning-стадии: заголовок одной строкой, поля без
  // dl/dt/dd-таблиц, options как простой нумерованный список с обоснованием.
  // Правила-«молнии» убраны как визуальный шум — методологические outcomes
  // видны в Decision Log и Provenance, дублировать в reasoning не нужно.
  const entries = Object.entries(stage.outputs ?? {}).filter(([key]) => !key.startsWith("_"));
  return (
    <section className="reasoning-stage">
      <h4 className="reasoning-stage__title">{stage.title || prettyLabel(stage.stage_id)}</h4>
      {entries.length === 0 ? (
        <p className="reasoning-stage__empty">Стадия ничего не зафиксировала.</p>
      ) : (
        <div className="reasoning-stage__body">
          {entries.map(([key, value]) => (
            <ReasoningField key={key} field={key} value={value} />
          ))}
        </div>
      )}
      {firedRules.length > 0 ? (
        <p className="reasoning-stage__rules">
          Сработали правила: {firedRules.map((r) => r.rule_id).join(", ")}
        </p>
      ) : null}
    </section>
  );
}

function ReasoningField({ field, value }: { field: string; value: unknown }) {
  // options — частый и важный кейс; рендерим как нумерованный список с
  // rationale/tradeoffs прозой, без отдельных классов на каждый элемент.
  if (field === "options" && Array.isArray(value)) {
    return (
      <div className="reasoning-stage__field">
        <span className="reasoning-stage__label">Варианты</span>
        <ol className="reasoning-stage__options">
          {value.map((opt, idx) => {
            const item = (opt ?? {}) as Record<string, unknown>;
            const confidence = typeof item.confidence === "number"
              ? ` (уверенность ${(item.confidence as number).toFixed(2)})`
              : "";
            return (
              <li key={idx}>
                <strong>{String(item.label ?? `Вариант ${idx + 1}`)}</strong>
                {confidence}
                {typeof item.rationale === "string" && item.rationale.trim() ? (
                  <span> — {item.rationale}</span>
                ) : null}
                {typeof item.tradeoffs === "string" && item.tradeoffs.trim() ? (
                  <span className="reasoning-stage__tradeoffs"> Компромисс: {item.tradeoffs}.</span>
                ) : null}
              </li>
            );
          })}
        </ol>
      </div>
    );
  }
  // jtbd_focus — частый объект { when, want, so_that }; разворачиваем
  // как один абзац с привычной структурой.
  if (
    field === "jtbd_focus"
    && value
    && typeof value === "object"
    && !Array.isArray(value)
  ) {
    const obj = value as Record<string, unknown>;
    const when = typeof obj.when === "string" ? obj.when : "";
    const want = typeof obj.want === "string" ? obj.want : "";
    const so_that = typeof obj.so_that === "string" ? obj.so_that : "";
    if (when || want || so_that) {
      return (
        <div className="reasoning-stage__field">
          <span className="reasoning-stage__label">JTBD</span>
          <p className="reasoning-stage__value">
            <em>Когда</em> {when || "—"}, <em>хочу</em> {want || "—"}, <em>чтобы</em> {so_that || "—"}.
          </p>
        </div>
      );
    }
  }
  // Простые скалярные значения — одной строкой.
  if (value === null || value === undefined || value === "") {
    return (
      <div className="reasoning-stage__field">
        <span className="reasoning-stage__label">{prettyLabel(field)}</span>
        <p className="reasoning-stage__value reasoning-stage__value--muted">не зафиксировано</p>
      </div>
    );
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return (
      <div className="reasoning-stage__field">
        <span className="reasoning-stage__label">{prettyLabel(field)}</span>
        <p className="reasoning-stage__value">{String(value)}</p>
      </div>
    );
  }
  // Сложные структуры — компактно сериализуем; в большинстве случаев
  // дополнительный JSON-блок излишен, но оставляем фоллбек.
  return (
    <details className="reasoning-stage__field reasoning-stage__field--json">
      <summary>{prettyLabel(field)}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
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

function TaskNodeDetail({
  task,
  onRetryTask,
  projectId,
  retrying = false,
}: {
  task: TaskNodeView;
  onRetryTask: (taskId: string) => void;
  projectId?: string;
  retrying?: boolean;
}) {
  const navigate = useNavigate();
  // Статус — из единого словаря (та же пилюля/цвет, что на графе и в ленте).
  const viz = taskStatusVisual(task.status);
  const failed = task.status === "failed";
  // v3.1: блок «открытые вопросы по задаче» убран — в новой Decision-модели
  // вопросы привязаны к checkpoint-сессиям и показываются на главном экране.

  return (
    <div className="task-detail">
      <div className="task-detail__head">
        <StatusPill tone={viz.tone}>{viz.label}</StatusPill>
        <span className="task-detail__type">{prettyLabel(task.template_type)}</span>
      </div>

      {task.status_summary ? (
        <p className="task-detail__summary">{task.status_summary}</p>
      ) : null}

      {failed ? (
        <div className="task-detail__alert">
          <AlertTriangle size={16} className="task-detail__alert-icon" aria-hidden />
          <div>
            <strong>Задача упала</strong>
            <p>
              Возможные причины: ошибка LLM-вызова, сбой валидации или гонка
              состояний. Повторите шаг — задача переподнимется с актуальным
              контекстом. Если для неё нужны решения, они появятся в шапке проекта.
            </p>
          </div>
        </div>
      ) : null}

      {task.retryable || (projectId && failed) ? (
        <div className="task-detail__actions">
          {task.retryable ? (
            <Button
              tone="secondary"
              icon={<RefreshCcw size={16} />}
              busy={retrying}
              onClick={() => onRetryTask(task.task_id)}
            >
              {retrying ? "Повторяю…" : "Повторить шаг"}
            </Button>
          ) : null}
          {projectId && failed ? (
            <Button tone="ghost" onClick={() => navigate(`/projects/${projectId}/decisions`)}>
              Открыть решения
            </Button>
          ) : null}
        </div>
      ) : null}

      {/* Технические детали — спокойный key-value список для тех, кому нужно. */}
      <dl className="task-detail__meta">
        <div>
          <dt>Шаблон</dt>
          <dd>{task.template_ref}</dd>
        </div>
        <div>
          <dt>Источник</dt>
          <dd>{labelForSourceKind(task.origin_kind)}</dd>
        </div>
        <div>
          <dt>Ref источника</dt>
          <dd>{task.origin_ref}</dd>
        </div>
        {task.slot_id ? (
          <div>
            <dt>Слот</dt>
            <dd>{task.slot_id}</dd>
          </div>
        ) : null}
      </dl>

      {projectId && task.template_type === "leaf" && task.status === "completed" ? (
        <ReasoningPanel projectId={projectId} taskId={task.task_id} />
      ) : null}
    </div>
  );
}

const ATTACHMENT_STATUS_LABELS: Record<string, { label: string; tone: "success" | "danger" | "warning" | "muted" }> = {
  pending: { label: "Извлечение текста…", tone: "muted" },
  succeeded: { label: "Текст извлечён", tone: "success" },
  failed: { label: "Текст не извлечён", tone: "danger" },
  unsupported: { label: "Формат без извлечения", tone: "warning" },
};

function attachmentStatusLabel(attachment: AttachmentView): string {
  return ATTACHMENT_STATUS_LABELS[attachment.extraction_status]?.label ?? attachment.extraction_status;
}

function isPdfAttachment(attachment: AttachmentView): boolean {
  return (
    attachment.mime_type === "application/pdf" ||
    attachment.original_filename.toLowerCase().endsWith(".pdf")
  );
}

/**
 * Карточка входных файлов. Строки оформлены и ведут себя как артефакты:
 * клик по файлу открывает его просмотр в правом контейнере (см. ArtifactsPage).
 * Действия (скачать/удалить) живут в просмотрщике, а не в строке — поэтому
 * строка узкая и не выходит за пределы колонки.
 */
function AttachmentsCard({
  projectId,
  inputArtifact,
  activeArtifactId,
  onSelectInputArtifact,
  selectedId,
  onSelect,
}: {
  projectId: string;
  inputArtifact: ArtifactSummaryView | null;
  activeArtifactId: string | null;
  onSelectInputArtifact: (artifactId: string) => void;
  selectedId: string | null;
  onSelect: (attachment: AttachmentView) => void;
}) {
  const attachmentsQuery = useQuery({
    queryKey: projectionKey(projectId, "attachments"),
    queryFn: () => api.getAttachments(projectId),
  });

  const attachments = attachmentsQuery.data ?? [];
  // Блок скрыт только если нет НИ введённого запроса, НИ приложенных файлов.
  if (attachments.length === 0 && !inputArtifact) {
    return null;
  }

  return (
    <SectionCard
      title="Входные материалы"
      subtitle="Текст запроса и приложенные файлы; нажмите, чтобы посмотреть"
    >
      <div className="artifact-list">
        {inputArtifact ? (
          <button
            type="button"
            className={cx(
              "artifact-list__item",
              activeArtifactId === inputArtifact.artifact_id && "artifact-list__item--active",
            )}
            onClick={() => onSelectInputArtifact(inputArtifact.artifact_id)}
          >
            <div className="artifact-list__title">
              <strong>Текст запроса</strong>
              <p>Введён вручную при создании проекта</p>
            </div>
            <div className="artifact-list__meta">
              <FileText size={14} />
            </div>
          </button>
        ) : null}
        {attachments.map((attachment) => (
          <button
            key={attachment.attachment_id}
            type="button"
            className={cx(
              "artifact-list__item",
              selectedId === attachment.attachment_id && "artifact-list__item--active",
            )}
            onClick={() => onSelect(attachment)}
          >
            <div className="artifact-list__title">
              <strong>{attachment.original_filename}</strong>
              <p>
                {formatFileSize(attachment.size_bytes)} · {attachmentStatusLabel(attachment)}
                {attachment.used_in_context ? " · использован" : ""}
              </p>
            </div>
            <div className="artifact-list__meta">
              <ChevronRight size={14} />
            </div>
          </button>
        ))}
      </div>
    </SectionCard>
  );
}

/**
 * Просмотр входного файла в правом контейнере — по аналогии с карточкой
 * артефакта. PDF встраивается во встроенный просмотрщик браузера (iframe),
 * остальные форматы показываются извлечённым текстом. Действия — скачать
 * оригинал и удалить (пока файл не использован в контексте).
 */
function AttachmentViewerPanel({
  projectId,
  attachment,
  onDeleted,
}: {
  projectId: string;
  attachment: AttachmentView;
  onDeleted: (attachmentId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const pdf = isPdfAttachment(attachment);
  const status = ATTACHMENT_STATUS_LABELS[attachment.extraction_status] ?? {
    label: attachment.extraction_status,
    tone: "muted" as const,
  };

  const textQuery = useQuery({
    queryKey: [projectId, "attachment-text", attachment.attachment_id],
    queryFn: () => api.getAttachmentText(projectId, attachment.attachment_id),
    enabled: !pdf && attachment.extraction_status === "succeeded",
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteAttachment(projectId, attachment.attachment_id),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "attachments") });
      onDeleted(attachment.attachment_id);
    },
    onError: (err: Error) => setError(err.message),
  });

  return (
    <div className="attachment-viewer">
      <div className="attachment-viewer__toolbar">
        <div className="attachment-viewer__status">
          <StatusPill tone={status.tone}>{status.label}</StatusPill>
          <span className="attachment-viewer__size">
            {formatFileSize(attachment.size_bytes)}
            {attachment.used_in_context ? " · использован в контексте" : ""}
          </span>
        </div>
        <div className="attachment-viewer__actions">
          <a
            className="artifact-detail__download"
            href={api.attachmentDownloadUrl(projectId, attachment.attachment_id)}
            download
            title="Скачать оригинал файла"
          >
            <Download size={14} /> Скачать
          </a>
          <button
            type="button"
            className="artifact-detail__download attachment-viewer__delete"
            disabled={!attachment.can_delete || deleteMutation.isPending}
            title={
              attachment.can_delete
                ? "Удалить файл"
                : "Файл уже использован в контексте задачи — удаление запрещено"
            }
            onClick={() => deleteMutation.mutate()}
          >
            <Trash2 size={14} /> Удалить
          </button>
        </div>
      </div>
      {error ? <p className="form-error">{error}</p> : null}
      {attachment.extraction_status === "failed" && attachment.extraction_error ? (
        <p className="attachment-viewer__note">{attachment.extraction_error}</p>
      ) : null}
      {pdf ? (
        <iframe
          className="attachment-viewer__pdf"
          src={api.attachmentViewUrl(projectId, attachment.attachment_id)}
          title={attachment.original_filename}
        />
      ) : attachment.extraction_status === "succeeded" ? (
        textQuery.isLoading ? (
          <LoadingPanel title="Загрузка содержимого…" />
        ) : textQuery.data && textQuery.data.text.trim() ? (
          <pre className="attachment-viewer__text">{textQuery.data.text}</pre>
        ) : (
          <EmptyState
            title="Текст пуст"
            description="Извлечённого текста нет — скачайте оригинал, чтобы открыть файл."
          />
        )
      ) : (
        <EmptyState
          title="Просмотр в браузере недоступен"
          description="Для этого формата нет извлечённого текста. Скачайте оригинал, чтобы открыть его локально."
          icon={<FileJson2 size={18} />}
        />
      )}
    </div>
  );
}

function ArtifactsPage({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const { artifactId } = useParams();
  // Выбранный входной файл для просмотра справа. Локальное состояние (не URL):
  // взаимоисключающе с выбранным артефактом — открытие одного снимает другое.
  const [selectedAttachment, setSelectedAttachment] = useState<AttachmentView | null>(null);
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

  // Входной запрос (роль input.request) — это введённый при создании текст.
  // Показываем его в блоке «Входные материалы» рядом с приложенными файлами,
  // а не в общем списке сгенерированных артефактов.
  const allArtifacts = artifactsQuery.data ?? [];
  const inputArtifact = allArtifacts.find((a) => a.artifact_role === "input.request") ?? null;
  // Артефакты от backend идут в порядке создания (старые сверху). В UI
  // менеджеру интереснее ВЕРХНИЙ артефакт = последний/финальный (например,
  // готовое ТЗ или review_report). Поэтому переворачиваем порядок.
  const artifacts = allArtifacts
    .filter((a) => a.artifact_role !== "input.request")
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));

  return (
    <div className="artifacts-page">
      <div className={cx("artifacts-layout", (artifactId || selectedAttachment) && "artifacts-layout--focused")}>
      <div className="artifacts-column">
      <SectionCard
        title="Артефакты проекта"
        subtitle="Документы и промежуточные результаты workflow"
        actions={
          artifacts.length > 0 ? (
            <a
              className="section-card__export"
              href={api.projectExportZipUrl(projectId)}
              download
              title="Скачать все Markdown-артефакты проекта одним архивом"
            >
              <Download size={13} />
              Экспорт MD
            </a>
          ) : undefined
        }
      >
        {artifacts.length === 0 ? (
          <EmptyState title="Артефакты отсутствуют" description="Запустите workflow, чтобы получить первые результаты." />
        ) : (
          <div className="artifact-list">
            {artifacts.map((artifact) => (
              <button
                key={artifact.artifact_id}
                type="button"
                className={cx("artifact-list__item", !selectedAttachment && artifactId === artifact.artifact_id && "artifact-list__item--active")}
                onClick={() => {
                  setSelectedAttachment(null);
                  navigate(`/projects/${projectId}/artifacts/${artifact.artifact_id}`);
                }}
              >
                <div className="artifact-list__title">
                  <strong>{stripRoleSuffix(artifact.title, artifact.artifact_role)}</strong>
                  <p>{prettyLabel(artifact.artifact_role)}</p>
                  {artifact.is_low_confidence ? (
                    <span className="artifact-lowconf-badge">
                      <AlertTriangle size={11} /> система не уверена
                    </span>
                  ) : null}
                </div>
                <div className="artifact-list__meta">
                  <span className="artifact-list__date">{formatDateOnly(artifact.created_at)}</span>
                  <span className="artifact-list__time">{formatTimeOnly(artifact.created_at)}</span>
                  <ChevronRight size={14} />
                </div>
              </button>
            ))}
          </div>
        )}
      </SectionCard>
      <AttachmentsCard
        projectId={projectId}
        inputArtifact={inputArtifact}
        activeArtifactId={!selectedAttachment ? artifactId ?? null : null}
        onSelectInputArtifact={(id) => {
          setSelectedAttachment(null);
          navigate(`/projects/${projectId}/artifacts/${id}`);
        }}
        selectedId={selectedAttachment?.attachment_id ?? null}
        onSelect={(attachment) => {
          setSelectedAttachment(attachment);
          navigate(`/projects/${projectId}/artifacts`);
        }}
      />
      </div>

      {selectedAttachment ? (
        <SectionCard title={selectedAttachment.original_filename} subtitle="Входной файл проекта">
          <AttachmentViewerPanel
            projectId={projectId}
            attachment={selectedAttachment}
            onDeleted={() => setSelectedAttachment(null)}
          />
        </SectionCard>
      ) : (
        <SectionCard
          title={
            artifactDetailQuery.data
              ? stripRoleSuffix(artifactDetailQuery.data.title, artifactDetailQuery.data.artifact_role)
              : "Выберите артефакт или файл"
          }
          subtitle={artifactDetailQuery.data?.description ?? "Читабельный документ и структурированные данные"}
        >
          {!artifactId ? (
            <EmptyState
              title="Выберите артефакт или файл"
              description="Откройте артефакт или входной файл слева, чтобы посмотреть его содержимое здесь."
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
      )}
      </div>
    </div>
  );
}

/**
 * v3.5 — расходы токенов по стадиям сборки одного артефакта.
 *
 * Показываем компактную таблицу: стадия | input | output | cache-read | total.
 * Если разбивки нет (артефакт сделан до v3.5, либо stub-провайдер) — не
 * рендерим вовсе. Внизу — итог по всем стадиям с подсветкой «дорого/средне/дёшево»
 * для быстрого поиска прожор.
 */
function ArtifactTokenUsage({ usage }: { usage: Record<string, import("./types").TokenUsageStage> }) {
  const stages = Object.entries(usage).filter(
    ([, v]) => (v?.total_tokens ?? 0) > 0 || (v?.input_tokens ?? 0) > 0 || (v?.output_tokens ?? 0) > 0,
  );
  if (stages.length === 0) {
    return null;
  }
  const stageLabel = (k: string): string => {
    // v3.10: pre_flight_planning → decision_identification (старый ключ
    // оставлен для legacy-артефактов).
    if (k === "decision_identification" || k === "pre_flight_planning") return "Выявление решений";
    if (k === "primary_generation") return "Основная сборка";
    if (k.startsWith("methodology_stage:")) return `Стадия методологии · ${k.slice("methodology_stage:".length)}`;
    return k;
  };
  const totalInput = stages.reduce((s, [, v]) => s + (v.input_tokens || 0), 0);
  const totalOutput = stages.reduce((s, [, v]) => s + (v.output_tokens || 0), 0);
  const totalCacheRead = stages.reduce((s, [, v]) => s + (v.cache_read_tokens || 0), 0);
  const grandTotal = totalInput + totalOutput;
  const fmt = (n: number) => n.toLocaleString("ru-RU");
  return (
    <div className="artifact-tokens">
      <div className="artifact-tokens__head">
        <strong>Токены</strong>
        <span className="artifact-tokens__total">всего {fmt(grandTotal)}</span>
      </div>
      <table className="artifact-tokens__table">
        <thead>
          <tr>
            <th>Стадия</th>
            <th>Input</th>
            <th>Output</th>
            <th>Cache-read</th>
            <th>Всего</th>
          </tr>
        </thead>
        <tbody>
          {stages.map(([key, val]) => {
            const stageTotal = (val.input_tokens || 0) + (val.output_tokens || 0);
            const share = grandTotal > 0 ? stageTotal / grandTotal : 0;
            const heavy = share > 0.5;
            return (
              <tr key={key} className={heavy ? "artifact-tokens__row--heavy" : undefined}>
                <td>{stageLabel(key)}</td>
                <td>{fmt(val.input_tokens || 0)}</td>
                <td>{fmt(val.output_tokens || 0)}</td>
                <td>{fmt(val.cache_read_tokens || 0)}</td>
                <td>
                  <strong>{fmt(stageTotal)}</strong>
                  {grandTotal > 0 ? (
                    <span className="artifact-tokens__share"> · {Math.round(share * 100)}%</span>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>Итого</td>
            <td>{fmt(totalInput)}</td>
            <td>{fmt(totalOutput)}</td>
            <td>{fmt(totalCacheRead)}</td>
            <td>
              <strong>{fmt(grandTotal)}</strong>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

interface FeasibilityCapability {
  name?: string;
  origin?: string;
  feasibility?: string;
  rationale?: string;
  blockers?: string[];
  prerequisites?: string[];
  confidence?: number;
  covered_by?: string;
  matched_capability?: string;
}

interface FeasibilityPayload {
  capabilities?: FeasibilityCapability[];
  overall_feasibility?: string;
  summary?: string;
}

const FEAS_VERDICT_LABEL: Record<string, string> = {
  feasible: "реализуемо",
  conditional: "при условии",
  uncertain: "под вопросом",
  infeasible: "не реализуемо",
};

const FEAS_OVERALL_LABEL: Record<string, string> = {
  feasible: "всё реализуемо",
  mixed: "частично реализуемо",
  blocked: "есть нереализуемые части",
};

// Структурный вид оценки реализуемости: цветной бейдж вердикта + чип
// покрывающего агента по каждой части. Сканируемо с одного взгляда (в отличие
// от плоской markdown-таблицы). Данные — из json_content артефакта.
function FeasibilityView({ data }: { data: FeasibilityPayload }) {
  const caps = Array.isArray(data.capabilities) ? data.capabilities : [];
  return (
    <article className="document-surface feasibility-view">
      {data.summary || data.overall_feasibility ? (
        <div className="feasibility-view__head">
          {data.overall_feasibility ? (
            <span className={cx("feas-overall", `feas-overall--${data.overall_feasibility}`)}>
              {FEAS_OVERALL_LABEL[data.overall_feasibility] ?? data.overall_feasibility}
            </span>
          ) : null}
          {data.summary ? <p className="feasibility-view__summary">{data.summary}</p> : null}
        </div>
      ) : null}
      <ul className="feasibility-view__list">
        {caps.map((cap, index) => {
          const verdict = cap.feasibility ?? "";
          return (
            <li key={index} className="feas-row">
              <div className="feas-row__top">
                <span className={cx("feas-badge", `feas-badge--${verdict}`)}>
                  {FEAS_VERDICT_LABEL[verdict] ?? (verdict || "—")}
                </span>
                <span className="feas-row__name">{cap.name ?? "—"}</span>
                {cap.covered_by ? (
                  <span className="feas-chip" title="Покрывающий агент · способность">
                    {cap.covered_by}
                    {cap.matched_capability ? ` · ${cap.matched_capability}` : ""}
                  </span>
                ) : (
                  <span className="feas-chip feas-chip--none" title="Ни один агент не покрывает эту часть">
                    нет агента
                  </span>
                )}
              </div>
              {cap.rationale ? <p className="feas-row__rationale">{cap.rationale}</p> : null}
              {Array.isArray(cap.blockers) && cap.blockers.length > 0 ? (
                <p className="feas-row__meta">
                  <span>Блокеры:</span> {cap.blockers.join("; ")}
                </p>
              ) : null}
              {Array.isArray(cap.prerequisites) && cap.prerequisites.length > 0 ? (
                <p className="feas-row__meta">
                  <span>Нужно для реализации:</span> {cap.prerequisites.join("; ")}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
    </article>
  );
}

function ArtifactDetailPanel({ detail, projectId }: { detail: ArtifactDetailView; projectId: string }) {
  const [mode, setMode] = useState<"doc" | "json" | "reasoning" | "validations" | "decisions">("doc");
  const [provenanceOpen, setProvenanceOpen] = useState(false);
  // За один разбор: рендерим markdown → HTML, проставляем стабильные id на
  // заголовки и собираем кликабельное оглавление. DOMParser — нативный, без
  // зависимостей; mermaid-host блоки переживают re-serialize.
  const { html, toc } = useMemo(() => {
    const raw = detail.markdown_content
      ? (marked.parse(preprocessMarkdownForMermaid(detail.markdown_content)) as string)
      : "<p>Markdown-представление отсутствует.</p>";
    const parsed = new DOMParser().parseFromString(raw, "text/html");
    const headings = Array.from(parsed.querySelectorAll("h2, h3")) as HTMLElement[];
    const tocEntries = headings
      .map((heading, index) => {
        const id = `doc-sec-${index + 1}`;
        heading.id = id;
        return {
          id,
          text: heading.textContent?.trim() ?? "",
          level: heading.tagName === "H3" ? 3 : 2,
        };
      })
      .filter((entry) => entry.text.length > 0);
    return { html: parsed.body.innerHTML, toc: tocEntries };
  }, [detail.markdown_content]);
  // Структурный вид для оценки реализуемости: парсим payload из json_content.
  // null → откатываемся на markdown-рендер (другая роль или битый JSON).
  const feasibilityData = useMemo<FeasibilityPayload | null>(() => {
    if (detail.artifact_role !== "feasibility_assessment" || !detail.json_content) return null;
    try {
      const parsed = JSON.parse(detail.json_content) as FeasibilityPayload;
      return Array.isArray(parsed?.capabilities) ? parsed : null;
    } catch {
      return null;
    }
  }, [detail.artifact_role, detail.json_content]);
  const articleRef = useRef<HTMLElement | null>(null);
  const scrollToSection = (id: string) => {
    // Скроллим к разделу внутри текущего документа, без смены URL-хеша
    // (чтобы не конфликтовать с роутером).
    const target = articleRef.current?.querySelector(`#${CSS.escape(id)}`);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  useEffect(() => {
    if (mode !== "doc") return;
    const root = articleRef.current;
    if (!root) return;
    const nodes = Array.from(root.querySelectorAll(".mermaid-host")) as HTMLElement[];
    if (nodes.length === 0) return;
    // Безопасный вызов: невалидный mermaid-синтаксис не должен ронять UI.
    mermaid.run({ nodes }).catch((err) => {
      console.warn("Mermaid render failed:", err);
    });
  }, [html, mode]);
  const traceQuery = useQuery({
    queryKey: [projectId, "methodology-trace", detail.created_by_task_id],
    queryFn: () => api.getMethodologyTrace(projectId, detail.created_by_task_id!),
    enabled: provenanceOpen && Boolean(detail.created_by_task_id),
  });
  // v3.0: решения, принятые при сборке этого артефакта.
  const decisionsQuery = useQuery({
    queryKey: ["artifact-decisions", projectId, detail.artifact_id],
    queryFn: () => api.getDecisionsForArtifact(projectId, detail.artifact_id),
  });
  const decisionsCount = decisionsQuery.data?.length ?? 0;

  // Подтверждение низкоуверенного артефакта — зеркально verify решения.
  const queryClient = useQueryClient();
  const verifyMutation = useMutation({
    mutationFn: () => api.verifyArtifact(projectId, detail.artifact_id, true),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "artifacts") });
      void queryClient.invalidateQueries({ queryKey: [projectId, "artifact-detail", detail.artifact_id] });
    },
  });
  // Ф3: согласование итогового артефакта с заказчиком (тумблер). Инвалидирует
  // также проекцию stages — степпер красит этап и разблокирует «Следующий этап».
  const signOffMutation = useMutation({
    mutationFn: (next: boolean) => api.signOffArtifact(projectId, detail.artifact_id, next),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "artifacts") });
      void queryClient.invalidateQueries({ queryKey: [projectId, "artifact-detail", detail.artifact_id] });
      void queryClient.invalidateQueries({ queryKey: projectionKey(projectId, "stages") });
    },
  });
  // Согласование показываем только на ИТОГОВОМ артефакте этапа (key_artifact_id
  // из проекции stages) — на нём держится переход к следующему гейту.
  const stagesQuery = useQuery({
    queryKey: projectionKey(projectId, "stages"),
    queryFn: () => api.getStages(projectId),
  });
  const isKeyArtifact = (stagesQuery.data?.stages ?? []).some(
    (s) => s.key_artifact_id === detail.artifact_id,
  );

  return (
    <div className="artifact-detail">
      <div className="artifact-detail__toolbar">
        <div className="segmented">
        <button className={cx("segmented__item", mode === "doc" && "segmented__item--active")} onClick={() => setMode("doc")} type="button">
          Документ
        </button>
        {/* Reasoning / CoT: рассуждение методологии, которое привело к
            этому артефакту. Полезно для проверки, ПОЧЕМУ артефакт именно
            такой. Доступно только если артефакт привязан к задаче-производителю. */}
        {detail.created_by_task_id ? (
          <button
            className={cx("segmented__item", mode === "reasoning" && "segmented__item--active")}
            onClick={() => setMode("reasoning")}
            type="button"
          >
            Рассуждение
          </button>
        ) : null}
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
        {/* v3.0: Решения, принятые при сборке этого артефакта. Вкладка
            показывается всегда (даже если 0), чтобы пользователь видел
            наличие концепта; счётчик подсказывает наполненность. */}
        <button
          className={cx("segmented__item", mode === "decisions" && "segmented__item--active")}
          onClick={() => setMode("decisions")}
          type="button"
        >
          Решения{decisionsCount > 0 ? ` (${decisionsCount})` : ""}
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
        {detail.markdown_content ? (
          <div className="artifact-detail__downloads">
            <a
              className="artifact-detail__download"
              href={api.artifactMdUrl(projectId, detail.artifact_id)}
              download
              title="Скачать артефакт как Markdown"
            >
              <Download size={14} />
              MD
            </a>
            <a
              className="artifact-detail__download"
              href={api.artifactPdfUrl(projectId, detail.artifact_id)}
              download
              title="Скачать артефакт как PDF"
            >
              <Download size={14} />
              PDF
            </a>
          </div>
        ) : null}
      </div>
      {/* Компактная одна строка с самой важной мета-инфой.
          Раньше была плитка из 11 «label/value» — занимала пол-экрана.
          Теперь главное: роль, дата, провайдер/модель, уверенность —
          одной строкой. Остальное — в развороте через <details>. */}
      <div className="artifact-meta-strip">
        <span className="artifact-meta-strip__pill">{prettyLabel(detail.artifact_role)}</span>
        <span className="artifact-meta-strip__sep">·</span>
        <span title={detail.created_at}>{formatDateTime(detail.created_at)}</span>
        {detail.provider || detail.model ? (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <span className="artifact-meta-strip__provider">
              {detail.provider ?? "—"}
              {detail.model ? ` · ${detail.model}` : ""}
            </span>
          </>
        ) : null}
        {detail.overall_confidence !== null && detail.overall_confidence !== undefined ? (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <span className="artifact-meta-strip__confidence">
              confidence {(detail.overall_confidence * 100).toFixed(0)}%
            </span>
          </>
        ) : null}
        {detail.usage_total_tokens !== null && detail.usage_total_tokens !== undefined ? (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <span
              className="artifact-meta-strip__tokens"
              title={`Вход: ${detail.usage_input_tokens ?? 0} · Выход: ${detail.usage_output_tokens ?? 0} · вызовов: ${detail.usage_call_count}`}
            >
              {detail.usage_total_tokens.toLocaleString("ru-RU")} токенов
              {detail.usage_source === "estimated" ? " (оценка)" : ""}
            </span>
          </>
        ) : (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <span className="artifact-meta-strip__tokens" title="Провайдер не вернул данные о токенах">
              токены n/a
            </span>
          </>
        )}
        {detail.is_superseded ? (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <StatusPill tone="warning">устарел</StatusPill>
          </>
        ) : null}
        {detail.is_low_confidence ? (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <span className="artifact-lowconf">
              <AlertTriangle size={12} />
              <span>низкая уверенность — подтвердите</span>
              <button
                type="button"
                className="artifact-lowconf__verify"
                disabled={verifyMutation.isPending}
                title="Я просмотрел артефакт — согласен, снять метку"
                onClick={() => verifyMutation.mutate()}
              >
                <CheckCircle2 size={12} /> подтверждаю
              </button>
            </span>
          </>
        ) : detail.user_verified ? (
          <>
            <span className="artifact-meta-strip__sep">·</span>
            <span
              className="artifact-verified"
              title={
                detail.user_verified_at
                  ? `Подтверждено вами ${detail.user_verified_at.slice(0, 16).replace("T", " ")}`
                  : "Подтверждено вами"
              }
            >
              <CheckCircle2 size={12} /> подтверждено
            </span>
          </>
        ) : null}
      </div>
      {isKeyArtifact ? (
        <div className={cx("artifact-signoff", detail.signed_off && "artifact-signoff--done")}>
          <div className="artifact-signoff__text">
            {detail.signed_off ? (
              <CheckCircle2 size={18} className="artifact-signoff__icon" aria-hidden />
            ) : (
              <AlertTriangle size={18} className="artifact-signoff__icon" aria-hidden />
            )}
            <div>
              <strong>
                {detail.signed_off ? "Согласовано с заказчиком" : "Согласование с заказчиком"}
              </strong>
              <p>
                {detail.signed_off
                  ? "Этап завершён. Можно переходить к следующему."
                  : "Это итоговый артефакт этапа. Согласуйте его, чтобы завершить этап и открыть переход к следующему."}
              </p>
            </div>
          </div>
          <Button
            tone={detail.signed_off ? "ghost" : "primary"}
            icon={detail.signed_off ? undefined : <CheckCircle2 size={16} />}
            busy={signOffMutation.isPending}
            onClick={() => signOffMutation.mutate(!detail.signed_off)}
          >
            {detail.signed_off ? "Снять согласование" : "Согласовать"}
          </Button>
        </div>
      ) : null}
      <details className="artifact-meta-extra">
        <summary>Подробные параметры артефакта</summary>
        <div className="artifact-meta-extra__grid">
          <div>
            <span>Тип</span>
            <strong>{prettyLabel(detail.artifact_kind)}</strong>
          </div>
          <div>
            <span>Задача-производитель</span>
            <strong>{detail.created_by_task_id ?? "—"}</strong>
          </div>
          {detail.complexity ? (
            <div>
              <span>Сложность</span>
              <strong>{prettyLabel(detail.complexity)}</strong>
            </div>
          ) : null}
          {detail.merge_strategy ? (
            <div>
              <span>Стратегия слияния</span>
              <strong>{prettyLabel(detail.merge_strategy)}</strong>
            </div>
          ) : null}
          {detail.methodology_pack_ref ? (
            <div>
              <span>Методология</span>
              <strong>{detail.methodology_pack_ref}</strong>
            </div>
          ) : null}
          {detail.parent_artifact_id ? (
            <div>
              <span>Предыдущая версия</span>
              <strong>
                <Link to={`/projects/${projectId}/artifacts/${detail.parent_artifact_id}`}>
                  v{detail.parent_artifact_id.slice(0, 8)}
                </Link>
              </strong>
            </div>
          ) : null}
          {detail.input_artifact_ids.length > 0 ? (
            <div>
              <span>Входные артефакты</span>
              <strong>{detail.input_artifact_ids.length}</strong>
            </div>
          ) : null}
          {detail.used_position_ids.length > 0 ? (
            <div>
              <span>Использованные положения</span>
              <strong>{detail.used_position_ids.length}</strong>
            </div>
          ) : null}
        </div>
        <ArtifactTokenUsage usage={detail.token_usage} />
      </details>
      {/* Блок «Развернуть provenance-ссылки» удалён: эти ссылки уже доступны
          в Provenance-модалке (segmented кнопка), а на самой страничке
          артефакта они только захламляли документ. */}
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
        feasibilityData ? (
          <FeasibilityView data={feasibilityData} />
        ) : (
          <div className="document-layout">
            {toc.length >= 2 ? (
              <nav className="document-toc" aria-label="Содержание документа">
                <p className="document-toc__title">Содержание</p>
                <ul className="document-toc__list">
                  {toc.map((item) => (
                    <li
                      key={item.id}
                      className={cx("document-toc__item", item.level === 3 && "document-toc__item--sub")}
                    >
                      <a
                        href={`#${item.id}`}
                        onClick={(event) => {
                          event.preventDefault();
                          scrollToSection(item.id);
                        }}
                      >
                        {item.text}
                      </a>
                    </li>
                  ))}
                </ul>
              </nav>
            ) : null}
            <article
              ref={articleRef}
              className="document-surface"
              dangerouslySetInnerHTML={{ __html: html }}
            />
          </div>
        )
      ) : null}
      {mode === "reasoning" && detail.created_by_task_id ? (
        <ReasoningPanel projectId={projectId} taskId={detail.created_by_task_id} />
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
      {mode === "decisions" ? (
        <ArtifactDecisionsTab
          projectId={projectId}
          decisions={decisionsQuery.data ?? []}
          loading={decisionsQuery.isLoading}
        />
      ) : null}
    </div>
  );
}

/** v3.0: рендер вкладки «Решения» внутри артефакта. */
function ArtifactDecisionsTab({
  projectId,
  decisions,
  loading,
}: {
  projectId: string;
  decisions: import("./types").DecisionItemView[];
  loading: boolean;
}) {
  const navigate = useNavigate();
  if (loading) return <LoadingPanel title="Загружаем решения…" />;
  if (decisions.length === 0) {
    return (
      <EmptyState
        title="Решений по этому артефакту нет"
        description="Реестр пополняется по мере прохождения задач. Если артефакт собран до v3.0, решения для него не зафиксированы."
      />
    );
  }
  return (
    <div className="artifact-decisions">
      <div className="artifact-decisions__head">
        <span>{decisions.length} {decisions.length === 1 ? "решение" : "решений"} принято при сборке</span>
        <Button tone="ghost" onClick={() => navigate(`/projects/${projectId}/decisions`)}>
          В полный реестр
        </Button>
      </div>
      <ul className="artifact-decisions__list">
        {decisions.map((d) => (
          <li key={d.decision_id} className="artifact-decisions__item">
            <div className="artifact-decisions__title">
              <span
                className={cx(
                  "artifact-decisions__level-dot",
                  `artifact-decisions__level-dot--${d.level}`,
                )}
                title={`Уровень: ${d.level}`}
              />
              <strong>{d.title}</strong>
            </div>
            <div className="artifact-decisions__chosen">
              <span className="artifact-decisions__chosen-label">Выбрано:</span>
              {d.chosen_option_label || "—"}
              {d.was_user_modified ? (
                <span className="artifact-decisions__user-mark"> · вами</span>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Дата и время отдельно — для двухстрочного отображения в meta-колонке
// списка артефактов в развёрнутом режиме.
function formatDateOnly(iso: string): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return "";
    return dt.toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  } catch {
    return "";
  }
}

function formatTimeOnly(iso: string): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return "";
    return dt.toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

// Чистит "(integration_operating_model)"-суффикс в названии артефакта.
// Backend новые артефакты так не маркирует, но в старых записях БД
// техническое имя роли всё ещё внутри title.
function stripRoleSuffix(title: string, role: string): string {
  if (!title || !role) return title;
  const trimmed = title.trim();
  const suffix = ` (${role})`;
  if (trimmed.toLowerCase().endsWith(suffix.toLowerCase())) {
    return trimmed.slice(0, -suffix.length).trim();
  }
  // На случай если в скобках лежит роль с другим регистром.
  return trimmed.replace(/\s*\([^()]+\)\s*$/u, (match) => {
    const inside = match.replace(/[\s()]+/g, "");
    if (inside.toLowerCase() === role.toLowerCase()) return "";
    return match;
  }).trim();
}

function flattenTaskNodes(nodes: TaskNodeView[]): TaskNodeView[] {
  return nodes.flatMap((node) => [node, ...flattenTaskNodes(node.children)]);
}

// Проекции, которые откат меняет одномоментно (без workflow-run, поэтому
// WS-пуш их не инвалидирует — делаем это вручную после успешного отката).
const ROLLBACK_INVALIDATED_PROJECTIONS: ProjectionName[] = [
  "shell",
  "task_graph",
  "situation",
  "timeline",
  "artifacts",
  "state",
  "overview",
  "stages",
  "debug",
];

const ROLLBACK_HISTORY_KEY = (projectId: string) => ["rollback-history", projectId] as const;

/**
 * Плавающая панель подтверждения отката внутри канвы графа (НЕ модал —
 * чтобы подсветка затрагиваемых узлов на графе оставалась видна). Показывает,
 * сколько шагов будет сброшено и сколько артефактов уйдёт в архив, а сами
 * затрагиваемые шаги подсвечиваются на графе (target + транзитивные). Откат
 * необратим для активных данных, поэтому подтверждение — отдельная danger-кнопка
 * (двухшаговый, осознанный жест: «взвести» на узле → подтвердить здесь).
 */
function RollbackConfirmBar({
  preview,
  loading,
  previewError,
  confirming,
  confirmError,
  onConfirm,
  onCancel,
}: {
  preview: RollbackPreviewView | undefined;
  loading: boolean;
  previewError: string | null;
  confirming: boolean;
  confirmError: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const stepCount = preview?.reverted_steps.length ?? 0;
  const artifactCount = preview?.archived_artifacts.length ?? 0;
  const blocked = Boolean(preview && !preview.rollbackable);
  const errorText = previewError ?? (blocked ? preview?.blocked_reason ?? null : null);
  const canConfirm = Boolean(preview) && !blocked && !previewError && !loading;
  return (
    <div className="rollback-bar" role="dialog" aria-label="Подтверждение отката">
      <span className="rollback-bar__icon" aria-hidden>
        <Undo2 size={18} />
      </span>
      <div className="rollback-bar__body">
        {loading ? (
          <span className="rollback-bar__title">Расчёт зависимостей…</span>
        ) : errorText ? (
          <span className="rollback-bar__title rollback-bar__title--error">{errorText}</span>
        ) : preview ? (
          <>
            <span className="rollback-bar__title">
              Откат до «{preview.target_title}»
            </span>
            <span className="rollback-bar__meta">
              Будет сброшено шагов: <strong>{stepCount}</strong> · в архив:{" "}
              <strong>{artifactCount}</strong>. Затрагиваемые шаги подсвечены на графе.
            </span>
            {confirmError ? (
              <span className="rollback-bar__title rollback-bar__title--error">{confirmError}</span>
            ) : null}
          </>
        ) : null}
      </div>
      <div className="rollback-bar__actions">
        <Button tone="ghost" onClick={onCancel} disabled={confirming}>
          Отмена
        </Button>
        {canConfirm ? (
          <Button
            tone="danger"
            icon={<Undo2 size={16} />}
            onClick={onConfirm}
            busy={confirming}
          >
            {confirming ? "Откат…" : "Подтвердить откат"}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function RollbackHistorySection({ projectId }: { projectId: string }) {
  const historyQuery = useQuery({
    queryKey: ROLLBACK_HISTORY_KEY(projectId),
    queryFn: () => api.getRollbackHistory(projectId),
  });
  const items = historyQuery.data?.items ?? [];
  if (items.length === 0) {
    return null;
  }
  return (
    <SectionCard title="История откатов" subtitle={`Выполнено откатов: ${items.length}`}>
      <ul className="rollback-history">
        {items.map((item) => (
          <li key={item.rollback_id} className="rollback-history__row">
            <div className="rollback-history__main">
              <strong>{item.target_title}</strong>
              {item.reason ? <span className="muted"> — {item.reason}</span> : null}
            </div>
            <div className="rollback-history__meta muted">
              {formatDateTime(item.created_at)} · шагов: {item.reverted_count} · в архив:{" "}
              {item.archived_artifact_count}
            </div>
          </li>
        ))}
      </ul>
    </SectionCard>
  );
}

function TaskGraphPage({ projectId }: { projectId: string }) {
  // W4.2 (G1): canvas-based task graph через ReactFlow + dagre.
  // Кликнул на узел → открывается drawer с тем же TaskNodeDetail,
  // что и на L2 Activity, плюс панель «Рассуждение» внутри.
  // Ни provider, ни model из UI не передаются — см. WorkspaceRoute.
  const provider = "";
  const model = "";
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selectedTask, setSelectedTask] = useState<TaskNodeView | null>(null);
  // Цель отката (task_id выбранного шага) — открывает диалог подтверждения.
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null);
  // Ф1: выбранная подвкладка-гейт (objective_ref). null → текущий (активный) гейт.
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  // Дип-линк из статус-бара: /task-graph?focus=<taskId> — центрируем граф.
  const [searchParams] = useSearchParams();
  const focusTaskId = searchParams.get("focus") ?? undefined;
  const taskGraphQuery = useQuery({
    queryKey: projectionKey(projectId, "task_graph"),
    queryFn: () => api.getTaskGraph(projectId),
  });
  // Чтобы с узла попадать на КОНКРЕТНЫЙ произведённый артефакт (по created_by_task_id).
  const artifactsQuery = useQuery({
    queryKey: projectionKey(projectId, "artifacts"),
    queryFn: () => api.getArtifacts(projectId),
  });
  const retryMutation = useMutation({
    mutationFn: (taskId: string) => api.retryTask(projectId, taskId, provider, model),
  });
  // Превью отката — чистое чтение; запускается только когда выбрана цель.
  const previewQuery = useQuery({
    queryKey: ["rollback-preview", projectId, rollbackTarget],
    queryFn: () => api.getRollbackPreview(projectId, rollbackTarget as string),
    enabled: rollbackTarget !== null,
  });
  const rollbackMutation = useMutation<RollbackResultView, Error, string>({
    mutationFn: (taskId: string) => api.rollbackStep(projectId, taskId),
    onSuccess: async () => {
      for (const projection of ROLLBACK_INVALIDATED_PROJECTIONS) {
        await queryClient.invalidateQueries({ queryKey: projectionKey(projectId, projection) });
      }
      await queryClient.invalidateQueries({ queryKey: ROLLBACK_HISTORY_KEY(projectId) });
      setRollbackTarget(null);
      // После отката активный гейт мог смениться (кросс-objective откат) —
      // сбрасываем выбор подвкладки, чтобы вид следовал за восстановленным гейтом.
      setSelectedRef(null);
      rollbackMutation.reset();
    },
  });

  // Ф1: цепочка гейтов для подвкладок + граф выбранного гейта. Все хуки —
  // до раннего return (правила хуков).
  const stagesQuery = useQuery({
    queryKey: projectionKey(projectId, "stages"),
    queryFn: () => api.getStages(projectId),
  });
  const activeRef = taskGraphQuery.data?.objective_ref ?? null;
  const effectiveRef = selectedRef ?? activeRef;
  const isActiveSelected = !effectiveRef || effectiveRef === activeRef;
  const objectiveGraphQuery = useQuery({
    queryKey: [projectId, "objective-task-graph", effectiveRef],
    queryFn: () => api.getObjectiveTaskGraph(projectId, effectiveRef as string),
    enabled: Boolean(effectiveRef) && !isActiveSelected,
  });

  if (taskGraphQuery.isLoading || !taskGraphQuery.data) {
    return <LoadingPanel title="Загрузка графа задач…" />;
  }

  const data = taskGraphQuery.data;
  // Граф выбранного гейта: активный — живой taskGraphQuery; иной — по objective.
  const displayGraph = isActiveSelected ? data : objectiveGraphQuery.data ?? null;
  const stages = stagesQuery.data?.stages ?? [];
  // Откат доступен на активном И завершённом гейте (откат завершённого вернёт
  // проект на него — кросс-objective). На скелете будущего гейта — нельзя.
  const canRollback = displayGraph ? displayGraph.objective_state !== "locked" : false;
  const closeRollback = () => {
    if (rollbackMutation.isPending) return; // не закрываем во время отката
    setRollbackTarget(null);
    rollbackMutation.reset();
  };
  // Какая задача сейчас перезапускается — её кнопка «Повторить» блокируется,
  // чтобы нельзя было запустить ретрай повторно до обновления графа (задача 2).
  const retryingTaskId = retryMutation.isPending ? retryMutation.variables ?? null : null;
  // Шаги, которые откатятся вместе с выбранным — для подсветки на графе (задача 3).
  const rollbackAffectedIds =
    rollbackTarget !== null
      ? previewQuery.data?.reverted_steps.map((s) => s.task_id) ?? []
      : [];
  return (
    <>
      <SectionCard
        title="Граф задач"
        subtitle={
          displayGraph
            ? `Завершено ${displayGraph.completed_leaf_tasks} из ${displayGraph.total_leaf_tasks} листовых задач`
            : "Загрузка графа гейта…"
        }
      >
        {/* Ф1: подвкладки по гейтам. Текущий исполняемый гейт помечен точкой,
            выбранная подвкладка — активным стилем. Граф неактивного гейта
            доступен сразу, его задачи показаны недоступными. */}
        {stages.length > 1 ? (
          <div className="tg-subtabs" role="tablist">
            {stages.map((s) => {
              const isSel = effectiveRef === s.objective_ref;
              return (
                <button
                  key={s.objective_ref}
                  type="button"
                  role="tab"
                  aria-selected={isSel}
                  className={cx(
                    "tg-subtab",
                    isSel && "tg-subtab--active",
                    s.is_current && "tg-subtab--current",
                  )}
                  title={
                    s.is_current
                      ? "Текущий исполняемый гейт"
                      : s.state === "done"
                        ? "Завершённый гейт"
                        : "Ещё не запущен — задачи показаны как недоступные"
                  }
                  onClick={() => setSelectedRef(s.objective_ref)}
                >
                  {s.is_current ? <span className="tg-subtab__dot" aria-hidden /> : null}
                  {shortStageLabel(s.objective_ref, s.title)}
                </button>
              );
            })}
          </div>
        ) : null}
        {displayGraph ? (
          <TaskGraphCanvas
            tree={displayGraph.nodes}
            onSelectNode={setSelectedTask}
            focusTaskId={isActiveSelected ? focusTaskId : undefined}
            completedLeafTasks={displayGraph.completed_leaf_tasks}
            totalLeafTasks={displayGraph.total_leaf_tasks}
            onRetry={(taskId) => retryMutation.mutate(taskId)}
            onOpenArtifacts={(task) => {
              const art = (artifactsQuery.data ?? []).find(
                (a) => a.created_by_task_id === task.task_id,
              );
              navigate(
                art
                  ? `/projects/${projectId}/artifacts/${art.artifact_id}`
                  : `/projects/${projectId}/artifacts`,
              );
            }}
            onGoToDecisions={() => navigate(`/projects/${projectId}/decisions/pending`)}
            onRollback={(taskId) => setRollbackTarget(taskId)}
            retryingTaskId={retryingTaskId}
            rollbackTargetId={canRollback ? rollbackTarget : null}
            rollbackAffectedIds={canRollback ? rollbackAffectedIds : []}
            rollbackOverlay={
              canRollback && rollbackTarget !== null ? (
                <RollbackConfirmBar
                  preview={previewQuery.data}
                  loading={previewQuery.isLoading}
                  previewError={
                    previewQuery.error instanceof Error ? previewQuery.error.message : null
                  }
                  confirming={rollbackMutation.isPending}
                  confirmError={
                    rollbackMutation.error instanceof Error ? rollbackMutation.error.message : null
                  }
                  onConfirm={() => {
                    if (rollbackTarget) rollbackMutation.mutate(rollbackTarget);
                  }}
                  onCancel={closeRollback}
                />
              ) : null
            }
          />
        ) : (
          <div className="tg-subtabs__loading">Загрузка графа гейта…</div>
        )}
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
              retrying={retryingTaskId === selectedTask.task_id}
            />
          ) : null}
        </Drawer>
      </SectionCard>

      <RollbackHistorySection projectId={projectId} />
    </>
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
      {/* Блок «Цель и ключевое состояние» был ручной формой задания цели —
          в текущей архитектуре цель формируется leaf-задачей goal_hypothesis
          и попадает в Layer A автоматически. Ручной ввод тут только дублировал
          результат пайплайна и путал пользователя. */}

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
    files: File[];
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
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);

  useEffect(() => {
    const objectives = objectivesQuery.data;
    if (!objectives?.length || objectiveRef) return;
    // По умолчанию цель — создание ТЗ (common.requirements_specification):
    // самый частый сценарий. Если его нет в реестре — первый в списке.
    const defaultObjective =
      objectives.find((item) => item.objective_ref.startsWith("common.requirements_specification")) ??
      objectives[0];
    if (defaultObjective) {
      setObjectiveRef(defaultObjective.objective_ref);
    }
  }, [objectiveRef, objectivesQuery.data]);

  // Сброс полей не привязан к закрытию: при оптимистичном закрытии на submit
  // ввод должен пережить возможную ошибку создания. Очистка — через remount
  // по ключу (см. AppFrame: createFormKey меняется при явном закрытии/успехе).

  const togglePack = (packRef: string) => {
    setManualPackOverride(true);
    setSelectedPacks((current) =>
      current.includes(packRef) ? current.filter((item) => item !== packRef) : [...current, packRef],
    );
  };

  const addFiles = (files: FileList | null | undefined) => {
    if (!files || files.length === 0) return;
    const incoming = Array.from(files);
    setAttachedFiles((current) => {
      const seen = new Set(current.map((f) => `${f.name}:${f.size}`));
      const merged = [...current];
      for (const file of incoming) {
        const key = `${file.name}:${file.size}`;
        if (!seen.has(key)) {
          seen.add(key);
          merged.push(file);
        }
      }
      return merged;
    });
  };

  const removeFile = (index: number) => {
    setAttachedFiles((current) => current.filter((_, i) => i !== index));
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOver(false);
    addFiles(event.dataTransfer?.files);
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
            files: attachedFiles,
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
                multiple
                accept=".txt,.md,.json,.csv,.pdf,.docx"
                onChange={(event) => {
                  addFiles(event.target.files);
                  event.target.value = "";
                }}
                hidden
              />
              <Paperclip size={14} />
              <span>Прикрепить файлы</span>
            </label>
            <button
              type="button"
              className="create-form__paste-button"
              onClick={handleAppendPaste}
              title="Вставить из буфера обмена (добавит в конец)"
            >
              <ClipboardPaste size={14} />
              <span>Вставить</span>
            </button>
            <span className="create-form__counter">
              {requestCharCount > 0 ? `${requestCharCount} символов` : "пока пусто"}
            </span>
          </div>
        </div>

        {attachedFiles.length > 0 ? (
          <div className="create-form__files">
            <small className="field__hint">
              Файлы будут приложены к проекту и пойдут в контекст (текст из .pdf/.docx/.txt
              извлекается автоматически): {attachedFiles.length}
            </small>
            <ul className="create-form__files-list">
              {attachedFiles.map((file, index) => (
                <li key={`${file.name}:${file.size}:${index}`} className="create-form__files-item">
                  <FileText size={14} className="create-form__files-icon" />
                  <span className="create-form__files-name">{file.name}</span>
                  <span className="create-form__files-size">{formatFileSize(file.size)}</span>
                  <button
                    type="button"
                    className="create-form__files-remove"
                    onClick={() => removeFile(index)}
                    aria-label={`Убрать ${file.name}`}
                  >
                    <X size={14} />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <button
          type="button"
          className="create-form__advanced-toggle"
          onClick={() => setAdvancedOpen((v) => !v)}
          aria-expanded={advancedOpen}
        >
          <ChevronDown
            size={14}
            className={cx(
              "create-form__advanced-caret",
              advancedOpen && "create-form__advanced-caret--open",
            )}
          />
          <span>Дополнительные настройки</span>
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

export default App;
