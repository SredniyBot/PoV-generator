import type { PropsWithChildren, ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  FileCog,
  FileText,
  Layers3,
  LoaderCircle,
  MessageSquareWarning,
  Play,
  Plus,
  RadioTower,
  Sparkles,
  Waypoints,
  X,
} from "lucide-react";

import type {
  ActionDescriptor,
  ArtifactSummaryView,
  JourneyStepView,
  ProjectListItemView,
  ProjectReviewView,
  ProjectShellView,
  ProjectSituationView,
  ProjectStateView,
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
  title: string;
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
}: {
  projects: ProjectListItemView[];
  selectedProjectId: string | null;
  onCreate: () => void;
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
            <Link
              key={project.project_id}
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
          ))
        )}
      </nav>
    </aside>
  );
}

export function WorkspaceTabs({ projectId }: { projectId: string }) {
  const tabs = [
    { to: `/projects/${projectId}/overview`, label: "Обзор" },
    { to: `/projects/${projectId}/artifacts`, label: "Артефакты" },
    { to: `/projects/${projectId}/journey`, label: "Ход выполнения" },
    { to: `/projects/${projectId}/state`, label: "Состояние" },
    { to: `/projects/${projectId}/review`, label: "Замечания" },
    { to: `/projects/${projectId}/debug`, label: "Технические детали" },
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

export function WorkspaceHeader({
  shell,
  connectionStatus,
  actions,
}: {
  shell: ProjectShellView;
  connectionStatus: RealtimeStatus;
  actions?: ReactNode;
}) {
  return (
    <header className="workspace-header">
      <div className="workspace-header__intro">
        <div className="workspace-header__eyebrow">
          <StatusPill tone={shell.status_label === "Готово" ? "success" : "active"}>{shell.status_label}</StatusPill>
          <span>Обновлено {formatDateTime(shell.updated_at)}</span>
        </div>
        <h1>{shell.name}</h1>
        <p>{shell.business_request}</p>
        <div className="workspace-header__meta">
          <span className="meta-chip">
            <Waypoints size={14} />
            {shell.recipe_ref}
          </span>
          {shell.enabled_domain_packs.map((pack) => (
            <span key={pack} className="meta-chip meta-chip--accent">
              <Layers3 size={14} />
              {pack}
            </span>
          ))}
          {shell.goal ? (
            <span className="meta-chip meta-chip--goal">
              <Sparkles size={14} />
              {shell.goal}
            </span>
          ) : null}
        </div>
      </div>
      <div className="workspace-header__side">
        <ConnectionBadge status={connectionStatus} />
        {actions}
      </div>
    </header>
  );
}

export function CommandBar({
  provider,
  model,
  onProviderChange,
  onModelChange,
  onRunNext,
  onRunUntilBlocked,
  pending,
}: {
  provider: string;
  model: string;
  onProviderChange: (value: string) => void;
  onModelChange: (value: string) => void;
  onRunNext: () => void;
  onRunUntilBlocked: () => void;
  pending: boolean;
}) {
  return (
    <div className="command-bar">
      <div className="command-bar__context">
        <strong>Управление выполнением</strong>
        <p>Запускайте следующий шаг или ведите проект до ближайшей осмысленной остановки.</p>
      </div>
      <div className="command-bar__controls">
        <label className="field">
          <span>Провайдер</span>
          <select value={provider} onChange={(event) => onProviderChange(event.target.value)}>
            <option value="stub">stub</option>
            <option value="openrouter">openrouter</option>
          </select>
        </label>
        <label className="field">
          <span>Модель</span>
          <input
            value={model}
            onChange={(event) => onModelChange(event.target.value)}
            placeholder="openai/gpt-4.1-mini"
          />
        </label>
      </div>
      <div className="command-bar__actions">
        <Button className="command-bar__button" tone="secondary" icon={<Play size={16} />} onClick={onRunNext} busy={pending}>
          Следующий шаг
        </Button>
        <Button
          className="command-bar__button"
          tone="primary"
          icon={<Sparkles size={16} />}
          onClick={onRunUntilBlocked}
          busy={pending}
        >
          Выполнить до блокировки
        </Button>
      </div>
    </div>
  );
}

export function JourneyStrip({
  steps,
  onOpenStep,
  flash,
}: {
  steps: JourneyStepView[];
  onOpenStep: (step: JourneyStepView) => void;
  flash?: boolean;
}) {
  return (
    <SectionCard title="Путь проекта" className={cx("journey-card", flash && "live-flash")}>
      <div className="journey-strip">
        {steps.map((step, index) => {
          const tone =
            step.status === "completed"
              ? "success"
              : step.is_current
                ? "active"
                : step.status === "blocked"
                  ? "danger"
                  : "muted";
          return (
            <button key={step.step_id} className={cx("journey-step", step.is_current && "journey-step--current")} onClick={() => onOpenStep(step)} type="button">
              <div className="journey-step__eyebrow">
                <span>{`Шаг ${index + 1}`}</span>
              </div>
              <span className="journey-step__title">{step.title}</span>
              <div className="journey-step__meta">
                <StatusPill tone={tone}>{prettyLabel(step.status)}</StatusPill>
                <span>{step.source_kind === "recipe_fragment" ? "Фрагмент" : "Базовый рецепт"}</span>
              </div>
            </button>
          );
        })}
      </div>
    </SectionCard>
  );
}

function actionIcon(kind: string): ReactNode {
  if (kind.includes("review")) return <MessageSquareWarning size={16} />;
  if (kind.includes("artifact")) return <FileText size={16} />;
  if (kind.includes("journey")) return <Waypoints size={16} />;
  if (kind.includes("debug")) return <FileCog size={16} />;
  return <ArrowRight size={16} />;
}

export function SituationPanel({
  situation,
  onAction,
  flash,
}: {
  situation: ProjectSituationView;
  onAction: (action: ActionDescriptor) => void;
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
      {situation.secondary_actions.length > 0 ? (
        <div className="inline-actions">
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
                  <StatusPill tone={entry.status === "blocked" ? "danger" : entry.status === "completed" ? "success" : "muted"}>
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
            <span>Цель</span>
            <strong>{state.goal ?? "Не зафиксирована"}</strong>
          </div>
          <div className="mini-metric">
            <span>Активные gaps</span>
            <strong>{state.active_gaps.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Readiness</span>
            <strong>{state.readiness.length}</strong>
          </div>
          <div className="mini-metric">
            <span>Domain packs</span>
            <strong>{state.enabled_domain_packs.length}</strong>
          </div>
        </div>
        <Link className="text-link" to={`/projects/${projectId}/state`}>
          Открыть состояние проекта
        </Link>
      </SectionCard>
    </div>
  );
}
