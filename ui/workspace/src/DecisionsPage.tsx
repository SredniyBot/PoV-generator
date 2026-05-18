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
import { useNavigate, useParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, ChevronDown, ChevronUp, Download, FileText, Lock } from "lucide-react";

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

/**
 * Парсим LLM-сгенерированные заголовки вида «Раздел N.M: subject — question».
 * Возвращает { sectionTag, cleanTitle } — секция в маленький pill, чистый
 * заголовок крупнее. Если префикса нет — возвращает оригинал в cleanTitle.
 */
function splitSectionPrefix(title: string): { sectionTag: string | null; cleanTitle: string } {
  // Маска: «Раздел X[.Y][.Z]:» или «Section X[.Y]:» в начале
  const m = title.match(/^\s*(Раздел|Section)\s+([0-9.]+)\s*:\s*(.+)$/i);
  if (m && m[2] && m[3]) {
    return { sectionTag: m[2], cleanTitle: m[3].trim() };
  }
  return { sectionTag: null, cleanTitle: title };
}

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
  const queryClient = useQueryClient();

  // v3.4: пометить рискованное решение как «просмотрено» — снимает badge.
  // Доступно только в read-only-режиме (реестр), не в checkpoint-сессии.
  const verifyMutation = useMutation({
    mutationFn: () =>
      api.verifyDecision(decision.project_id, decision.decision_id, true),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["decisions", decision.project_id] });
    },
  });

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

  // v3.3: парсим LLM-префикс «Раздел N.M:» — выносим в маленький тег
  const { sectionTag, cleanTitle } = splitSectionPrefix(decision.title);
  const chosenAlt = decision.alternatives.find((a) => a.option_id === decision.chosen_option_id);

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
            {sectionTag ? <span className="decision-card__section-tag">§ {sectionTag}</span> : null}
            <span className="decision-card__question">{cleanTitle}</span>
            {decision.is_low_confidence ? (
              <span
                className="decision-card__risky-badge"
                title="Уверенность системы в предложенном дефолте ниже 0.5 — стоит просмотреть лично"
              >
                <AlertTriangle size={12} className="decision-card__risky-icon" />
                <span className="decision-card__risky-text">система не уверена</span>
                {!isInteractive ? (
                  <button
                    type="button"
                    className="decision-card__risky-verify"
                    title="Я просмотрел этот выбор — согласен, снять метку"
                    disabled={verifyMutation.isPending}
                    onClick={(e) => {
                      e.stopPropagation();
                      verifyMutation.mutate();
                    }}
                  >
                    <Check size={11} />
                    <span>подтверждаю</span>
                  </button>
                ) : null}
              </span>
            ) : decision.user_verified && !isInteractive ? (
              <span
                className="decision-card__verified-badge"
                title={
                  decision.user_verified_at
                    ? `Подтверждено вами ${decision.user_verified_at.slice(0, 16).replace("T", " ")}`
                    : "Подтверждено вами"
                }
              >
                <Check size={11} />
                <span>подтверждено</span>
              </span>
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
          {decision.description && decision.description !== decision.title ? (
            <p className="decision-card__description">{decision.description}</p>
          ) : null}

          {/* INTERACTIVE РЕЖИМ (checkpoint): radio/checkbox для выбора */}
          {isInteractive && decision.alternatives.length > 0 ? (
            <ul
              className="decision-card__options"
              role={decision.answer_mode === "multiple" ? "group" : "radiogroup"}
            >
              {decision.alternatives.map((alt) => {
                const isProposed = alt.option_id === proposedId;
                const isMulti = decision.answer_mode === "multiple";
                let isSelected: boolean;
                if (isMulti) {
                  const ids =
                    interactive?.currentAnswer?.kind === "select_alternative"
                      ? interactive.currentAnswer.selected_option_ids ?? []
                      : decision.chosen_option_ids ?? [];
                  isSelected = ids.includes(alt.option_id);
                } else {
                  isSelected = effectiveSelectedId === alt.option_id;
                }
                return (
                  <li key={alt.option_id} className="decision-card__option">
                    <label className="decision-card__option-label">
                      <input
                        type={isMulti ? "checkbox" : "radio"}
                        name={`alt-${decision.decision_id}`}
                        checked={isSelected}
                        onChange={(e) => {
                          setFreeTextOpen(false);
                          if (isMulti) {
                            const prevIds =
                              interactive?.currentAnswer?.kind === "select_alternative"
                                ? interactive.currentAnswer.selected_option_ids ?? []
                                : decision.chosen_option_ids ?? [];
                            const nextIds = e.target.checked
                              ? [...prevIds.filter((x) => x !== alt.option_id), alt.option_id]
                              : prevIds.filter((x) => x !== alt.option_id);
                            handleAnswer({
                              decision_id: decision.decision_id,
                              kind: "select_alternative",
                              selected_option_ids: nextIds,
                            });
                          } else if (alt.option_id === proposedId) {
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
                          {isProposed && !isMulti ? (
                            <span className="decision-card__option-hint">(по умолчанию)</span>
                          ) : null}
                          {alt.confidence !== null && alt.confidence !== undefined ? (
                            <span
                              className={cx(
                                "decision-card__option-confidence",
                                alt.confidence < 0.5 && "decision-card__option-confidence--low",
                              )}
                              title="Уверенность системы в этом варианте"
                            >
                              {Math.round(alt.confidence * 100)}%
                            </span>
                          ) : null}
                        </span>
                        {alt.description ? (
                          <span className="decision-card__option-desc">{alt.description}</span>
                        ) : null}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          ) : null}

          {/* READ-ONLY РЕЖИМ (реестр): выбранный вариант — prominent */}
          {!isInteractive ? (
            <div className="decision-card__chosen-box">
              <div className="decision-card__chosen-head">
                <span className="decision-card__chosen-icon" aria-hidden="true">✓</span>
                <span className="decision-card__chosen-label">Выбрано:</span>
                <span className="decision-card__chosen-value">
                  {chosenAlt ? chosenAlt.label : decision.chosen_option_label || (decision.user_free_text_answer ? "Свой ответ" : "—")}
                </span>
                {chosenAlt && chosenAlt.confidence !== null && chosenAlt.confidence !== undefined ? (
                  <span
                    className={cx(
                      "decision-card__option-confidence",
                      chosenAlt.confidence < 0.5 && "decision-card__option-confidence--low",
                    )}
                    title="Уверенность системы в выбранном варианте"
                  >
                    {Math.round(chosenAlt.confidence * 100)}%
                  </span>
                ) : null}
              </div>
              {chosenAlt?.description ? (
                <p className="decision-card__chosen-desc">{chosenAlt.description}</p>
              ) : null}
              {decision.rationale && decision.rationale !== chosenAlt?.description ? (
                <p className="decision-card__chosen-rationale">{decision.rationale}</p>
              ) : null}
              {decision.user_free_text_answer ? (
                <p className="decision-card__chosen-freetext">«{decision.user_free_text_answer}»</p>
              ) : null}
            </div>
          ) : null}

          {/* INTERACTIVE: свой ответ + free_text mode (когда нет альтернатив) */}
          {isInteractive ? (
            <div className="decision-card__free">
              {decision.alternatives.length === 0 || decision.answer_mode === "free_text" ? (
                /* Чистый free_text — textarea + «Принять как есть» */
                <div className="decision-card__free--primary">
                  <textarea
                    className="decision-card__free-input"
                    placeholder="Ваш ответ…"
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
                    rows={4}
                  />
                  <button
                    type="button"
                    className="decision-card__free-skip"
                    onClick={() => {
                      setFreeTextDraft("");
                      handleAnswer({
                        decision_id: decision.decision_id,
                        kind: "accept_default",
                      });
                    }}
                  >
                    Не знаю · принять как есть
                  </button>
                </div>
              ) : (
                /* Опции есть, но даём escape hatch */
                <>
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
                      placeholder="Свой вариант — будет применён в неизменном виде."
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
                </>
              )}
            </div>
          ) : null}

          {/* META-БЛОК в режиме реестра: артефакт + альтернативы dropdown */}
          {!isInteractive ? (
            <div className="decision-card__meta-row">
              {decision.affected_artifact_ids.length > 0 ? (
                <Link
                  to={`/projects/${decision.project_id}/artifacts/${decision.affected_artifact_ids[0]}`}
                  className="decision-card__artifact-link"
                  onClick={(e) => e.stopPropagation()}
                >
                  <FileText size={13} />
                  Артефакт
                </Link>
              ) : null}
              {decision.alternatives.length > 1 ? (
                <details className="decision-card__alts-drop">
                  <summary>
                    Альтернативы ({decision.alternatives.length - 1})
                  </summary>
                  <ul className="decision-card__alts-list">
                    {decision.alternatives
                      .filter((a) => a.option_id !== decision.chosen_option_id)
                      .map((alt) => (
                        <li key={alt.option_id}>
                          <span className="decision-card__alt-title">
                            {alt.label}
                            {alt.confidence !== null && alt.confidence !== undefined ? (
                              <span
                                className={cx(
                                  "decision-card__option-confidence",
                                  alt.confidence < 0.5 && "decision-card__option-confidence--low",
                                )}
                                title="Уверенность системы в этом варианте"
                              >
                                {Math.round(alt.confidence * 100)}%
                              </span>
                            ) : null}
                          </span>
                          {alt.description ? (
                            <span className="decision-card__alt-desc">{alt.description}</span>
                          ) : null}
                        </li>
                      ))}
                  </ul>
                </details>
              ) : null}
            </div>
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
  const filteredItems = showRiskyOnly
    ? view.items.filter((d) => d.is_low_confidence)
    : view.items;
  // Сортировка по важности — самые требующие внимания вверху:
  //   1. status="proposed" (ждут ответа пользователя)
  //   2. is_low_confidence (LLM не уверена в дефолте)
  //   3. По уровню: business → architecture → detail
  //   4. По дате создания (свежее — выше)
  const levelWeight: Record<DecisionLevel, number> = {
    business: 0,
    architecture: 1,
    detail: 2,
  };
  const visibleItems = [...filteredItems].sort((a, b) => {
    const aPending = a.status === "proposed" ? 0 : 1;
    const bPending = b.status === "proposed" ? 0 : 1;
    if (aPending !== bPending) return aPending - bPending;
    const aRisky = a.is_low_confidence ? 0 : 1;
    const bRisky = b.is_low_confidence ? 0 : 1;
    if (aRisky !== bRisky) return aRisky - bRisky;
    const lw = levelWeight[a.level] - levelWeight[b.level];
    if (lw !== 0) return lw;
    return (b.created_at || "").localeCompare(a.created_at || "");
  });

  return (
    <div className="decisions-page">
      <SectionCard
        title={
          <div className="decisions-page__header">
            <span>Реестр решений</span>
            <a
              className="decisions-page__download"
              href={`/api/projects/${projectId}/decisions/export.pdf`}
              target="_blank"
              rel="noreferrer"
              title="Скачать весь реестр в PDF"
            >
              <Download size={14} /> Скачать PDF
            </a>
          </div>
        }
      >
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
