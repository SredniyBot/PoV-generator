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
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { api } from "./api";
import type {
  ArtifactSectionStatus,
  ArtifactSkeletonView,
  FailurePinView,
  ObjectiveProgressView,
  WorkflowRunView,
} from "./types";

interface ProjectOverviewV2Props {
  projectId: string;
  onOpenClarifications?: () => void;
  onOpenDecisionLog?: () => void;
  onOpenArtifactFull?: (artifactId: string) => void;
  /**
   * Команда "продолжить движение проекта до естественной остановки"
   * (= run-until-blocked). НЕ "следующий шаг" — менеджеру нет разницы
   * между шагами, а engagement-режим определяет когда система спросит.
   */
  onContinue?: () => void;
  /**
   * Переделать конкретный шаг (= retry-task). Используется когда менеджер
   * посмотрел результат последнего шага и хочет переделать его, не
   * откатывая весь проект.
   */
  onRetryTask?: (taskId: string) => void;
}

export function ProjectOverviewV2({
  projectId,
  onOpenClarifications,
  onOpenDecisionLog,
  onOpenArtifactFull,
  onContinue,
  onRetryTask,
}: ProjectOverviewV2Props) {
  const queryClient = useQueryClient();

  // L6-4: inline drawer для раздела артефакта с P5 failure pins.
  const [openSection, setOpenSection] = useState<
    { artifactId: string; sectionId: string } | null
  >(null);
  useEffect(() => {
    if (!openSection) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenSection(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openSection]);

  // L6-9: точный isRunning + cancel через активный workflow run.
  // Не используем commandMutations.busy потому что он true для любых
  // команд (set-goal, accept-assumption и т.д.), что даёт ложно-
  // положительный isRunning.
  const activeRunQuery = useQuery<WorkflowRunView | null>({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => api.getActiveWorkflowRun(projectId),
    refetchInterval: 1500,
  });
  const activeRun = activeRunQuery.data ?? null;
  const isRunning =
    activeRun !== null && (activeRun.status === "running" || activeRun.status === "pending");

  const pauseMutation = useMutation({
    mutationFn: (runId: string) => api.cancelWorkflow(projectId, runId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
    },
  });

  // L6-10: для "Переделать последний шаг" нужен task_id последнего
  // выполненного / выполняющегося шага. Берём из активного run или, если
  // active нет — из последнего недавнего run.
  const recentRunsQuery = useQuery({
    queryKey: [projectId, "workflow-runs", "recent-1"],
    queryFn: () => api.listWorkflowRuns(projectId, 1),
    enabled: !activeRun,
  });
  const lastStep = useMemo(() => {
    const sourceSteps =
      activeRun?.steps ?? recentRunsQuery.data?.[0]?.steps ?? [];
    if (sourceSteps.length === 0) return null;
    // Берём последний step с task_id — это последняя содержательная задача.
    for (let i = sourceSteps.length - 1; i >= 0; i--) {
      const step = sourceSteps[i];
      if (step && step.task_id) return step;
    }
    return null;
  }, [activeRun?.steps, recentRunsQuery.data]);

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

  const versionsQuery = useQuery({
    queryKey: ["artifact-versions", projectId],
    queryFn: () => api.getArtifactVersions(projectId),
  });

  // Цепочка версий, в которой живёт primaryArtifactId.
  const versionChain = useMemo(() => {
    if (!versionsQuery.data || !primaryArtifactId) return null;
    for (const chain of versionsQuery.data.chains) {
      if (chain.some((v) => v.artifact_id === primaryArtifactId)) return chain;
    }
    return null;
  }, [versionsQuery.data, primaryArtifactId]);

  const blockingCount = clarifications.data?.blocking_count ?? 0;
  const openCount = clarifications.data?.open_count ?? 0;
  const assumedCount = decisionLog.data?.assumed_count ?? 0;
  const decisionsTotal = decisionLog.data?.total_count ?? 0;

  const lastStepLabel = lastStep?.task_key ?? lastStep?.selected_step_id ?? null;
  const primaryCta = useMemo(
    () =>
      computePrimaryCta({
        blockingCount,
        isRunning,
        hasAnyArtifact: Boolean(primaryArtifactId),
        sectionsTotal: skeleton.data?.sections_total,
        sectionsDone: skeleton.data?.sections_done,
        artifactProgress: overview.data?.objective_progress,
        runError: activeRun?.error_message,
        lastTaskId: lastStep?.task_id ?? null,
        lastTaskLabel: lastStepLabel,
        onOpenClarifications,
        onPause: activeRun ? () => pauseMutation.mutate(activeRun.run_id) : undefined,
        onContinue,
        onRetryTask,
        pausing: pauseMutation.isPending || Boolean(activeRun?.cancel_requested),
      }),
    [
      blockingCount,
      isRunning,
      primaryArtifactId,
      skeleton.data?.sections_total,
      skeleton.data?.sections_done,
      overview.data?.objective_progress,
      activeRun,
      lastStep?.task_id,
      lastStepLabel,
      onOpenClarifications,
      onContinue,
      onRetryTask,
      pauseMutation,
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
            <div className="overview-mc__artifact-meta">
              {versionChain && versionChain.length > 1 && (
                <VersionDropdown
                  chain={versionChain}
                  currentId={primaryArtifactId!}
                  onOpenVersion={(artifactId) => onOpenArtifactFull?.(artifactId)}
                />
              )}
              {skeleton.data && (
                <span className="overview-mc__artifact-progress">
                  <strong>
                    {skeleton.data.sections_done}/{skeleton.data.sections_total}
                  </strong>
                  <span>разделов</span>
                </span>
              )}
            </div>
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
                    setOpenSection({
                      artifactId: primaryArtifactId,
                      sectionId: section.section_id,
                    })
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
            {primaryCta.secondary && (
              <button
                type="button"
                className="status-card__secondary"
                onClick={primaryCta.secondary.onClick}
                title={primaryCta.secondary.hint}
              >
                ↻ {primaryCta.secondary.label}
                {primaryCta.secondary.hint && (
                  <span className="status-card__secondary-hint">
                    · {primaryCta.secondary.hint}
                  </span>
                )}
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

      {openSection && (
        <SectionDrawer
          projectId={projectId}
          artifactId={openSection.artifactId}
          sectionId={openSection.sectionId}
          onClose={() => setOpenSection(null)}
          onOpenFull={() => {
            onOpenArtifactFull?.(openSection.artifactId);
            setOpenSection(null);
          }}
        />
      )}
    </section>
  );
}

// ---- Section drawer (L6-4) -------------------------------------------------

interface SectionDrawerProps {
  projectId: string;
  artifactId: string;
  sectionId: string;
  onClose: () => void;
  onOpenFull: () => void;
}

function SectionDrawer({
  projectId,
  artifactId,
  sectionId,
  onClose,
  onOpenFull,
}: SectionDrawerProps) {
  const artifact = useQuery({
    queryKey: ["artifact-detail-drawer", projectId, artifactId],
    queryFn: () => api.getArtifactDetail(projectId, artifactId),
  });
  const pinsQuery = useQuery({
    queryKey: ["pins-drawer", projectId, artifactId],
    queryFn: () => api.getFailurePins(projectId, artifactId),
  });

  const sectionData = useMemo(() => {
    if (!artifact.data?.json_content) return null;
    try {
      const parsed = JSON.parse(artifact.data.json_content) as unknown;
      return findSection(parsed, sectionId);
    } catch {
      return null;
    }
  }, [artifact.data?.json_content, sectionId]);

  const sectionPins = useMemo<FailurePinView[]>(() => {
    if (!pinsQuery.data) return [];
    return pinsQuery.data.pins.filter(
      (pin) => !pin.section_id || pin.section_id === sectionId,
    );
  }, [pinsQuery.data, sectionId]);

  return (
    <div
      className="section-drawer-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        className="section-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="section-drawer-title"
      >
        <header className="section-drawer__header">
          <div>
            <p className="section-drawer__eyebrow">Раздел артефакта</p>
            <h2 id="section-drawer-title" className="section-drawer__title">
              {humanize(sectionId)}
            </h2>
          </div>
          <button
            type="button"
            className="section-drawer__close"
            onClick={onClose}
            aria-label="Закрыть"
          >
            ✕
          </button>
        </header>

        {sectionPins.length > 0 && (
          <section className="section-drawer__pins" aria-label="Подозрительные места">
            <h3 className="section-drawer__pins-title">
              ⚠ Стоит проверить ({sectionPins.length})
            </h3>
            <ul className="section-drawer__pins-list" role="list">
              {sectionPins.map((pin) => (
                <li
                  key={pin.pin_id}
                  className={`section-drawer__pin section-drawer__pin--${pin.severity}`}
                >
                  <span className="section-drawer__pin-kind">{pinKindLabel(pin.kind)}</span>
                  <span className="section-drawer__pin-message">{pin.message}</span>
                  {pin.confidence_without_user !== null && (
                    <span className="section-drawer__pin-confidence">
                      уверенность: {Math.round(pin.confidence_without_user * 100)}%
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className="section-drawer__body" aria-label="Содержимое раздела">
          <h3 className="section-drawer__body-title">Содержимое</h3>
          {artifact.isLoading ? (
            <p className="section-drawer__hint">Загрузка…</p>
          ) : sectionData !== null ? (
            <SectionContentRenderer value={sectionData} />
          ) : (
            <p className="section-drawer__hint">
              Раздел ещё не сгенерирован. Когда система до него дойдёт — здесь
              появится текст.
            </p>
          )}
        </section>

        <footer className="section-drawer__footer">
          <button
            type="button"
            className="section-drawer__action"
            onClick={onOpenFull}
          >
            Открыть полностью →
          </button>
        </footer>
      </aside>
    </div>
  );
}

function SectionContentRenderer({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <p className="section-drawer__hint">Раздел пустой.</p>;
  }
  if (typeof value === "string") {
    return <p className="section-drawer__text">{value}</p>;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return <p className="section-drawer__text">{String(value)}</p>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <p className="section-drawer__hint">Список пустой.</p>;
    return (
      <ul className="section-drawer__list">
        {value.map((item, idx) => (
          <li key={idx}>
            {typeof item === "string" ? (
              item
            ) : (
              <pre className="section-drawer__json">{JSON.stringify(item, null, 2)}</pre>
            )}
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return <pre className="section-drawer__json">{JSON.stringify(value, null, 2)}</pre>;
  }
  return <p className="section-drawer__text">{String(value)}</p>;
}

function findSection(data: unknown, sectionId: string): unknown | null {
  if (data === null || data === undefined) return null;
  if (typeof data === "object" && !Array.isArray(data)) {
    const obj = data as Record<string, unknown>;
    // 1. explicit sections array
    if (Array.isArray(obj.sections)) {
      const found = obj.sections.find((s) => {
        if (typeof s !== "object" || s === null) return false;
        const sec = s as Record<string, unknown>;
        return String(sec.id) === sectionId;
      });
      if (found && typeof found === "object") {
        return (found as Record<string, unknown>).content ?? found;
      }
    }
    // 2. top-level key
    if (sectionId in obj) {
      return obj[sectionId];
    }
  }
  // 3. list index "item_N"
  if (Array.isArray(data) && sectionId.startsWith("item_")) {
    const idx = Number.parseInt(sectionId.slice("item_".length), 10);
    if (!Number.isNaN(idx) && idx >= 1 && idx <= data.length) {
      return data[idx - 1];
    }
  }
  return null;
}

function humanize(key: string): string {
  return key
    .replace(/[_-]+/g, " ")
    .split(" ")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function pinKindLabel(kind: FailurePinView["kind"]): string {
  switch (kind) {
    case "candidate_open":
      return "Открытый вопрос";
    case "assumption":
      return "Допущение";
    case "validation_finding":
      return "Замечание валидации";
    default:
      return kind;
  }
}

// ---- helpers ---------------------------------------------------------------

interface CtaAction {
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  tone?: "primary" | "danger";
}

interface SecondaryAction {
  label: string;
  hint?: string;
  onClick?: () => void;
}

interface CtaInfo {
  headline: string;
  detail?: string;
  action?: CtaAction;
  secondary?: SecondaryAction;
}

/**
 * L6-9: computePrimaryCta — единственное действие на главном экране проекта.
 *
 * Правила:
 * - Никаких runtime-терминов ("шаг", "блокировка", "workflow").
 * - Одно действие на состояние, основанное на JTBD менеджера:
 *   - блокеры → "Ответить (N)" — снять блок (M-J3)
 *   - идёт работа → "Приостановить" — взять контроль обратно
 *   - есть ошибка → "Повторить попытку"
 *   - цель достигнута → "Принять и закрыть" — передать (M-J1, P9 пока disabled)
 *   - артефакта ещё нет → "Поехали" — стартовать работу
 *   - иначе → "Продолжить" (= run-until-blocked)
 *
 * Команда execute "продолжить" → onContinue (run-until-blocked).
 * Сколько раз система остановится — определяется engagement-режимом
 * в шапке проекта, не пользователь вручную выбирает "по шагу" vs "до конца".
 */
function computePrimaryCta(input: {
  blockingCount: number;
  isRunning: boolean;
  hasAnyArtifact: boolean;
  sectionsTotal: number | undefined;
  sectionsDone: number | undefined;
  artifactProgress?: ObjectiveProgressView;
  runError: string | null | undefined;
  lastTaskId: string | null;
  lastTaskLabel: string | null;
  onOpenClarifications?: () => void;
  onPause?: () => void;
  onContinue?: () => void;
  onRetryTask?: (taskId: string) => void;
  pausing: boolean;
}): CtaInfo {
  // L6-10: secondary "Переделать последний шаг" доступно когда есть
  // идентифицируемый последний шаг и retry-обработчик. Появляется в
  // состояниях, где это уместно (idle с историей, error). Не появляется
  // в blockers (фокус на ответе) и empty (нечего переделывать).
  const retryAction: SecondaryAction | null =
    input.lastTaskId && input.onRetryTask
      ? {
          label: "Переделать последний шаг",
          hint: input.lastTaskLabel ? humanizeStepLabel(input.lastTaskLabel) : undefined,
          onClick: () => input.onRetryTask?.(input.lastTaskId!),
        }
      : null;

  // 1. Блокирующие вопросы — самое срочное (M-J3). Никаких secondary —
  // фокус: ответить.
  if (input.blockingCount > 0) {
    return {
      headline: `Ждут вашего решения: ${input.blockingCount} ${pluralizeQuestion(input.blockingCount)}`,
      detail: "Без ответа система не может двигаться дальше.",
      action: {
        label: `Ответить на ${input.blockingCount}`,
        onClick: input.onOpenClarifications,
        tone: "primary",
      },
    };
  }
  // 2. Идёт работа — дать паузу. Retry появится после остановки.
  if (input.isRunning) {
    return {
      headline: "Идёт работа",
      detail: "Система движется к результату. Можно подождать или взять паузу.",
      action: {
        label: input.pausing ? "Останавливаем…" : "Приостановить",
        onClick: input.onPause,
        tone: "danger",
        disabled: input.pausing || !input.onPause,
      },
    };
  }
  // 3. Ошибка предыдущего прогона — нужно решение перезапустить.
  if (input.runError) {
    return {
      headline: "Что-то пошло не так",
      detail: input.runError,
      action: {
        label: "Повторить попытку",
        onClick: input.onContinue,
        tone: "primary",
      },
      secondary: retryAction ?? undefined,
    };
  }
  // 4. Цель достигнута — передать.
  const progress = input.artifactProgress;
  if (
    progress &&
    progress.artifacts_required > 0 &&
    progress.artifacts_ready >= progress.artifacts_required &&
    progress.gates_passed >= progress.gates_required
  ) {
    return {
      headline: "Готово",
      detail: "Артефакты собраны и проверены. Можно передавать команде.",
      // P9 (формальная кнопка передачи) пока disabled — следующая итерация.
      action: { label: "Принять и закрыть", disabled: true, tone: "primary" },
      secondary: retryAction ?? undefined,
    };
  }
  // 5. Артефакт ещё пустой — старт. Нечего переделывать.
  if (!input.hasAnyArtifact) {
    return {
      headline: "Готов к запуску",
      detail: "Система разберёт ваш материал и начнёт собирать результат.",
      action: { label: "Поехали", onClick: input.onContinue, tone: "primary" },
    };
  }
  // 6. Артефакт частично готов, никто не ждёт — продолжаем + опц переделать.
  const partialProgress =
    typeof input.sectionsDone === "number" && typeof input.sectionsTotal === "number"
      ? `${input.sectionsDone} из ${input.sectionsTotal} разделов`
      : null;
  return {
    headline: "Можно двигаться дальше",
    detail: partialProgress
      ? `Готово ${partialProgress}. Система продолжит и остановится, если что-то понадобится от вас.`
      : "Система продолжит работу и остановится, когда понадобится ваше решение.",
    action: { label: "Продолжить", onClick: input.onContinue, tone: "primary" },
    secondary: retryAction ?? undefined,
  };
}

function humanizeStepLabel(label: string): string {
  // task_key вида "common.requirements_spec_generation@1.0.0" → "requirements spec generation"
  const beforeVersion = label.split("@")[0] ?? label;
  const parts = beforeVersion.split(".");
  const core = parts.length > 0 ? parts[parts.length - 1] : label;
  return (core ?? label).replace(/_/g, " ");
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

// ---- Version dropdown (L6-6 P8) -------------------------------------------

interface VersionDropdownProps {
  chain: Array<{
    artifact_id: string;
    label: string;
    is_current: boolean;
    created_at: string;
  }>;
  currentId: string;
  onOpenVersion: (artifactId: string) => void;
}

function VersionDropdown({ chain, currentId, onOpenVersion }: VersionDropdownProps) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (!target.closest(".version-dropdown")) setOpen(false);
    };
    document.addEventListener("click", handler);
    return () => document.removeEventListener("click", handler);
  }, [open]);

  const currentVersion = chain.find((v) => v.artifact_id === currentId);
  const label = currentVersion?.label ?? "версия";

  return (
    <div className={`version-dropdown${open ? " version-dropdown--open" : ""}`}>
      <button
        type="button"
        className="version-dropdown__trigger"
        onClick={(event) => {
          event.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span>{label}</span>
        <span className="version-dropdown__caret" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <ul className="version-dropdown__menu" role="listbox">
          {[...chain].reverse().map((version) => {
            const isCurrent = version.artifact_id === currentId;
            return (
              <li key={version.artifact_id} role="option" aria-selected={isCurrent}>
                <button
                  type="button"
                  className={`version-dropdown__item${isCurrent ? " version-dropdown__item--current" : ""}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setOpen(false);
                    if (!isCurrent) onOpenVersion(version.artifact_id);
                  }}
                >
                  <span className="version-dropdown__item-label">{version.label}</span>
                  {isCurrent && (
                    <span className="version-dropdown__item-current">текущая</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
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
