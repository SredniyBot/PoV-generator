/**
 * StageStatusBar — постоянный статус-слой над вкладками (gate stepper).
 *
 * Единый источник правды о том, «где мы в цепочке этапов» (ТЗ → Архитектура →
 * Реализация) и «что сломалось на активном этапе». Заменяет разрозненную подачу
 * (бледные чипсы шапки / закопанный прогресс в Обзоре / отдельную run-панель).
 *
 * Данные — проекция `stages` (`/api/projects/:id/stages`): степпер этапов +
 * прогресс/ошибки активного. Живая активность прогона (тикер + лента шагов)
 * приходит как `children` (RunActivitySection в App.tsx), чтобы не дублировать
 * счётчики — ошибки/блокировки несёт сам степпер.
 */

import type { ReactNode } from "react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { AlertTriangle, ArrowRight, CheckCircle2, Clock, Network, RotateCcw } from "lucide-react";

import { api } from "./api";
import type { StageView } from "./types";
import { cx } from "./ui";

function pluralRu(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function StageIcon({ stage }: { stage: StageView }) {
  if (stage.state === "done") return <CheckCircle2 size={14} className="stage-seg__check" />;
  if (stage.state === "active") return <span className="stage-seg__dot stage-seg__dot--filled">●</span>;
  return <span className="stage-seg__dot">○</span>;
}

function titleForRef(ref: string, stages: StageView[]): string {
  const match = stages.find((s) => s.objective_ref === ref);
  return match ? match.title : ref.split("@")[0] ?? ref;
}

export function StageStatusBar({
  projectId,
  onActivateNextObjective,
  activating,
  onRetryTask,
  children,
}: {
  projectId: string;
  onActivateNextObjective: (objectiveRef: string) => void;
  activating: boolean;
  onRetryTask: (taskId: string) => void;
  children?: ReactNode;
}) {
  const navigate = useNavigate();
  const [popoverOpen, setPopoverOpen] = useState(false);

  const stagesQuery = useQuery({
    // ключ совпадает с projectionKey(projectId, "stages") в App.tsx — WS-пуш
    // (projection_changed: stages) инвалидирует именно его.
    queryKey: [projectId, "stages"],
    queryFn: () => api.getStages(projectId),
  });

  const data = stagesQuery.data;
  // Бар — некритичный хром: пока нет данных, показываем только run-секцию.
  if (!data || data.stages.length === 0) {
    return children ? <div className="status-bar">{children}</div> : null;
  }

  const active = data.stages.find((s) => s.is_current) ?? null;
  const failedCount = active?.failed_count ?? 0;
  const attentionCount = (active?.blocked_count ?? 0) + (active?.awaiting_signoff ?? 0);
  const failingTasks = active?.failing_tasks ?? [];

  const goToNode = (taskId: string) => {
    setPopoverOpen(false);
    navigate(`/projects/${projectId}/task-graph?focus=${taskId}`);
  };

  return (
    <div className="status-bar">
      <div className="stage-bar">
        <ol className="stage-bar__track">
          {data.stages.map((stage, idx) => (
            <li
              key={stage.objective_ref}
              className={cx("stage-seg", `stage-seg--${stage.state}`, stage.is_current && "stage-seg--current")}
            >
              <span className="stage-seg__icon">
                <StageIcon stage={stage} />
              </span>
              <span className="stage-seg__body">
                <span className="stage-seg__title" title={stage.objective_ref}>
                  {stage.title}
                </span>
                {stage.is_current ? (
                  <span className="stage-seg__progress">
                    арт {stage.artifacts_ready}/{stage.artifacts_required}
                    {stage.gates_required > 0 ? (
                      <> · гейт {stage.gates_passed}/{stage.gates_required}</>
                    ) : null}
                  </span>
                ) : null}
              </span>
              {idx < data.stages.length - 1 ? (
                <span className="stage-seg__sep" aria-hidden>
                  ›
                </span>
              ) : null}
            </li>
          ))}
        </ol>

        <div className="stage-bar__aside">
          {failedCount > 0 || attentionCount > 0 ? (
            <div className="stage-badges">
              {failedCount > 0 ? (
                <button
                  type="button"
                  className="stage-badge stage-badge--danger"
                  onClick={() => setPopoverOpen((v) => !v)}
                  aria-expanded={popoverOpen}
                >
                  <AlertTriangle size={13} /> {failedCount}{" "}
                  {pluralRu(failedCount, "ошибка", "ошибки", "ошибок")}
                </button>
              ) : null}
              {attentionCount > 0 ? (
                <button
                  type="button"
                  className="stage-badge stage-badge--warning"
                  onClick={() => setPopoverOpen((v) => !v)}
                  aria-expanded={popoverOpen}
                >
                  <Clock size={13} /> {attentionCount} ждут
                </button>
              ) : null}

              {popoverOpen && failingTasks.length > 0 ? (
                <>
                  <button
                    type="button"
                    className="stage-popover__backdrop"
                    aria-label="Закрыть"
                    onClick={() => setPopoverOpen(false)}
                  />
                  <div className="stage-popover" role="dialog">
                    <div className="stage-popover__head">
                      <span>Проблемные шаги этапа «{active?.title}»</span>
                      <button
                        type="button"
                        className="stage-popover__close"
                        onClick={() => setPopoverOpen(false)}
                      >
                        ×
                      </button>
                    </div>
                    <ul className="stage-popover__list">
                      {failingTasks.map((t) => (
                        <li key={t.task_id} className="stage-popover__item">
                          <div className="stage-popover__item-head">
                            <span className={cx("stage-popover__dot", `stage-popover__dot--${t.status}`)} />
                            <span className="stage-popover__title">{t.title}</span>
                          </div>
                          {t.reason ? <div className="stage-popover__reason">{t.reason}</div> : null}
                          <div className="stage-popover__actions">
                            {t.retryable ? (
                              <button
                                type="button"
                                className="stage-popover__action"
                                onClick={() => {
                                  setPopoverOpen(false);
                                  onRetryTask(t.task_id);
                                }}
                              >
                                <RotateCcw size={12} /> Повторить
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="stage-popover__action"
                              onClick={() => goToNode(t.task_id)}
                            >
                              <Network size={12} /> На графе
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                </>
              ) : null}
            </div>
          ) : null}

          {data.objective_complete && data.next_objective_refs.length > 0 ? (
            <div className="stage-cta-group">
              {data.next_objective_refs.map((ref) => (
                <button
                  key={ref}
                  type="button"
                  className="stage-cta"
                  disabled={activating}
                  onClick={() => onActivateNextObjective(ref)}
                  title={`Активировать этап: ${titleForRef(ref, data.stages)}`}
                >
                  Следующий этап <ArrowRight size={14} />
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      {children}
    </div>
  );
}
