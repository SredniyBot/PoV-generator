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
import { AlertTriangle, ArrowRight, CheckCircle2, Circle, CircleDot, Clock, MessageSquare, Network, RotateCcw, X } from "lucide-react";

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

// Ф3: активный этап, у которого все артефакты готовы, но итоговый ещё не
// согласован с заказчиком. Такой этап красим жёлтым и приглашаем нажать —
// согласование закрывает этап и открывает «Следующий этап».
function awaitingSignoff(stage: StageView): boolean {
  return (
    stage.state === "active" &&
    stage.artifacts_required > 0 &&
    stage.artifacts_ready >= stage.artifacts_required &&
    !stage.signed_off
  );
}

function StageIcon({ stage }: { stage: StageView }) {
  if (stage.state === "done") return <CheckCircle2 size={14} className="stage-seg__check" />;
  if (stage.state === "active") {
    if (awaitingSignoff(stage)) {
      return <CircleDot size={14} className="stage-seg__dot stage-seg__dot--awaiting" />;
    }
    return <CircleDot size={14} className="stage-seg__dot stage-seg__dot--filled" />;
  }
  return <Circle size={14} className="stage-seg__dot" />;
}

function titleForRef(ref: string, stages: StageView[]): string {
  const match = stages.find((s) => s.objective_ref === ref);
  return match ? match.title : ref.split("@")[0] ?? ref;
}

// Короткая подпись этапа из objective_ref — дорожка должна быть лаконичной,
// а не повторять длинное процессное название цели. Полное название и прогресс
// показываем в подсказке (stageTooltip). Неизвестная цель → исходный заголовок.
export function shortStageLabel(ref: string, fallback: string): string {
  const id = (ref.split("@")[0] ?? "").toLowerCase();
  if (id.includes("requirements") || id.includes("specification")) return "ТЗ";
  if (id.includes("architecture") || id.includes("design")) return "Архитектура";
  if (
    id.includes("implementation") ||
    id.includes("build") ||
    id.includes("plan") ||
    id.includes("realiz")
  ) {
    return "Реализация";
  }
  return fallback;
}

// Подсказка при наведении: полное название цели + прогресс активного этапа.
function stageTooltip(stage: StageView): string {
  let t = stage.title;
  if (stage.is_current) {
    t += ` · артефакты ${stage.artifacts_ready}/${stage.artifacts_required}`;
    if (stage.gates_required > 0) {
      t += ` · гейт ${stage.gates_passed}/${stage.gates_required}`;
    }
  }
  return t;
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
  // Какой поповер открыт: ошибки / решения / закрыт.
  const [popover, setPopover] = useState<null | "errors" | "decisions">(null);

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
  // Ф3: активный этап с готовыми артефактами ждёт согласования заказчиком —
  // «Следующий этап» показываем, но держим недоступным с подсказкой.
  const awaitingActive = active ? awaitingSignoff(active) : false;
  // Ошибки — упавшие задачи (чинятся повтором), в своём поповере.
  const failedTasks = (active?.failing_tasks ?? []).filter((t) => t.status === "failed");
  const failedCount = failedTasks.length;
  // «Ждут» — открытые решения; счётчик и список из одного источника.
  const pendingDecisions = active?.pending_decisions ?? [];
  const attentionCount = pendingDecisions.length;

  const goToNode = (taskId: string) => {
    setPopover(null);
    navigate(`/projects/${projectId}/task-graph?focus=${taskId}`);
  };
  const goToDecisions = () => {
    setPopover(null);
    navigate(`/projects/${projectId}/decisions/pending`);
  };

  return (
    <div className="status-bar">
      <div className="stage-bar">
        <ol className="stage-bar__track">
          {data.stages.map((stage, idx) => {
            // Завершённый/готовый этап с ключевым артефактом кликабелен —
            // открывает дилеверабл этапа (ТЗ/Архитектура/...).
            const openable = Boolean(stage.key_artifact_id);
            const awaiting = awaitingSignoff(stage);
            const label = shortStageLabel(stage.objective_ref, stage.title);
            return (
              <li
                key={stage.objective_ref}
                className={cx(
                  "stage-seg",
                  `stage-seg--${stage.state}`,
                  stage.is_current && "stage-seg--current",
                  openable && "stage-seg--openable",
                  awaiting && "stage-seg--awaiting-signoff",
                )}
              >
                {openable ? (
                  <button
                    type="button"
                    className="stage-seg__open"
                    title={
                      awaiting
                        ? `Согласуйте итоговый артефакт, чтобы завершить этап: ${label}`
                        : `Открыть результат этапа: ${label}`
                    }
                    onClick={() =>
                      navigate(`/projects/${projectId}/artifacts/${stage.key_artifact_id}`)
                    }
                  >
                    <span className="stage-seg__icon">
                      <StageIcon stage={stage} />
                    </span>
                    <span className="stage-seg__title" title={stageTooltip(stage)}>
                      {label}
                    </span>
                  </button>
                ) : (
                  <>
                    <span className="stage-seg__icon">
                      <StageIcon stage={stage} />
                    </span>
                    <span className="stage-seg__title" title={stageTooltip(stage)}>
                      {label}
                    </span>
                  </>
                )}
                {idx < data.stages.length - 1 ? (
                  <span className="stage-seg__sep" aria-hidden>
                    ›
                  </span>
                ) : null}
              </li>
            );
          })}
        </ol>

        <div className="stage-bar__aside">
          {failedCount > 0 || attentionCount > 0 ? (
            <div className="stage-badges">
              {failedCount > 0 ? (
                <button
                  type="button"
                  className="stage-badge stage-badge--danger"
                  onClick={() => setPopover((v) => (v === "errors" ? null : "errors"))}
                  aria-expanded={popover === "errors"}
                >
                  <AlertTriangle size={13} /> {failedCount}{" "}
                  {pluralRu(failedCount, "ошибка", "ошибки", "ошибок")}
                </button>
              ) : null}
              {attentionCount > 0 ? (
                <button
                  type="button"
                  className="stage-badge stage-badge--warning"
                  onClick={() => setPopover((v) => (v === "decisions" ? null : "decisions"))}
                  aria-expanded={popover === "decisions"}
                >
                  <Clock size={13} /> {attentionCount} ждут
                </button>
              ) : null}

              {popover ? (
                <button
                  type="button"
                  className="stage-popover__backdrop"
                  aria-label="Закрыть"
                  onClick={() => setPopover(null)}
                />
              ) : null}

              {popover === "errors" && failedTasks.length > 0 ? (
                <div className="stage-popover" role="dialog">
                  <div className="stage-popover__head">
                    <span>Шаги с ошибкой · этап «{active?.title}»</span>
                    <button type="button" className="stage-popover__close" aria-label="Закрыть" onClick={() => setPopover(null)}>
                      <X size={16} />
                    </button>
                  </div>
                  <ul className="stage-popover__list">
                    {failedTasks.map((t) => (
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
                                setPopover(null);
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
              ) : null}

              {popover === "decisions" && pendingDecisions.length > 0 ? (
                <div className="stage-popover" role="dialog">
                  <div className="stage-popover__head">
                    <span>Решения ждут ответа · этап «{active?.title}»</span>
                    <button type="button" className="stage-popover__close" aria-label="Закрыть" onClick={() => setPopover(null)}>
                      <X size={16} />
                    </button>
                  </div>
                  <ul className="stage-popover__list">
                    {pendingDecisions.map((d) => (
                      <li key={d.decision_id} className="stage-popover__item">
                        <div className="stage-popover__item-head">
                          <span className="stage-popover__dot stage-popover__dot--blocked" />
                          <span className="stage-popover__title">{d.title}</span>
                        </div>
                        <div className="stage-popover__actions">
                          <button type="button" className="stage-popover__action" onClick={goToDecisions}>
                            <MessageSquare size={12} /> Ответить
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}

          {/* Кнопку показываем, когда артефакты этапа готовы (цель завершена ИЛИ
              ждёт согласования) — но пока итоговый артефакт не согласован, она
              недоступна с поясняющей подсказкой (Ф3). */}
          {(data.objective_complete || awaitingActive) && data.next_objective_refs.length > 0 ? (
            <div className="stage-cta-group">
              {data.next_objective_refs.map((ref) => (
                // Ф4: переход больше НЕ держится огульно реквизитами. Честный
                // гейтинг гранулярный — непредоставленный реквизит держит в графе
                // только свою задачу-потребителя. Кнопку гасит лишь согласование.
                <button
                  key={ref}
                  type="button"
                  className="stage-cta"
                  disabled={activating || awaitingActive}
                  onClick={() => onActivateNextObjective(ref)}
                  title={
                    awaitingActive
                      ? "Сначала согласуйте итоговый артефакт этапа"
                      : `Активировать этап: ${titleForRef(ref, data.stages)}`
                  }
                >
                  Следующий этап <ArrowRight size={14} />
                </button>
              ))}
              {(data.blocked_by_requisites?.length ?? 0) > 0 ? (
                <p className="stage-cta-pending">
                  Часть задач реализации будет ждать данные:{" "}
                  {(data.blocked_by_requisites ?? []).join("; ")}. Можно перейти и заполнить по
                  ходу во вкладке «Реквизиты» — ждут только эти задачи, остальные идут.
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
      {children}
    </div>
  );
}
