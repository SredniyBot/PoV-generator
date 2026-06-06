import type { CSSProperties, PropsWithChildren, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink } from "react-router-dom";

import { api as apiClient } from "./api";
import { activeRunRefetchInterval } from "./realtime";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  FileCog,
  FileText,
  GitBranch,
  Layers3,
  LoaderCircle,
  MessageSquareWarning,
  Plus,
  RadioTower,
  RefreshCcw,
  Settings,
  Sparkles,
  Trash2,
  Waypoints,
  X,
} from "lucide-react";
// MessageSquareWarning остаётся в импорте — используется в actionIcon()
// и SituationPanel'е, не только в legacy «Вопросы: N»-кнопке.

import type {
  ActionDescriptor,
  ArtifactSummaryView,
  ProjectListItemView,
  ProjectReviewView,
  ProjectShellView,
  ProjectSituationView,
  ProjectStateView,
  TaskNodeView,
  TimelineEntryView,
} from "./types";
import type { RealtimeStatus } from "./useProjectRealtime";

export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function prettyLabel(input: string | null | undefined): string {
  if (!input) {
    return "—";
  }
  const normalized = input.trim().toLowerCase();
  const labels: Record<string, string> = {
    info: "Информация",
    success: "Успех",
    warning: "Предупреждение",
    error: "Ошибка",
    failed: "Ошибка",
    passed: "Пройдено",
    missing: "Отсутствует",
    active: "Активно",
    open: "Открыто",
    answered: "Отвечено",
    assumed: "Допущение",
    deferred: "Отложено",
    low: "Низкий",
    medium: "Средний",
    high: "Высокий",
    critical: "Критичный",
    task: "Задача",
    subtree: "Поддерево",
    none: "Не блокирует",
    completed: "Завершено",
    blocked: "Заблокировано",
    queued: "В очереди",
    pending: "Ожидание",
    in_progress: "Выполняется",
    waiting_for_children: "Ожидает подзадачи",
    done: "Готово",
    waiting: "Ожидает",
    candidate: "Кандидат",
    obsolete: "Устарело",
    needs_changes: "Нужны правки",
    needs_user_input: "Нужен ввод пользователя",
    objective: "Цель",
    child: "Подзадача",
    domain_pack: "Доменный пакет",
    manual: "Ручное действие",
  };
  if (labels[normalized]) {
    return labels[normalized];
  }
  return input
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function StatusPill({
  tone,
  children,
}: PropsWithChildren<{ tone: "neutral" | "active" | "success" | "warning" | "danger" | "muted" }>) {
  return <span className={cx("status-pill", `status-pill--${tone}`)}>{children}</span>;
}

export function Button({
  children,
  tone = "secondary",
  icon,
  onClick,
  type = "button",
  disabled,
  busy = false,
  className,
}: {
  children: ReactNode;
  tone?: "primary" | "secondary" | "ghost" | "danger";
  icon?: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
  busy?: boolean;
  className?: string;
}) {
  return (
    <button
      className={cx("button", `button--${tone}`, className)}
      onClick={onClick}
      type={type}
      disabled={disabled || busy}
    >
      {busy ? <LoaderCircle className="button__spinner" size={16} /> : icon}
      <span>{children}</span>
    </button>
  );
}

export function IconButton({
  label,
  icon,
  onClick,
}: {
  label: string;
  icon: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="icon-button" aria-label={label} title={label} onClick={onClick} type="button">
      {icon}
    </button>
  );
}

export function SectionCard({
  title,
  subtitle,
  actions,
  tone = "default",
  className,
  children,
}: PropsWithChildren<{
  // ReactNode (а не только string) — чтобы в title можно было передать
  // композицию с кнопкой возврата / иконкой (v3.0 CheckpointSessionPage).
  // h2 принимает любой children, обратной несовместимости не возникает.
  title: ReactNode;
  subtitle?: string;
  actions?: ReactNode;
  tone?: "default" | "warning" | "danger" | "accent";
  className?: string;
}>) {
  return (
    <section className={cx("section-card", `section-card--${tone}`, className)}>
      <header className="section-card__header">
        <div>
          <h2 className="section-card__title">{title}</h2>
          {subtitle ? <p className="section-card__subtitle">{subtitle}</p> : null}
        </div>
        {actions ? <div className="section-card__actions">{actions}</div> : null}
      </header>
      <div className="section-card__body">{children}</div>
    </section>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state__icon">{icon ?? <Sparkles size={18} />}</div>
      <div className="empty-state__content">
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  );
}

export function LoadingPanel({ title = "Загрузка данных…" }: { title?: string }) {
  return (
    <SectionCard title={title}>
      <div className="skeleton-stack">
        <div className="skeleton skeleton--line skeleton--lg" />
        <div className="skeleton skeleton--line" />
        <div className="skeleton skeleton--line skeleton--sm" />
      </div>
    </SectionCard>
  );
}

export function Modal({
  open,
  title,
  onClose,
  children,
}: PropsWithChildren<{ open: boolean; title: string; onClose: () => void }>) {
  if (!open) {
    return null;
  }
  return (
    <div className="overlay" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal__header">
          <div>
            <h2>{title}</h2>
          </div>
          <IconButton label="Закрыть" icon={<X size={18} />} onClick={onClose} />
        </header>
        <div className="modal__body">{children}</div>
      </div>
    </div>
  );
}

export function Drawer({
  open,
  title,
  onClose,
  children,
}: PropsWithChildren<{ open: boolean; title: string; onClose: () => void }>) {
  if (!open) {
    return null;
  }
  return (
    <div className="overlay overlay--drawer" role="presentation" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer__header">
          <h2>{title}</h2>
          <IconButton label="Закрыть" icon={<X size={18} />} onClick={onClose} />
        </header>
        <div className="drawer__body">{children}</div>
      </aside>
    </div>
  );
}

export function ProjectRail({
  projects,
  selectedProjectId,
  onCreate,
  onDeleteProject,
  deletingProjectId,
}: {
  projects: ProjectListItemView[];
  selectedProjectId: string | null;
  onCreate: () => void;
  onDeleteProject: (project: { project_id: string; name: string }) => void;
  deletingProjectId: string | null;
}) {
  return (
    <aside className="project-rail">
      <div className="brand-panel">
        <div className="brand-mark">
          <span className="brand-mark__core" />
        </div>
        <div className="brand-copy">
          <strong>PoV Generator</strong>
          <span>Operator Workspace</span>
        </div>
      </div>
      <Button tone="primary" icon={<Plus size={16} />} onClick={onCreate}>
        Новый проект
      </Button>
      <div className="project-rail__header">
        <span>Проекты</span>
        <span>{projects.length}</span>
      </div>
      <nav className="project-rail__list" aria-label="Проекты">
        {projects.length === 0 ? (
          <EmptyState
            title="Проектов пока нет"
            description="Создайте первый кейс прямо из интерфейса."
            icon={<Layers3 size={18} />}
          />
        ) : (
          projects.map((project) => (
            // Кнопка удаления — сиблинг <Link>, а не потомок (button внутри
            // <a> невалиден по HTML5). Обёртка задаёт positioning-контекст.
            <div key={project.project_id} className="project-item-wrap">
              <Link
                className={cx(
                  "project-item",
                  selectedProjectId === project.project_id && "project-item--active",
                  project.has_blockers && "project-item--blocked",
                )}
                to={`/projects/${project.project_id}/overview`}
              >
                <div className="project-item__topline">
                  <strong>{project.name}</strong>
                  {project.has_blockers ? <AlertTriangle size={14} /> : <ChevronRight size={14} />}
                </div>
                <div className="project-item__meta">
                  <StatusPill tone={project.has_blockers ? "danger" : "muted"}>{project.status_label}</StatusPill>
                  <span>{formatDateTime(project.updated_at)}</span>
                </div>
                <p className="project-item__step">{project.current_step_title ?? "Шаг пока не выбран"}</p>
              </Link>
              <button
                type="button"
                className="project-item__delete"
                title="Удалить проект"
                aria-label={`Удалить проект «${project.name}»`}
                disabled={deletingProjectId === project.project_id}
                onClick={() => {
                  const confirmed = window.confirm(
                    `Удалить проект «${project.name}»?\n\nБудут безвозвратно удалены все артефакты, решения и история. Действие необратимо.`,
                  );
                  if (confirmed) {
                    onDeleteProject({ project_id: project.project_id, name: project.name });
                  }
                }}
              >
                {deletingProjectId === project.project_id ? (
                  <LoaderCircle size={14} className="spin" />
                ) : (
                  <Trash2 size={14} />
                )}
              </button>
            </div>
          ))
        )}
      </nav>
      {/* «Настройки» — внизу рейла. Системные настройки (LLM-провайдеры,
          модели, назначения), не связанные с конкретным проектом. */}
      <div className="project-rail__footer">
        <Link to="/settings" className="rail-link" title="Настройки LLM-провайдеров и моделей">
          <Settings size={14} /> Настройки
        </Link>
      </div>
    </aside>
  );
}

export function WorkspaceTabs({ projectId }: { projectId: string }) {
  // 5 вкладок проекта. «⚙ Настройки» убран — он создавал путаницу с
  // root-level страницей `/settings` (LLM-провайдеры). Содержимое
  // прошлого таба (Состояние / Замечания / Технические детали) — это
  // диагностические страницы; доступ к ним остаётся через прямые
  // URL `/state`, `/review`, `/debug` для bookmarks / power-users.
  // v3.1: legacy «Вопросы» и «Журнал решений» удалены — Decision (v3.0
  // реестр) полностью покрывает оба сценария.
  const tabs = [
    { to: `/projects/${projectId}/overview`, label: "Обзор" },
    { to: `/projects/${projectId}/artifacts`, label: "Артефакты" },
    { to: `/projects/${projectId}/decisions`, label: "Решения" },
    { to: `/projects/${projectId}/requisites`, label: "Реквизиты" },
    { to: `/projects/${projectId}/task-graph`, label: "Задачи" },
    { to: `/projects/${projectId}/methodology`, label: "Методология" },
  ];
  return (
    <div className="tabs">
      {tabs.map((tab) => (
        <NavLink key={tab.to} to={tab.to} className={({ isActive }) => cx("tabs__item", isActive && "tabs__item--active")}>
          {tab.label}
        </NavLink>
      ))}
    </div>
  );
}

export function ConnectionBadge({ status }: { status: RealtimeStatus }) {
  const tone = status === "connected" ? "success" : status === "degraded" ? "warning" : "muted";
  const label =
    status === "connected"
      ? "Realtime активен"
      : status === "connecting"
        ? "Подключение…"
        : status === "degraded"
          ? "Realtime недоступен"
          : "Realtime отключён";
  return (
    <div className="connection-badge">
      <StatusPill tone={tone}>
        <RadioTower size={12} />
        {label}
      </StatusPill>
    </div>
  );
}

const CLARIFICATION_MODE_OPTIONS = {
  autopilot: {
    label: "Автопилот",
    description: "Система спрашивает только критичные вопросы, остальное фиксирует как допущения.",
  },
  balanced: {
    label: "Сбалансированный",
    description: "Система спрашивает блокирующие и высоко влияющие вопросы. Режим по умолчанию.",
  },
  control: {
    label: "Контроль",
    description: "Система чаще просит подтверждения по важным решениям, не показывая технический шум.",
  },
  expert: {
    label: "Экспертный",
    description: "Система показывает больше спорных вопросов, причин и вариантов для ручного контроля.",
  },
} as const;

export function WorkspaceHeader({
  shell,
  connectionStatus,
  clarificationMode,
  onClarificationModeChange,
  modePending,
  actions,
  pendingCheckpointCount,
  pendingCheckpointSessionId,
  onOpenCheckpoints,
  onActivateNextObjective,
  activatingNextObjective,
}: {
  shell: ProjectShellView;
  connectionStatus: RealtimeStatus;
  clarificationMode?: string;
  onClarificationModeChange?: (mode: string) => void;
  modePending?: boolean;
  actions?: ReactNode;
  // v3.0: pending checkpoint-сессии (см. /api/projects/{id}/checkpoints).
  // Если > 0 — показываем красный бэйдж; клик ведёт на /checkpoints
  // (список) или сразу на /checkpoints/{id} если одна.
  pendingCheckpointCount?: number;
  pendingCheckpointSessionId?: string | null;
  onOpenCheckpoints?: () => void;
  // Цепочка objective'ов: когда текущий objective завершён и у него есть
  // compatible_next_objectives, UI показывает кнопку перехода.
  onActivateNextObjective?: (objectiveRef: string) => void;
  activatingNextObjective?: boolean;
}) {
  const selectedMode = clarificationMode && clarificationMode in CLARIFICATION_MODE_OPTIONS ? clarificationMode : "balanced";
  const selectedModeOption = CLARIFICATION_MODE_OPTIONS[selectedMode as keyof typeof CLARIFICATION_MODE_OPTIONS];
  return (
    <header className="workspace-header">
      <div className="workspace-header__intro">
        <div className="workspace-header__eyebrow">
          <StatusPill tone={shell.status_label === "Готово" ? "success" : "active"}>{shell.status_label}</StatusPill>
          <span>Обновлено {formatDateTime(shell.updated_at)}</span>
        </div>
        <h1>{shell.name}</h1>
        <p className="workspace-header__request">{shell.business_request}</p>
        <div className="workspace-header__meta">
          {shell.objective_history && shell.objective_history.length > 0 ? (
            <>
              {shell.objective_history.map((ref) => (
                <span key={ref} className="meta-chip meta-chip--muted" title="Завершённый objective">
                  <CheckCircle2 size={14} />
                  {ref}
                </span>
              ))}
              <ChevronRight size={14} className="meta-chip__sep" />
            </>
          ) : null}
          <span className="meta-chip">
            <Waypoints size={14} />
            {shell.objective_ref}
          </span>
          <span className="meta-chip meta-chip--accent">
            <Layers3 size={14} />
            Доменов: {shell.active_domain_packs.length}
          </span>
          {/* CTA «Перейти к следующему этапу» переехала в StageStatusBar
              (степпер над вкладками) — здесь больше не дублируется. */}
          {pendingCheckpointCount && pendingCheckpointCount > 0 ? (
            <button
              type="button"
              className="meta-chip meta-chip--button meta-chip--danger"
              onClick={onOpenCheckpoints}
              title="Workflow приостановлен — нужны ваши решения перед сборкой артефакта"
            >
              <GitBranch size={14} />
              Решения ждут: {pendingCheckpointCount}
            </button>
          ) : null}
        </div>
      </div>
      <div className="workspace-header__side">
        <ConnectionBadge status={connectionStatus} />
        {onClarificationModeChange ? (
          <div className="mode-control">
            <label className="field">
              <span>Режим участия пользователя</span>
              <select
                value={selectedMode}
                onChange={(event) => onClarificationModeChange(event.target.value)}
                disabled={modePending}
              >
                {Object.entries(CLARIFICATION_MODE_OPTIONS).map(([value, option]) => (
                  <option key={value} value={value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <p>{selectedModeOption.description}</p>
          </div>
        ) : null}
        {actions}
      </div>
    </header>
  );
}

export function CommandBar({
  projectId,
}: {
  // L6-10: глобальная шапка = слой СТАТУСА, а не команд.
  //
  // Старое поведение (вечная кнопка «Продолжить» на каждой вкладке) давало
  // ложный сигнал «проект простаивает» и могла предлагать запуск даже когда
  // нужно сперва ответить на вопросы. См. USERS_AND_JTBD §5B C1 (trust
  // calibration): UI должен честно отражать состояние, а не подталкивать к
  // действию когда оно невозможно.
  //
  // Теперь шапка показывает один компактный индикатор:
  //   - "Идёт работа"           + "Приостановить" (единственная глобальная команда)
  //   - "Готово"                 (статус, без действия)
  //   - ""                       (idle/блокеры) — команды живут в Обзоре,
  //                              блокеры уже показаны бейджем в шапке выше
  projectId: string;
}) {
  const queryClient = useQueryClient();
  const activeRunQuery = useQuery({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => apiClient.getActiveWorkflowRun(projectId),
    // Прогресс инвалидируется WS-пушем (workflow_runs); полл — страховка
    // только пока run идёт, на простое off.
    refetchInterval: activeRunRefetchInterval,
  });
  const pauseMutation = useMutation({
    mutationFn: (runId: string) => apiClient.cancelWorkflow(projectId, runId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
    },
  });

  const activeRun = activeRunQuery.data ?? null;
  const isRunning =
    activeRun !== null && (activeRun.status === "running" || activeRun.status === "pending");

  if (!isRunning) {
    return null;
  }

  const pausing = pauseMutation.isPending || Boolean(activeRun?.cancel_requested);
  return (
    <div className="command-bar command-bar--running">
      <span className="command-bar__pulse" aria-hidden />
      <span className="command-bar__status">Идёт работа</span>
      <button
        type="button"
        className="command-bar__pause"
        onClick={() => activeRun && pauseMutation.mutate(activeRun.run_id)}
        disabled={pausing}
      >
        {pausing ? "Останавливаем…" : "Приостановить"}
      </button>
    </div>
  );
}

export function TaskGraphTree({
  nodes,
  onOpenTask,
  flash,
}: {
  nodes: TaskNodeView[];
  onOpenTask: (task: TaskNodeView) => void;
  flash?: boolean;
}) {
  return (
    <SectionCard title="Карта зависимостей" subtitle="Это структура работ, а не порядок выполнения: система запускает любые допустимые задачи" className={cx("task-graph-card", flash && "live-flash")}>
      <div className="task-graph-tree task-graph-tree--stacked">
        {nodes.length === 0 ? (
          <EmptyState title="Граф пока пуст" description="Он появится после создания или перепланирования проекта." />
        ) : (
          nodes.map((node) => (
            <TaskGraphNode key={node.task_id} node={node} onOpenTask={onOpenTask} />
          ))
        )}
      </div>
    </SectionCard>
  );
}

function TaskGraphNode({
  node,
  onOpenTask,
}: {
  node: TaskNodeView;
  onOpenTask: (task: TaskNodeView) => void;
}) {
  const tone =
    node.status === "completed"
      ? "success"
      : node.status === "failed"
        ? "danger"
        : node.blocking_clarification_count > 0
          ? "warning"
          : node.is_current || node.status === "ready"
            ? "active"
            : "muted";
  return (
    <div className="task-graph-node" style={{ "--task-depth": node.depth } as CSSProperties}>
            <button
              className={cx(
                "task-node",
                "task-node--row",
                node.is_current && "task-node--current",
                node.status === "failed" && "task-node--danger",
                node.blocking_clarification_count > 0 && "task-node--warning",
              )}
              onClick={() => onOpenTask(node)}
              type="button"
            >
              <div className={cx("task-node__marker", `task-node__marker--${tone}`)} />
              <div className="task-node__content">
          <span className="task-node__title">{node.title}</span>
          {node.status_summary ? <p className="task-node__summary">{node.status_summary}</p> : null}
              </div>
              <div className="task-node__meta">
          <StatusPill tone={tone}>{prettyLabel(node.status)}</StatusPill>
          {node.blocking_clarification_count > 0 ? (
            <StatusPill tone="warning">{node.blocking_clarification_count} уточн.</StatusPill>
          ) : null}
          <span>{prettyLabel(node.template_type)}</span>
          <span>{prettyLabel(node.origin_kind)}</span>
          {node.slot_id ? <span>{node.slot_id}</span> : null}
              </div>
            </button>
      {node.children.length > 0 ? (
        <div className="task-graph-node__children">
          {node.children.map((child) => (
            <TaskGraphNode key={child.task_id} node={child} onOpenTask={onOpenTask} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function actionIcon(kind: string): ReactNode {
  if (kind.includes("review")) return <MessageSquareWarning size={16} />;
  if (kind.includes("artifact")) return <FileText size={16} />;
  if (kind.includes("task_graph")) return <Waypoints size={16} />;
  if (kind.includes("debug")) return <FileCog size={16} />;
  return <ArrowRight size={16} />;
}

export function SituationPanel({
  situation,
  onAction,
  onRetryTask,
  retryTaskId,
  flash,
}: {
  situation: ProjectSituationView;
  onAction: (action: ActionDescriptor) => void;
  onRetryTask?: (taskId: string) => void;
  retryTaskId?: string | null;
  flash?: boolean;
}) {
  return (
    <SectionCard
      title="Текущая ситуация"
      subtitle={situation.blocking ? "Процесс требует внимания" : "Проект можно вести дальше"}
      tone={situation.blocking ? "warning" : "accent"}
      className={cx("situation-panel", flash && "live-flash")}
      actions={
        situation.primary_action ? (
          <Button
            tone={situation.blocking ? "danger" : "primary"}
            icon={actionIcon(situation.primary_action.kind)}
            onClick={() => onAction(situation.primary_action!)}
          >
            {situation.primary_action.label}
          </Button>
        ) : undefined
      }
    >
      <div className="situation-panel__headline-row">
        <div>
          <h3 className="hero-title">{situation.headline}</h3>
          <p className="muted-copy">{situation.summary}</p>
        </div>
        <StatusPill tone={situation.blocking ? "danger" : "active"}>{situation.status_label}</StatusPill>
      </div>
      {situation.blockers.length > 0 ? (
        <div className="blocker-list">
          {situation.blockers.slice(0, 3).map((blocker) => (
            <article key={`${blocker.kind}-${blocker.related_id ?? blocker.summary}`} className="blocker-card">
              <div className="blocker-card__head">
                <AlertTriangle size={16} />
                <strong>{blocker.title}</strong>
                <StatusPill tone={blocker.severity === "high" ? "danger" : "warning"}>{blocker.severity}</StatusPill>
              </div>
              <p>{blocker.summary}</p>
            </article>
          ))}
        </div>
      ) : null}
      {onRetryTask && retryTaskId || situation.secondary_actions.length > 0 ? (
        <div className="inline-actions">
          {onRetryTask && retryTaskId ? (
            <button className="inline-actions__item" onClick={() => onRetryTask(retryTaskId)} type="button">
              <RefreshCcw size={16} />
              <span>Повторить шаг</span>
            </button>
          ) : null}
          {situation.secondary_actions.map((action) => (
            <button key={action.kind + action.label} className="inline-actions__item" onClick={() => onAction(action)} type="button">
              {actionIcon(action.kind)}
              <span>{action.label}</span>
            </button>
          ))}
        </div>
      ) : null}
    </SectionCard>
  );
}

export function TimelineFeed({
  entries,
  onOpenEntry,
  recentSequences,
  flash,
}: {
  entries: TimelineEntryView[];
  onOpenEntry: (entry: TimelineEntryView) => void;
  recentSequences: number[];
  flash?: boolean;
}) {
  return (
    <SectionCard
      title="Операционная лента"
      subtitle="Ключевые события проекта в человекочитаемом виде"
      className={cx("timeline-card", flash && "live-flash")}
    >
      <div className="timeline-feed">
        {entries.length === 0 ? (
          <EmptyState title="Событий пока нет" description="Лента начнёт заполняться по мере выполнения шагов." />
        ) : (
          entries.map((entry) => (
            <button
              key={entry.sequence}
              type="button"
              className={cx("timeline-entry", recentSequences.includes(entry.sequence) && "timeline-entry--fresh")}
              onClick={() => onOpenEntry(entry)}
            >
              <div className="timeline-entry__line" />
              <div className="timeline-entry__body">
                <div className="timeline-entry__head">
                  <strong>{entry.title}</strong>
                  <StatusPill tone={entry.status === "error" || entry.status === "blocked" ? "danger" : entry.status === "warning" ? "warning" : entry.status === "completed" || entry.status === "success" ? "success" : "muted"}>
                    {prettyLabel(entry.status)}
                  </StatusPill>
                </div>
                <p>{entry.summary}</p>
                <div className="timeline-entry__meta">
                  <span>{formatDateTime(entry.created_at)}</span>
                  <span className="timeline-entry__cta">
                    Открыть детали
                    <ChevronRight size={14} />
                  </span>
                </div>
              </div>
            </button>
          ))
        )}
      </div>
    </SectionCard>
  );
}

export function ArtifactRail({
  projectId,
  artifacts,
  review,
  state,
  flashArtifacts,
}: {
  projectId: string;
  artifacts: ArtifactSummaryView[];
  review: ProjectReviewView;
  state: ProjectStateView;
  flashArtifacts?: boolean;
}) {
  return (
    <div className="side-stack">
      <SectionCard title="Ключевые артефакты" className={flashArtifacts ? "live-flash" : undefined}>
        <div className="artifact-rail">
          {artifacts.length === 0 ? (
            <EmptyState title="Артефактов пока нет" description="Они появятся после первых шагов workflow." />
          ) : (
            artifacts.map((artifact) => (
              <Link
                key={artifact.artifact_id}
                className="artifact-card"
                to={`/projects/${projectId}/artifacts/${artifact.artifact_id}`}
              >
                <div className="artifact-card__head">
                  <strong>{artifact.title}</strong>
                  <StatusPill tone={artifact.has_markdown ? "success" : "muted"}>{artifact.artifact_role}</StatusPill>
                </div>
                <div className="artifact-card__meta">
                  <span>{formatDateTime(artifact.created_at)}</span>
                  <span>{artifact.created_by_task_id ?? "system"}</span>
                </div>
              </Link>
            ))
          )}
        </div>
      </SectionCard>

      <SectionCard title="Ревью и замечания" tone={review.status === "needs_changes" ? "warning" : "default"}>
        {review.status === "missing" ? (
          <EmptyState title="Ревью ещё не запускалось" description="Замечания появятся после review-шага." />
        ) : (
          <div className="compact-review">
            <div className="compact-review__head">
              <StatusPill tone={review.status === "passed" ? "success" : review.status === "needs_changes" ? "warning" : "muted"}>
                {prettyLabel(review.status)}
              </StatusPill>
              <span>{review.updated_at ? formatDateTime(review.updated_at) : "—"}</span>
            </div>
            <p>{review.summary ?? "Сводка ревью отсутствует."}</p>
            {review.issues.slice(0, 3).map((issue, index) => (
              <div key={`${issue.message}-${index}`} className="review-issue-preview">
                <StatusPill tone={issue.severity === "high" ? "danger" : "warning"}>{issue.severity}</StatusPill>
                <span>{issue.message}</span>
              </div>
            ))}
            <Link className="text-link" to={`/projects/${projectId}/review`}>
              Открыть раздел замечаний
            </Link>
          </div>
        )}
      </SectionCard>

      <SectionCard title="Состояние проекта">
        <div className="state-mini-grid">
          <div className="mini-metric">
            <span>Допущения</span>
            <strong>{state.assumptions.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Решения</span>
            <strong>{state.decisions.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Открытые gaps</span>
            <strong>{state.active_gaps.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Домены</span>
            <strong>{state.active_domain_packs.length}</strong>
          </div>
        </div>
        <Link className="text-link" to={`/projects/${projectId}/state`}>
          Открыть состояние проекта
        </Link>
      </SectionCard>
    </div>
  );
}
