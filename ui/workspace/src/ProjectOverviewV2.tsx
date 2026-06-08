/**
 * Вкладка «Проект» — командный центр.
 *
 * Левая колонка: блок workflow (дорожка этапов + живой прогон и лента шагов),
 *   приходит как `workflowSlot` из App (там живут commandMutations).
 * Правая колонка: «Что сейчас нужно» (единственное действие) → «Режим участия»
 *   → «Подробнее о проекте» (домены + ссылка на входные артефакты).
 *
 * Скелет артефакта переехал на вкладку «Артефакты»; здесь его больше нет.
 */
import type { ReactNode } from "react";
import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";

import { api } from "./api";
import { activeRunRefetchInterval } from "./realtime";
import { ModeControl } from "./ui";
import type { ObjectiveProgressView, WorkflowRunView } from "./types";

interface ProjectOverviewV2Props {
  projectId: string;
  /** Блок workflow (StageStatusBar + RunActivitySection) — рендерится в App. */
  workflowSlot?: ReactNode;
  /**
   * v3.1: единая точка перехода в Decision-реестр.
   */
  onOpenDecisions?: () => void;
  onOpenArtifactFull?: (artifactId: string) => void;
  /**
   * Команда "продолжить движение проекта до естественной остановки"
   * (= run-until-blocked).
   */
  onContinue?: () => void;
  /** Переделать конкретный шаг (= retry-task). */
  onRetryTask?: (taskId: string) => void;
  // Режим участия — переехал из шапки сюда, под «Что сейчас нужно».
  clarificationMode?: string;
  onClarificationModeChange?: (mode: string) => void;
  modePending?: boolean;
  // «Подробнее о проекте».
  domainPacks?: string[];
  onOpenInputArtifacts?: () => void;
}

export function ProjectOverviewV2({
  projectId,
  workflowSlot,
  onOpenDecisions,
  onContinue,
  onRetryTask,
  clarificationMode,
  onClarificationModeChange,
  modePending,
  domainPacks,
  onOpenInputArtifacts,
}: ProjectOverviewV2Props) {
  const queryClient = useQueryClient();

  // Точный isRunning + cancel через активный workflow run.
  const activeRunQuery = useQuery<WorkflowRunView | null>({
    queryKey: [projectId, "workflow-run-active"],
    queryFn: () => api.getActiveWorkflowRun(projectId),
    refetchInterval: activeRunRefetchInterval,
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

  // Для "Переделать последний шаг" нужен task_id последнего содержательного шага.
  const recentRunsQuery = useQuery({
    queryKey: [projectId, "workflow-runs", "recent-1"],
    queryFn: () => api.listWorkflowRuns(projectId, 1),
    enabled: !activeRun,
  });
  const lastStep = useMemo(() => {
    const sourceSteps = activeRun?.steps ?? recentRunsQuery.data?.[0]?.steps ?? [];
    if (sourceSteps.length === 0) return null;
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

  // pending-checkpoint сессии — основной источник «нужно ваше решение».
  const checkpoints = useQuery({
    queryKey: ["checkpoints-list", projectId],
    queryFn: () => api.getCheckpoints(projectId),
    refetchInterval: isRunning ? 5000 : false,
  });

  // Реестр решений — для счётчика «ждут ответа».
  const decisions = useQuery({
    queryKey: ["decisions", projectId],
    queryFn: () => api.getDecisionsRegistry(projectId),
  });

  const primaryArtifactId = overview.data?.key_artifacts?.[0]?.artifact_id;

  const pendingDecisionsCount = (checkpoints.data?.items ?? [])
    .filter((s) => s.status === "pending")
    .reduce((sum, s) => sum + s.decisions.length, 0);
  const surfacedPendingCount = decisions.data?.surfaced_pending ?? 0;
  const blockingCount = pendingDecisionsCount > 0 ? pendingDecisionsCount : surfacedPendingCount;

  const lastStepLabel = lastStep?.task_key ?? lastStep?.selected_step_id ?? null;
  const primaryCta = useMemo(
    () =>
      computePrimaryCta({
        blockingCount,
        isRunning,
        hasAnyArtifact: Boolean(primaryArtifactId),
        artifactProgress: overview.data?.objective_progress,
        runError: activeRun?.error_message,
        lastTaskId: lastStep?.task_id ?? null,
        lastTaskLabel: lastStepLabel,
        onOpenDecisions,
        onPause: activeRun ? () => pauseMutation.mutate(activeRun.run_id) : undefined,
        onContinue,
        onRetryTask,
        pausing: pauseMutation.isPending || Boolean(activeRun?.cancel_requested),
      }),
    [
      blockingCount,
      isRunning,
      primaryArtifactId,
      overview.data?.objective_progress,
      activeRun,
      lastStep?.task_id,
      lastStepLabel,
      onOpenDecisions,
      onContinue,
      onRetryTask,
      pauseMutation,
    ],
  );

  const domains = domainPacks ?? [];

  return (
    <section className="overview-mc">
      <div className="overview-mc__layout">
        <div className="overview-mc__main">{workflowSlot}</div>

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

          {/* Режим участия — перенесён из шапки сюда, под «Что сейчас нужно». */}
          <ModeControl
            value={clarificationMode}
            onChange={onClarificationModeChange}
            pending={modePending}
          />

          {/* Подробнее о проекте: домены + переход к входным артефактам. */}
          <div className="overview-about">
            <div className="overview-about__title">Подробнее о проекте</div>
            <div className="overview-about__block">
              <span className="overview-about__label">Домены</span>
              {domains.length > 0 ? (
                <div className="overview-about__chips">
                  {domains.map((d) => (
                    <span key={d} className="overview-about__chip">
                      {humanizePackRef(d)}
                    </span>
                  ))}
                </div>
              ) : (
                <span className="overview-about__value">—</span>
              )}
            </div>
            {onOpenInputArtifacts ? (
              <button
                type="button"
                className="overview-about__link"
                onClick={onOpenInputArtifacts}
              >
                Входные материалы <ArrowRight size={14} />
              </button>
            ) : null}
          </div>
        </aside>
      </div>
    </section>
  );
}

// ---- helpers ---------------------------------------------------------------

// Доменный пакет из ref в человекочитаемое: "domain.fintech@1.0.0" → "fintech".
function humanizePackRef(ref: string): string {
  const id = ref.split("@")[0] ?? ref;
  const last = id.split(".").pop() ?? id;
  return last.replace(/_/g, " ");
}

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
 * computePrimaryCta — единственное действие на главном экране проекта.
 *
 * Никаких runtime-терминов; одно действие на состояние по JTBD менеджера:
 *   блокеры → "Ответить (N)"; идёт работа → "Приостановить"; ошибка →
 *   "Повторить"; цель достигнута → "Готово"; пусто → "Поехали";
 *   иначе → "Продолжить" (= run-until-blocked).
 */
function computePrimaryCta(input: {
  blockingCount: number;
  isRunning: boolean;
  hasAnyArtifact: boolean;
  artifactProgress?: ObjectiveProgressView;
  runError: string | null | undefined;
  lastTaskId: string | null;
  lastTaskLabel: string | null;
  onOpenDecisions?: () => void;
  onPause?: () => void;
  onContinue?: () => void;
  onRetryTask?: (taskId: string) => void;
  pausing: boolean;
}): CtaInfo {
  const retryAction: SecondaryAction | null =
    input.lastTaskId && input.onRetryTask
      ? {
          label: "Переделать последний шаг",
          hint: input.lastTaskLabel ? humanizeStepLabel(input.lastTaskLabel) : undefined,
          onClick: () => input.onRetryTask?.(input.lastTaskId!),
        }
      : null;

  // 1. Блокирующие вопросы — самое срочное. Никаких secondary — фокус: ответить.
  if (input.blockingCount > 0) {
    return {
      headline: `Ждут вашего решения: ${input.blockingCount} ${pluralizeQuestion(input.blockingCount)}`,
      detail: "Без ответа система не может двигаться дальше.",
      action: {
        label: `Ответить на ${input.blockingCount}`,
        onClick: input.onOpenDecisions,
        tone: "primary",
      },
    };
  }
  // 2. Идёт работа — дать паузу.
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
  // 3. Ошибка предыдущего прогона.
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
  // 4. Цель достигнута.
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
      secondary: retryAction ?? undefined,
    };
  }
  // 5. Артефакт ещё пустой — старт.
  if (!input.hasAnyArtifact) {
    return {
      headline: "Готов к запуску",
      detail: "Система разберёт ваш материал и начнёт собирать результат.",
      action: { label: "Поехали", onClick: input.onContinue, tone: "primary" },
    };
  }
  // 6. Частично готов, никто не ждёт — продолжаем.
  return {
    headline: "Можно двигаться дальше",
    detail: "Система продолжит работу и остановится, когда понадобится ваше решение.",
    action: { label: "Продолжить", onClick: input.onContinue, tone: "primary" },
    secondary: retryAction ?? undefined,
  };
}

function humanizeStepLabel(label: string): string {
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
