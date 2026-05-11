/**
 * L6-1: Goal-anchored mission control (P3 v2 из USERS_AND_JTBD.md §5B).
 *
 * Двухпанельный главный экран проекта:
 * - Левая часть (артефакт): skeleton артефакта со статусами разделов.
 * - Правая часть (статус): primary CTA + прогресс + текущая активность
 *   + ярлыки на «Вопросы» и «Журнал решений».
 *
 * Закрывает 4 вопроса менеджера с главного экрана:
 *   состояние, что от меня, как продвинуться, когда будет.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "./api";
import type {
  ArtifactSectionStatus,
  ArtifactSkeletonView,
  ObjectiveProgressView,
} from "./types";

interface ProjectOverviewV2Props {
  projectId: string;
  isRunning?: boolean;
  onOpenClarifications?: () => void;
  onOpenDecisionLog?: () => void;
  onOpenArtifactSection?: (artifactId: string, sectionId: string) => void;
  onRunNext?: () => void;
  onCancelRun?: () => void;
}

export function ProjectOverviewV2({
  projectId,
  isRunning,
  onOpenClarifications,
  onOpenDecisionLog,
  onOpenArtifactSection,
  onRunNext,
  onCancelRun,
}: ProjectOverviewV2Props) {
  const overview = useQuery({
    queryKey: ["overview-v2", projectId],
    queryFn: () => api.getOverview(projectId),
  });

  const clarifications = useQuery({
    queryKey: ["clarifications-v2", projectId],
    queryFn: () => api.getClarifications(projectId),
  });

  const decisionLog = useQuery({
    queryKey: ["decisions-v2", projectId],
    queryFn: () => api.getDecisionLog(projectId),
  });

  const primaryArtifactId = overview.data?.key_artifacts?.[0]?.artifact_id;

  const skeleton = useQuery({
    queryKey: ["skeleton-v2", projectId, primaryArtifactId],
    queryFn: () => api.getArtifactSkeleton(projectId, primaryArtifactId!),
    enabled: Boolean(primaryArtifactId),
  });

  const blockingCount = clarifications.data?.blocking_count ?? 0;
  const openCount = clarifications.data?.open_count ?? 0;
  const assumedCount = decisionLog.data?.assumed_count ?? 0;
  const decisionsTotal = decisionLog.data?.total_count ?? 0;

  const primaryCta = useMemo(
    () =>
      computePrimaryCta({
        blockingCount,
        isRunning: Boolean(isRunning),
        artifactProgress: overview.data?.objective_progress,
        onOpenClarifications,
        onCancelRun,
        onRunNext,
      }),
    [
      blockingCount,
      isRunning,
      overview.data?.objective_progress,
      onOpenClarifications,
      onCancelRun,
      onRunNext,
    ],
  );

  return (
    <section className="overview-mc">
      <div className="overview-mc__layout">
        <article className="overview-mc__artifact" aria-labelledby="overview-mc-title">
          <header className="overview-mc__artifact-header">
            <div className="overview-mc__artifact-headline">
              <p className="overview-mc__artifact-eyebrow">
                {humanizeArtifactRole(skeleton.data?.artifact_role)}
              </p>
              <h1 id="overview-mc-title" className="overview-mc__artifact-title">
                {skeleton.data?.title ??
                  overview.data?.key_artifacts?.[0]?.title ??
                  "Артефакт ещё формируется"}
              </h1>
            </div>
            {skeleton.data && (
              <span className="overview-mc__artifact-progress">
                <strong>
                  {skeleton.data.sections_done}/{skeleton.data.sections_total}
                </strong>
                <span>разделов</span>
              </span>
            )}
          </header>

          {skeleton.isLoading && primaryArtifactId ? (
            <div className="skeleton-placeholder skeleton-placeholder--loading">
              Загружаем структуру артефакта…
            </div>
          ) : skeleton.data ? (
            <ul className="skeleton-list" role="list">
              {skeleton.data.sections.map((section) => (
                <SkeletonSectionItem
                  key={section.section_id}
                  section={section}
                  onClick={() =>
                    primaryArtifactId &&
                    onOpenArtifactSection?.(primaryArtifactId, section.section_id)
                  }
                />
              ))}
            </ul>
          ) : (
            <div className="skeleton-placeholder">
              <p className="skeleton-placeholder__title">Артефакт ещё формируется системой</p>
              <p className="skeleton-placeholder__activity">
                {overview.data?.current_activity ??
                  "Идёт сбор фактов из бизнес-запроса. Скоро появится скелет ТЗ."}
              </p>
            </div>
          )}
        </article>

        <aside className="overview-mc__status" aria-label="Статус и действия">
          <div className="status-card status-card--primary">
            <div className="status-card__eyebrow">Что сейчас нужно</div>
            <div className="status-card__headline">{primaryCta.headline}</div>
            {primaryCta.detail && <p className="status-card__detail">{primaryCta.detail}</p>}
            {primaryCta.action && (
              <button
                type="button"
                className={`status-card__cta${
                  primaryCta.action.tone === "danger" ? " status-card__cta--danger" : ""
                }`}
                onClick={primaryCta.action.onClick}
                disabled={primaryCta.action.disabled}
              >
                {primaryCta.action.label}
              </button>
            )}
          </div>

          <div className="status-card">
            <div className="status-card__eyebrow">Прогресс цели</div>
            <ProgressRow
              label="Артефакты"
              done={overview.data?.objective_progress?.artifacts_ready}
              total={overview.data?.objective_progress?.artifacts_required}
            />
            <ProgressRow
              label="Проверки"
              done={overview.data?.objective_progress?.gates_passed}
              total={overview.data?.objective_progress?.gates_required}
            />
          </div>

          {overview.data?.current_activity && (
            <div className="status-card status-card--ghost">
              <div className="status-card__eyebrow">Идёт сейчас</div>
              <p className="status-card__detail">{overview.data.current_activity}</p>
            </div>
          )}

          {openCount > 0 && (
            <button
              type="button"
              className="overview-mc__shortcut"
              onClick={onOpenClarifications}
            >
              <span className="overview-mc__shortcut-title">Открытые вопросы</span>
              <span className="overview-mc__shortcut-counter">
                <strong>{openCount}</strong>
                {assumedCount > 0 && (
                  <span className="overview-mc__auto-badge" title="Авто-решений">
                    🤖 {assumedCount}
                  </span>
                )}
              </span>
            </button>
          )}

          {decisionsTotal > 0 && (
            <button
              type="button"
              className="overview-mc__shortcut"
              onClick={onOpenDecisionLog}
            >
              <span className="overview-mc__shortcut-title">Журнал решений</span>
              <span className="overview-mc__shortcut-counter">
                <strong>{decisionsTotal}</strong>
              </span>
            </button>
          )}
        </aside>
      </div>
    </section>
  );
}

// ---- helpers ---------------------------------------------------------------

interface CtaAction {
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  tone?: "primary" | "danger";
}

interface CtaInfo {
  headline: string;
  detail?: string;
  action?: CtaAction;
}

function computePrimaryCta(input: {
  blockingCount: number;
  isRunning: boolean;
  artifactProgress?: ObjectiveProgressView;
  onOpenClarifications?: () => void;
  onCancelRun?: () => void;
  onRunNext?: () => void;
}): CtaInfo {
  if (input.blockingCount > 0) {
    return {
      headline: `Заблокировано: ${input.blockingCount} ${pluralizeQuestion(input.blockingCount)}`,
      detail: "Система не может продолжить без вашего решения.",
      action: {
        label: `Ответить на ${input.blockingCount}`,
        onClick: input.onOpenClarifications,
        tone: "primary",
      },
    };
  }
  if (input.isRunning) {
    return {
      headline: "Идёт работа",
      detail: "Можно подождать или остановить процесс.",
      action: { label: "Остановить", onClick: input.onCancelRun, tone: "danger" },
    };
  }
  const progress = input.artifactProgress;
  if (
    progress &&
    progress.artifacts_required > 0 &&
    progress.artifacts_ready >= progress.artifacts_required &&
    progress.gates_passed >= progress.gates_required
  ) {
    return {
      headline: "Цель достигнута",
      detail: "Артефакты собраны и проверены. Можно передавать команде.",
      action: { label: "Передать команде", disabled: true, tone: "primary" },
    };
  }
  return {
    headline: "Нет срочных задач",
    detail: "Система готова к следующему шагу — продолжит работу по нажатию.",
    action: { label: "Запустить следующий шаг", onClick: input.onRunNext, tone: "primary" },
  };
}

function pluralizeQuestion(n: number): string {
  const last = n % 10;
  const last2 = n % 100;
  if (last2 >= 11 && last2 <= 14) return "вопросов";
  if (last === 1) return "вопрос";
  if (last >= 2 && last <= 4) return "вопроса";
  return "вопросов";
}

interface ProgressRowProps {
  label: string;
  done: number | undefined;
  total: number | undefined;
}

function ProgressRow({ label, done, total }: ProgressRowProps) {
  const safeDone = done ?? 0;
  const safeTotal = total ?? 0;
  const pct = safeTotal > 0 ? Math.min(100, Math.round((safeDone / safeTotal) * 100)) : 0;
  return (
    <div className="status-card__row">
      <span className="status-card__row-label">{label}</span>
      <span className="status-card__row-value">
        <strong>{safeDone}</strong> / {safeTotal}
      </span>
      <span className="status-card__row-bar" aria-hidden>
        <span className="status-card__row-bar-fill" style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
}

interface SkeletonSectionItemProps {
  section: ArtifactSkeletonView["sections"][number];
  onClick: () => void;
}

function SkeletonSectionItem({ section, onClick }: SkeletonSectionItemProps) {
  return (
    <li className={`skeleton-item skeleton-item--${section.status}`}>
      <button type="button" className="skeleton-item__button" onClick={onClick}>
        <span className="skeleton-item__icon" aria-hidden>
          {statusIcon(section.status)}
        </span>
        <span className="skeleton-item__body">
          <span className="skeleton-item__title">{section.title}</span>
          {section.summary && (
            <span className="skeleton-item__summary">{section.summary}</span>
          )}
        </span>
        <span className="skeleton-item__meta">
          {section.has_pins && (
            <span
              className="skeleton-item__pins"
              title={`${section.pin_count} подозрительных мест — стоит проверить`}
            >
              ⚠ {section.pin_count}
            </span>
          )}
          <span className="skeleton-item__status">{statusLabel(section.status)}</span>
        </span>
      </button>
    </li>
  );
}

function statusIcon(status: ArtifactSectionStatus): string {
  switch (status) {
    case "done":
      return "✓";
    case "in_progress":
      return "⚙";
    case "needs_review":
      return "⚠";
    case "pending":
    default:
      return "○";
  }
}

function statusLabel(status: ArtifactSectionStatus): string {
  switch (status) {
    case "done":
      return "готов";
    case "in_progress":
      return "в работе";
    case "needs_review":
      return "проверьте";
    case "pending":
    default:
      return "ожидание";
  }
}

function humanizeArtifactRole(role: string | undefined): string {
  if (!role) return "Артефакт проекта";
  return role
    .replace(/[_-]+/g, " ")
    .split(" ")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}
