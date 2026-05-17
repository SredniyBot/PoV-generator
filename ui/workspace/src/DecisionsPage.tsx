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

import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, ChevronDown, ChevronUp, Lock } from "lucide-react";

import { api } from "./api";
import { Button, EmptyState, LoadingPanel, SectionCard, cx } from "./ui";
import type {
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

const STATUS_LABEL: Record<DecisionStatus, string> = {
  proposed: "Ожидает ответа",
  accepted_default: "Принят дефолт",
  user_overridden: "Изменено вами",
  deferred: "Отложено",
  locked_in: "Зафиксировано",
  superseded: "Устарело",
};

/** Текстовое описание уровней, видимых пользователю в режиме (для empty state). */
function humanLevelsForMode(mode: string): string {
  switch (mode) {
    case "autopilot":
      return "ничего (всё решается автоматически)";
    case "balanced":
      return "бизнес";
    case "control":
      return "бизнес и архитектура";
    case "expert":
      return "бизнес, архитектура и детали";
    default:
      return "бизнес";
  }
}

// ---------------------------------------------------------------------------
// DecisionCard — переиспользуемая карточка решения
// ---------------------------------------------------------------------------

interface DecisionCardProps {
  decision: DecisionItemView;
  /** Интерактивный режим (для checkpoint-сессии). */
  interactive?: {
    currentAnswer: CheckpointAnswerPayload | null;
    onAnswerChange: (answer: CheckpointAnswerPayload | null) => void;
  };
  /** В реестре — компактный (свёрнутый), в checkpoint — развёрнутый. */
  defaultExpanded?: boolean;
  /** Скрыть level/status-маркеры (например, в контексте артефакта). */
  hideMeta?: boolean;
}

/**
 * Карточка решения. Минималистичная: вопрос → описание → опции.
 *
 * Дизайн-принципы:
 * - В checkpoint-режиме видны ТОЛЬКО: вопрос, описание, варианты, кнопка
 *   собственного ответа. Никаких ярлыков, статусов, проценов уверенности.
 * - Вариант, предложенный системой, помечен «(по умолчанию)» — мягко,
 *   не как баннер.
 * - Описание каждого варианта — одна строка, без двух-колонок pros/cons.
 *   Если очень нужны pros/cons — они идут как inline-текст, через тире.
 * - В режиме реестра — компактная плашка, разворачивается по клику.
 * - Уровень показывается малозаметным indicator (точка-цвет), не плашкой.
 */
function DecisionCard({
  decision,
  interactive,
  defaultExpanded = false,
  hideMeta = false,
}: DecisionCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [freeTextDraft, setFreeTextDraft] = useState<string>("");
  const [freeTextOpen, setFreeTextOpen] = useState(false);

  const currentAnswerKind = interactive?.currentAnswer?.kind ?? null;
  const selectedAlternativeId =
    currentAnswerKind === "select_alternative"
      ? interactive?.currentAnswer?.selected_option_id ?? null
      : null;

  const handleAnswer = (answer: CheckpointAnswerPayload | null) => {
    interactive?.onAnswerChange(answer);
  };

  const isInteractive = interactive !== undefined;
  const showBody = expanded || isInteractive;

  // Дефолтное состояние interactive: выбран предложенный вариант (radio
  // позиция там), но в ответе пользователя ничего нет (null) — это значит
  // «принимаю по умолчанию» при submit.
  const proposedId = decision.chosen_option_id;
  const effectiveSelectedId =
    selectedAlternativeId ??
    (currentAnswerKind === "accept_default" || currentAnswerKind === null ? proposedId : null);

  return (
    <div
      className={cx(
        "decision-card",
        isInteractive && "decision-card--interactive",
        isInteractive && currentAnswerKind && "decision-card--answered",
      )}
    >
      <header
        className={cx("decision-card__head", !isInteractive && "decision-card__head--clickable")}
        onClick={!isInteractive ? () => setExpanded((v) => !v) : undefined}
      >
        {!hideMeta ? (
          <span
            className={cx("decision-card__level-dot", `decision-card__level-dot--${decision.level}`)}
            title={`Уровень: ${LEVEL_LABEL[decision.level]}${decision.is_low_confidence ? " · Система не уверена" : ""}`}
          />
        ) : null}
        <div className="decision-card__head-text">
          <h3 className="decision-card__title">
            {decision.title}
            {decision.is_low_confidence ? (
              <AlertTriangle
                size={14}
                className="decision-card__risky-icon"
                aria-label="Система не уверена в дефолте"
              />
            ) : null}
          </h3>
          {!isInteractive && !hideMeta ? (
            <span className="decision-card__head-status">{STATUS_LABEL[decision.status]}</span>
          ) : null}
        </div>
        {!isInteractive ? (
          <button
            type="button"
            className="decision-card__toggle"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
            aria-label={expanded ? "Свернуть" : "Развернуть"}
          >
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        ) : null}
      </header>

      {showBody ? (
        <div className="decision-card__body">
          {decision.description ? (
            <p className="decision-card__description">{decision.description}</p>
          ) : null}

          {decision.alternatives.length > 0 ? (
            <ul className="decision-card__options" role="radiogroup">
              {decision.alternatives.map((alt) => {
                const isProposed = alt.option_id === proposedId;
                const isSelected = effectiveSelectedId === alt.option_id;
                return (
                  <li key={alt.option_id} className="decision-card__option">
                    {isInteractive ? (
                      <label className="decision-card__option-label">
                        <input
                          type="radio"
                          name={`alt-${decision.decision_id}`}
                          checked={isSelected}
                          onChange={() => {
                            setFreeTextOpen(false);
                            // Если выбран дефолтный — это accept_default
                            // (чтобы при submit не дёргать ненужный override).
                            if (alt.option_id === proposedId) {
                              handleAnswer({
                                decision_id: decision.decision_id,
                                kind: "accept_default",
                              });
                            } else {
                              handleAnswer({
                                decision_id: decision.decision_id,
                                kind: "select_alternative",
                                selected_option_id: alt.option_id,
                              });
                            }
                          }}
                        />
                        <span className="decision-card__option-content">
                          <span className="decision-card__option-title">
                            {alt.label}
                            {isProposed ? (
                              <span className="decision-card__option-hint">(по умолчанию)</span>
                            ) : null}
                          </span>
                          {alt.description ? (
                            <span className="decision-card__option-desc">{alt.description}</span>
                          ) : null}
                        </span>
                      </label>
                    ) : (
                      <span className="decision-card__option-content">
                        <span
                          className={cx(
                            "decision-card__option-title",
                            isProposed && "decision-card__option-title--chosen",
                          )}
                        >
                          {alt.label}
                          {isProposed ? (
                            <span className="decision-card__option-hint">(выбрано)</span>
                          ) : null}
                        </span>
                        {alt.description ? (
                          <span className="decision-card__option-desc">{alt.description}</span>
                        ) : null}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          ) : null}

          {decision.user_free_text_answer ? (
            <div className="decision-card__user-text">
              <span className="decision-card__user-text-label">Ваш ответ:</span>
              <p>{decision.user_free_text_answer}</p>
            </div>
          ) : null}

          {isInteractive ? (
            <div className="decision-card__free">
              <button
                type="button"
                className="decision-card__free-toggle"
                onClick={() => setFreeTextOpen((v) => !v)}
              >
                {freeTextOpen ? "Скрыть свой ответ" : "Дать свой ответ"}
              </button>
              {freeTextOpen ? (
                <textarea
                  className="decision-card__free-input"
                  placeholder="Сформулируйте свой вариант. Он будет применён в неизменном виде."
                  value={
                    currentAnswerKind === "free_text"
                      ? interactive.currentAnswer?.free_text ?? ""
                      : freeTextDraft
                  }
                  onChange={(e) => {
                    const v = e.target.value;
                    setFreeTextDraft(v);
                    if (v.trim()) {
                      handleAnswer({
                        decision_id: decision.decision_id,
                        kind: "free_text",
                        free_text: v,
                      });
                    } else if (currentAnswerKind === "free_text") {
                      handleAnswer({
                        decision_id: decision.decision_id,
                        kind: "accept_default",
                      });
                    }
                  }}
                  rows={3}
                />
              ) : null}
            </div>
          ) : null}

          {/* Дополнительная меторматация в реестре — collapsed по умолчанию */}
          {!isInteractive && (decision.rationale || decision.level_rationale) ? (
            <details className="decision-card__more">
              <summary>Подробнее</summary>
              {decision.rationale ? (
                <p className="decision-card__more-line">
                  <span className="decision-card__more-label">Почему:</span>
                  {decision.rationale}
                </p>
              ) : null}
              {decision.level_rationale ? (
                <p className="decision-card__more-line">
                  <span className="decision-card__more-label">Уровень:</span>
                  {decision.level_rationale}
                </p>
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
  const navigate = useNavigate();
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
  // v3.0: показываем banner со ссылкой на legacy /clarifications, если
  // там есть открытые вопросы (fallback-путь для случаев, когда pre-flight
  // не предусмотрел вопрос).
  const clarificationsQuery = useQuery({
    queryKey: ["clarifications-open-banner", projectId],
    queryFn: () => api.getClarifications(projectId),
  });

  if (query.isLoading || !query.data) {
    return <LoadingPanel title="Загружаем реестр решений…" />;
  }
  const view: ProjectDecisionsView = query.data;
  const visibleItems = showRiskyOnly
    ? view.items.filter((d) => d.is_low_confidence)
    : view.items;

  const openClarCount = clarificationsQuery.data?.open_count ?? 0;

  return (
    <div className="decisions-page">
      {openClarCount > 0 ? (
        <div className="decisions-page__legacy-banner">
          <div>
            <strong>{openClarCount}</strong>{" "}
            {openClarCount === 1 ? "вопрос ждёт ответа" : "вопросов ждут ответа"} в старом потоке
            (fallback от валидации). Он не попал в pre-flight, но требует вашего внимания.
          </div>
          <Button
            tone="secondary"
            onClick={() => navigate(`/projects/${projectId}/clarifications`)}
          >
            Открыть
          </Button>
        </div>
      ) : null}

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
                ? `Реестр пополнится по мере прохождения задач workflow. В режиме «${view.mode}» вам будут показаны решения уровней: ${humanLevelsForMode(view.mode)}. Остальное система примет автоматически и тоже разместит здесь — для просмотра постфактум.`
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
      // Инвалидируем все затронутые проекции — backend сразу же стартует
      // новый workflow run, нам нужно подхватить прогресс в шапке.
      queryClient.invalidateQueries({ queryKey: ["checkpoint", projectId, sessionId] });
      queryClient.invalidateQueries({ queryKey: ["decisions", projectId] });
      queryClient.invalidateQueries({ queryKey: ["checkpoints-list", projectId] });
      queryClient.invalidateQueries({ queryKey: ["project-shell", projectId] });
      queryClient.invalidateQueries({ queryKey: [projectId, "workflow-run-active"] });
      queryClient.invalidateQueries({ queryKey: [projectId, "workflow-runs"] });
      queryClient.invalidateQueries({ queryKey: [projectId, "task-graph"] });
      queryClient.invalidateQueries({ queryKey: [projectId, "artifacts"] });
      // Назад на обзор — там пользователь видит свежий workflow run progress
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

  const totalCount = session.decisions.length;
  const overriddenCount = Object.values(answers).filter(
    (a) => a.kind !== "accept_default",
  ).length;

  const handleSubmit = () => {
    submitMutation.mutate(Object.values(answers));
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
        <p className="checkpoint-intro__lead">
          {totalCount === 1 ? "1 вопрос" : `${totalCount} вопросов`} перед продолжением.
          Все ответы уже выбраны системой по умолчанию — измените те, по которым не согласны, и отправьте.
        </p>

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
            {overriddenCount === 0
              ? "Будут применены варианты по умолчанию."
              : `Изменено: ${overriddenCount} из ${totalCount}.`}
          </div>
          <Button tone="primary" disabled={submitMutation.isPending} onClick={handleSubmit}>
            <Lock size={14} />
            {submitMutation.isPending ? "Отправка…" : "Отправить и продолжить"}
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
