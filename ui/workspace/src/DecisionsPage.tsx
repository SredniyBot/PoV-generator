/**
 * v3.0 — Реестр решений и страница checkpoint-сессии.
 *
 * Два компонента в одном файле, потому что они разделяют общий
 * `DecisionCard` и стилистически живут в одном «семействе».
 *
 * - DecisionsRegistryPage — полный реестр решений проекта с фильтрами
 *   по уровню, статусу, источнику. Карточки в read-only режиме
 *   (override через UI — Phase 5).
 * - CheckpointSessionPage — interactive прохождение решений в pending
 *   сессии: по каждой карточке кнопки accept/select/free/defer, в
 *   конце submit-bar.
 *
 * Стилистика согласована с styles.css (`.decision-card`, `.checkpoint-*`).
 */

import { useState, useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, AlertTriangle, Lock, Hourglass, ChevronDown, ChevronUp, ArrowLeft } from "lucide-react";

import { api } from "./api";
import {
  Button,
  EmptyState,
  LoadingPanel,
  SectionCard,
  StatusPill,
  cx,
} from "./ui";
import type {
  CheckpointAnswerKind,
  CheckpointAnswerPayload,
  CheckpointSessionView,
  DecisionItemView,
  DecisionLevel,
  DecisionStatus,
  ProjectDecisionsView,
} from "./types";

// ---------------------------------------------------------------------------
// Лейблы и тоны (синхронно с backend enum'ами)
// ---------------------------------------------------------------------------

const LEVEL_LABEL: Record<DecisionLevel, string> = {
  business: "Бизнес",
  architecture: "Архитектура",
  detail: "Детали",
};

const LEVEL_TONE: Record<DecisionLevel, "danger" | "warning" | "muted"> = {
  business: "danger",
  architecture: "warning",
  detail: "muted",
};

const STATUS_LABEL: Record<DecisionStatus, string> = {
  proposed: "Ожидает ответа",
  accepted_default: "Принят дефолт",
  user_overridden: "Изменено вами",
  deferred: "Отложено",
  locked_in: "Зафиксировано",
  superseded: "Устарело",
};

const STATUS_TONE: Record<DecisionStatus, "active" | "success" | "warning" | "muted"> = {
  proposed: "active",
  accepted_default: "success",
  user_overridden: "success",
  deferred: "warning",
  locked_in: "muted",
  superseded: "muted",
};

// ---------------------------------------------------------------------------
// DecisionCard — переиспользуемая карточка решения
// ---------------------------------------------------------------------------

interface DecisionCardProps {
  decision: DecisionItemView;
  /** Если задано — карточка в интерактивном режиме (для checkpoint). */
  interactive?: {
    currentAnswer: CheckpointAnswerPayload | null;
    onAnswerChange: (answer: CheckpointAnswerPayload | null) => void;
  };
  /** В реестре — компактный режим, в чекпоинте — развёрнутый. */
  defaultExpanded?: boolean;
}

function DecisionCard({ decision, interactive, defaultExpanded = false }: DecisionCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [freeTextDraft, setFreeTextDraft] = useState<string>("");

  const chosen = decision.alternatives.find((alt) => alt.is_chosen);
  const currentAnswerKind = interactive?.currentAnswer?.kind ?? null;
  const selectedAlternativeId =
    currentAnswerKind === "select_alternative"
      ? interactive?.currentAnswer?.selected_option_id ?? null
      : null;

  const handleAnswer = (answer: CheckpointAnswerPayload | null) => {
    interactive?.onAnswerChange(answer);
  };

  // В интерактивном режиме — карточка всегда видна развёрнутой,
  // в реестре — переключаемая.
  const showBody = expanded || interactive !== undefined;

  return (
    <div
      className={cx(
        "decision-card",
        decision.is_low_confidence && "decision-card--risky",
        interactive && currentAnswerKind && "decision-card--answered",
      )}
    >
      <div className="decision-card__head">
        <div className="decision-card__head-text">
          <div className="decision-card__badges">
            <StatusPill tone={LEVEL_TONE[decision.level]}>
              {LEVEL_LABEL[decision.level]}
            </StatusPill>
            {interactive === undefined ? (
              <StatusPill tone={STATUS_TONE[decision.status]}>
                {STATUS_LABEL[decision.status]}
              </StatusPill>
            ) : null}
            {decision.is_low_confidence ? (
              <StatusPill tone="warning">
                <AlertTriangle size={12} /> Система не уверена
              </StatusPill>
            ) : null}
            {decision.was_user_modified ? (
              <StatusPill tone="success">
                <CheckCircle2 size={12} /> Изменено вами
              </StatusPill>
            ) : null}
          </div>
          <h3 className="decision-card__title">{decision.title}</h3>
          {decision.description ? (
            <p className="decision-card__description">{decision.description}</p>
          ) : null}
        </div>
        {interactive === undefined ? (
          <button
            type="button"
            className="decision-card__toggle"
            onClick={() => setExpanded((v) => !v)}
            aria-label={expanded ? "Свернуть" : "Развернуть"}
          >
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        ) : null}
      </div>

      {showBody ? (
        <div className="decision-card__body">
          <div className="decision-card__proposed">
            <span className="decision-card__proposed-label">Предложено системой:</span>
            <strong className="decision-card__proposed-value">
              {chosen ? chosen.label : decision.chosen_option_label || "—"}
            </strong>
            {decision.rationale ? (
              <p className="decision-card__rationale">{decision.rationale}</p>
            ) : null}
          </div>

          {decision.alternatives.length > 0 ? (
            <div className="decision-card__alternatives">
              <div className="decision-card__alternatives-label">Альтернативы</div>
              <div className="decision-card__alternatives-list">
                {decision.alternatives.map((alt) => {
                  const isProposed = alt.option_id === decision.chosen_option_id;
                  const isSelected = selectedAlternativeId === alt.option_id;
                  return (
                    <div
                      key={alt.option_id}
                      className={cx(
                        "decision-alt",
                        isProposed && "decision-alt--proposed",
                        isSelected && "decision-alt--selected",
                      )}
                    >
                      <div className="decision-alt__head">
                        <div className="decision-alt__label">
                          {interactive !== undefined ? (
                            <label className="decision-alt__radio">
                              <input
                                type="radio"
                                name={`alt-${decision.decision_id}`}
                                checked={isSelected || (currentAnswerKind === null && isProposed === false ? false : false)}
                                onChange={() =>
                                  handleAnswer({
                                    decision_id: decision.decision_id,
                                    kind: "select_alternative",
                                    selected_option_id: alt.option_id,
                                  })
                                }
                              />
                              <span>{alt.label}</span>
                            </label>
                          ) : (
                            <span>{alt.label}</span>
                          )}
                          {isProposed ? (
                            <span className="decision-alt__hint">по умолчанию</span>
                          ) : null}
                        </div>
                        {alt.confidence !== null ? (
                          <span className="decision-alt__confidence">
                            уверенность {Math.round(alt.confidence * 100)}%
                          </span>
                        ) : null}
                      </div>
                      {alt.description ? (
                        <p className="decision-alt__description">{alt.description}</p>
                      ) : null}
                      {alt.pros.length > 0 || alt.cons.length > 0 ? (
                        <div className="decision-alt__props">
                          {alt.pros.length > 0 ? (
                            <div className="decision-alt__props-col">
                              <span className="decision-alt__props-label">Плюсы</span>
                              <ul>
                                {alt.pros.map((p, i) => (
                                  <li key={i}>{p}</li>
                                ))}
                              </ul>
                            </div>
                          ) : null}
                          {alt.cons.length > 0 ? (
                            <div className="decision-alt__props-col">
                              <span className="decision-alt__props-label">Минусы</span>
                              <ul>
                                {alt.cons.map((c, i) => (
                                  <li key={i}>{c}</li>
                                ))}
                              </ul>
                            </div>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}

          {decision.level_rationale ? (
            <div className="decision-card__meta">
              <span className="decision-card__meta-label">Почему этот уровень:</span>
              <span className="decision-card__meta-value">{decision.level_rationale}</span>
            </div>
          ) : null}

          {decision.user_free_text_answer ? (
            <div className="decision-card__user-text">
              <span className="decision-card__user-text-label">Ваш ответ:</span>
              <p>{decision.user_free_text_answer}</p>
            </div>
          ) : null}

          {/* Interactive controls — только в режиме checkpoint */}
          {interactive !== undefined ? (
            <div className="decision-card__actions">
              <Button
                tone={currentAnswerKind === "accept_default" ? "primary" : "ghost"}
                onClick={() =>
                  handleAnswer({
                    decision_id: decision.decision_id,
                    kind: "accept_default",
                  })
                }
              >
                <CheckCircle2 size={14} /> Принять предложение
              </Button>
              <Button
                tone={currentAnswerKind === "defer" ? "secondary" : "ghost"}
                onClick={() =>
                  handleAnswer({
                    decision_id: decision.decision_id,
                    kind: "defer",
                  })
                }
              >
                <Hourglass size={14} /> Отложить
              </Button>
              {currentAnswerKind !== null ? (
                <Button tone="ghost" onClick={() => handleAnswer(null)}>
                  Сбросить
                </Button>
              ) : null}
            </div>
          ) : null}

          {/* Свободный ответ */}
          {interactive !== undefined ? (
            <details className="decision-card__free-text">
              <summary>Или дать свой ответ</summary>
              <textarea
                className="decision-card__free-input"
                placeholder="Сформулируйте ваш вариант. Он будет применён в неизменном виде."
                value={
                  currentAnswerKind === "free_text"
                    ? interactive.currentAnswer?.free_text ?? ""
                    : freeTextDraft
                }
                onChange={(e) => {
                  const v = e.target.value;
                  setFreeTextDraft(v);
                  if (currentAnswerKind === "free_text") {
                    handleAnswer({
                      decision_id: decision.decision_id,
                      kind: "free_text",
                      free_text: v,
                    });
                  }
                }}
                rows={3}
              />
              {currentAnswerKind !== "free_text" && freeTextDraft.trim() ? (
                <Button
                  tone="primary"
                  onClick={() =>
                    handleAnswer({
                      decision_id: decision.decision_id,
                      kind: "free_text",
                      free_text: freeTextDraft.trim(),
                    })
                  }
                >
                  Применить свой ответ
                </Button>
              ) : null}
            </details>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DecisionsRegistryPage — реестр решений проекта
// ---------------------------------------------------------------------------

type LevelFilter = "all" | DecisionLevel;
type StatusFilter = "all" | DecisionStatus;

export function DecisionsRegistryPage({ projectId }: { projectId: string }) {
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [showRiskyOnly, setShowRiskyOnly] = useState(false);

  const query = useQuery({
    queryKey: ["decisions", projectId, levelFilter, statusFilter],
    queryFn: () =>
      api.getDecisionsRegistry(projectId, {
        level: levelFilter === "all" ? undefined : levelFilter,
        status: statusFilter === "all" ? undefined : statusFilter,
      }),
  });

  if (query.isLoading || !query.data) {
    return <LoadingPanel title="Загружаем реестр решений…" />;
  }
  const view: ProjectDecisionsView = query.data;
  const visibleItems = showRiskyOnly
    ? view.items.filter((d) => d.is_low_confidence)
    : view.items;

  return (
    <div className="decisions-page">
      <SectionCard title="Реестр решений">
        <p className="decisions-page__intro">
          Все решения, которые система приняла или собирается принять при сборке артефактов проекта.
          В режиме <strong>{view.mode}</strong> вы видите как ваши участвующие решения, так и те, что
          были приняты автоматически.
        </p>

        {/* Hero counters */}
        <div className="decisions-hero">
          <DecisionCounter
            label="На вашем уровне"
            value={view.surfaced_total}
            sub={view.surfaced_pending > 0 ? `${view.surfaced_pending} ждут ответа` : "все ответы получены"}
            tone={view.surfaced_pending > 0 ? "active" : "muted"}
            emphasis
          />
          <DecisionCounter label="Бизнес" value={view.business_count} tone="danger" />
          <DecisionCounter label="Архитектура" value={view.architecture_count} tone="warning" />
          <DecisionCounter label="Детали" value={view.detail_count} tone="muted" />
          <DecisionCounter
            label="Система не уверена"
            value={view.low_confidence_count}
            tone={view.low_confidence_count > 0 ? "warning" : "muted"}
          />
        </div>

        {/* Filters toolbar */}
        <div className="decisions-toolbar">
          <div className="decisions-filter">
            <span className="decisions-filter__label">Уровень:</span>
            <div className="segmented">
              {(["all", "business", "architecture", "detail"] as LevelFilter[]).map((f) => (
                <button
                  key={f}
                  type="button"
                  className={cx("segmented__item", levelFilter === f && "segmented__item--active")}
                  onClick={() => setLevelFilter(f)}
                >
                  {f === "all" ? "Все" : LEVEL_LABEL[f as DecisionLevel]}
                </button>
              ))}
            </div>
          </div>
          <div className="decisions-filter">
            <span className="decisions-filter__label">Статус:</span>
            <div className="segmented">
              {(
                ["all", "proposed", "accepted_default", "user_overridden", "deferred"] as StatusFilter[]
              ).map((f) => (
                <button
                  key={f}
                  type="button"
                  className={cx("segmented__item", statusFilter === f && "segmented__item--active")}
                  onClick={() => setStatusFilter(f)}
                >
                  {f === "all" ? "Все" : STATUS_LABEL[f as DecisionStatus]}
                </button>
              ))}
            </div>
          </div>
          <label className="decisions-filter__check">
            <input
              type="checkbox"
              checked={showRiskyOnly}
              onChange={(e) => setShowRiskyOnly(e.target.checked)}
            />
            <span>Только рискованные</span>
          </label>
        </div>

        {visibleItems.length === 0 ? (
          <EmptyState
            title="Решений по этому фильтру нет"
            description={
              view.items.length === 0
                ? "Реестр пополнится по мере прохождения задач workflow. В autopilot решения принимаются молча и сразу попадают сюда; в остальных режимах часть из них вы увидите в checkpoint-сессиях."
                : "Снимите фильтры выше, чтобы увидеть остальные решения."
            }
          />
        ) : (
          <div className="decisions-list">
            {visibleItems.map((d) => (
              <DecisionCard key={d.decision_id} decision={d} />
            ))}
          </div>
        )}
      </SectionCard>
    </div>
  );
}

function DecisionCounter({
  label,
  value,
  sub,
  tone,
  emphasis,
}: {
  label: string;
  value: number;
  sub?: string;
  tone: "active" | "danger" | "warning" | "success" | "muted";
  emphasis?: boolean;
}) {
  return (
    <div className={cx("decision-counter", emphasis && "decision-counter--emphasis")}>
      <span className={cx("decision-counter__value", `decision-counter__value--${tone}`)}>
        {value}
      </span>
      <span className="decision-counter__label">{label}</span>
      {sub ? <span className="decision-counter__sub">{sub}</span> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CheckpointSessionPage — interactive прохождение сессии
// ---------------------------------------------------------------------------

export function CheckpointSessionPage({ projectId }: { projectId: string }) {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["checkpoint", projectId, sessionId],
    queryFn: () => api.getCheckpointDetail(projectId, sessionId!),
    enabled: !!sessionId,
  });

  // Локальное состояние: какие ответы пользователь дал по каждому decision
  const [answers, setAnswers] = useState<Record<string, CheckpointAnswerPayload>>({});

  const submitMutation = useMutation({
    mutationFn: (payload: CheckpointAnswerPayload[]) =>
      api.submitCheckpointAnswers(projectId, sessionId!, payload),
    onSuccess: () => {
      // Инвалидируем затронутые проекции
      queryClient.invalidateQueries({ queryKey: ["checkpoint", projectId, sessionId] });
      queryClient.invalidateQueries({ queryKey: ["decisions", projectId] });
      queryClient.invalidateQueries({ queryKey: ["checkpoints-list", projectId] });
      queryClient.invalidateQueries({ queryKey: ["project-shell", projectId] });
      // Назад на обзор
      navigate(`/projects/${projectId}/overview`);
    },
  });

  if (query.isLoading || !query.data) {
    return <LoadingPanel title="Загружаем сессию…" />;
  }
  const session: CheckpointSessionView = query.data;

  if (session.status !== "pending") {
    return (
      <div className="checkpoint-page">
        <SectionCard title="Сессия уже закрыта">
          <p>
            Эта checkpoint-сессия в статусе <strong>{session.status}</strong>. Ответы по ней
            больше не принимаются.
          </p>
          <Button tone="primary" onClick={() => navigate(`/projects/${projectId}/decisions`)}>
            Открыть реестр решений
          </Button>
        </SectionCard>
      </div>
    );
  }

  const answeredCount = Object.keys(answers).length;
  const totalCount = session.decisions.length;
  const allAnsweredDefault =
    answeredCount === 0 ? null : answeredCount === totalCount;

  const handleSubmit = () => {
    submitMutation.mutate(Object.values(answers));
  };
  const acceptAll = () => {
    const map: Record<string, CheckpointAnswerPayload> = {};
    session.decisions.forEach((d) => {
      map[d.decision_id] = { decision_id: d.decision_id, kind: "accept_default" };
    });
    setAnswers(map);
  };

  return (
    <div className="checkpoint-page">
      <SectionCard
        title={
          <div className="checkpoint-page__title">
            <Button tone="ghost" onClick={() => navigate(`/projects/${projectId}/overview`)}>
              <ArrowLeft size={14} /> К проекту
            </Button>
            <span>Перед сборкой «{session.task_title || session.artifact_role}»</span>
          </div>
        }
      >
        <div className="checkpoint-intro">
          <div className="checkpoint-intro__lead">
            <strong>{totalCount} {totalCount === 1 ? "решение" : totalCount < 5 ? "решения" : "решений"}</strong>{" "}
            ждёт ответа на вашем уровне вовлечения. Подтвердите дефолты или скорректируйте — после
            submit задача продолжится с зафиксированными выборами.
          </div>
          <div className="checkpoint-progress">
            <div
              className="checkpoint-progress__bar"
              style={{ width: `${(answeredCount / Math.max(1, totalCount)) * 100}%` }}
            />
            <span className="checkpoint-progress__text">
              {answeredCount} / {totalCount} отвечено
            </span>
          </div>
          {allAnsweredDefault === null ? (
            <Button tone="primary" onClick={acceptAll}>
              <CheckCircle2 size={14} /> Принять все дефолты
            </Button>
          ) : null}
        </div>

        <div className="checkpoint-decisions">
          {session.decisions.map((decision) => (
            <DecisionCard
              key={decision.decision_id}
              decision={decision}
              defaultExpanded
              interactive={{
                currentAnswer: answers[decision.decision_id] ?? null,
                onAnswerChange: (ans) => {
                  setAnswers((prev) => {
                    const next = { ...prev };
                    if (ans === null) {
                      delete next[decision.decision_id];
                    } else {
                      next[decision.decision_id] = ans;
                    }
                    return next;
                  });
                },
              }}
            />
          ))}
        </div>

        <div className="checkpoint-footer">
          <div className="checkpoint-footer__hint">
            {answeredCount < totalCount
              ? `На оставшиеся ${totalCount - answeredCount} ${
                  totalCount - answeredCount === 1 ? "решение" : "решения"
                } применятся дефолты при отправке.`
              : "Все решения проработаны — можно отправлять."}
          </div>
          <Button
            tone="primary"
            disabled={submitMutation.isPending}
            onClick={handleSubmit}
          >
            <Lock size={14} />
            {submitMutation.isPending ? "Отправка…" : "Подтвердить и продолжить"}
          </Button>
        </div>
      </SectionCard>

      {submitMutation.isError ? (
        <SectionCard title="Ошибка отправки" tone="danger">
          <p>{(submitMutation.error as Error).message}</p>
        </SectionCard>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CheckpointsListPage — список pending checkpoint'ов (когда их несколько)
// ---------------------------------------------------------------------------

export function CheckpointsListPage({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: ["checkpoints-list", projectId],
    queryFn: () => api.getCheckpoints(projectId),
  });
  if (query.isLoading || !query.data) return <LoadingPanel title="Загружаем checkpoint-сессии…" />;
  const view = query.data;
  const pending = view.items.filter((s) => s.status === "pending");
  const finalized = view.items.filter((s) => s.status !== "pending");
  return (
    <div className="checkpoint-page">
      <SectionCard title="Checkpoint-сессии">
        {pending.length === 0 ? (
          <EmptyState
            title="Нет активных сессий"
            description="Когда задача дойдёт до точки, где нужны ваши решения — сессия появится здесь."
          />
        ) : (
          <ul className="checkpoint-list">
            {pending.map((s) => (
              <li key={s.session_id} className="checkpoint-list__item">
                <div>
                  <div className="checkpoint-list__title">{s.task_title || s.artifact_role}</div>
                  <div className="checkpoint-list__meta">
                    {s.decisions.length}{" "}
                    {s.decisions.length === 1 ? "решение" : "решений"} · создано {s.created_at?.slice(0, 16)?.replace("T", " ")}
                  </div>
                </div>
                <Button
                  tone="primary"
                  onClick={() => navigate(`/projects/${projectId}/checkpoints/${s.session_id}`)}
                >
                  Открыть
                </Button>
              </li>
            ))}
          </ul>
        )}
        {finalized.length > 0 ? (
          <details className="checkpoint-list__archive">
            <summary>Закрытые сессии ({finalized.length})</summary>
            <ul className="checkpoint-list">
              {finalized.map((s) => (
                <li key={s.session_id} className="checkpoint-list__item checkpoint-list__item--muted">
                  <div>
                    <div className="checkpoint-list__title">{s.task_title || s.artifact_role}</div>
                    <div className="checkpoint-list__meta">
                      Закрыта {s.finalized_at?.slice(0, 16)?.replace("T", " ")} · статус {s.status}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </SectionCard>
    </div>
  );
}
