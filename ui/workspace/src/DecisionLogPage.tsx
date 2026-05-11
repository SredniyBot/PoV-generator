/**
 * L6-5: Decision log page (P7 first-class journal из §5B/§5C).
 *
 * Закрывает M-J5 «поддерживать живой проект после передачи» и
 * M-J6 «защитить ход решений при оспаривании»: каждая запись фиксирует
 * что решили, на основании чего, какие альтернативы рассматривались.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { DecisionLogEntryView } from "./types";

type FilterKind = "all" | "answered" | "assumed" | "auto";

interface DecisionLogPageProps {
  projectId: string;
}

export function DecisionLogPage({ projectId }: DecisionLogPageProps) {
  const log = useQuery({
    queryKey: ["decision-log", projectId],
    queryFn: () => api.getDecisionLog(projectId),
  });
  const [filter, setFilter] = useState<FilterKind>("all");

  const entries = log.data?.entries ?? [];
  const filteredEntries = useMemo(
    () => applyFilter(entries, filter),
    [entries, filter],
  );

  return (
    <section className="decision-log">
      <header className="decision-log__header">
        <div>
          <p className="decision-log__eyebrow">Журнал решений</p>
          <h1 className="decision-log__title">
            {log.data?.total_count ?? 0} принятых решений
          </h1>
        </div>
        <div className="decision-log__counters">
          <Counter
            label="Ответили вы"
            value={log.data?.answered_count ?? 0}
            tone="accent"
          />
          <Counter
            label="Авто-решения"
            value={log.data?.assumed_count ?? 0}
            tone="info"
            icon="🤖"
          />
        </div>
      </header>

      <nav className="decision-log__filters" aria-label="Фильтры">
        <FilterButton current={filter} value="all" onChange={setFilter}>
          Все
        </FilterButton>
        <FilterButton current={filter} value="answered" onChange={setFilter}>
          Ответили
        </FilterButton>
        <FilterButton current={filter} value="assumed" onChange={setFilter}>
          Допущения
        </FilterButton>
        <FilterButton current={filter} value="auto" onChange={setFilter}>
          🤖 Авто
        </FilterButton>
      </nav>

      {log.isLoading ? (
        <p className="decision-log__hint">Загрузка журнала…</p>
      ) : filteredEntries.length === 0 ? (
        <div className="decision-log__empty">
          <p className="decision-log__empty-title">
            {entries.length === 0
              ? "Решений пока нет"
              : "Ничего не подходит под фильтр"}
          </p>
          <p className="decision-log__hint">
            {entries.length === 0
              ? "Когда система задаст вопрос и получит ответ — он появится здесь."
              : "Попробуйте другой фильтр или вкладку «Все»."}
          </p>
        </div>
      ) : (
        <ul className="decision-log__list" role="list">
          {filteredEntries.map((entry) => (
            <DecisionItem key={entry.decision_id} entry={entry} />
          ))}
        </ul>
      )}
    </section>
  );
}

// ---- helpers ----

function applyFilter(
  entries: DecisionLogEntryView[],
  filter: FilterKind,
): DecisionLogEntryView[] {
  switch (filter) {
    case "answered":
      return entries.filter((e) => e.kind === "answered");
    case "assumed":
      return entries.filter((e) => e.kind === "assumed");
    case "auto":
      return entries.filter((e) => e.auto_resolved);
    default:
      return entries;
  }
}

interface CounterProps {
  label: string;
  value: number;
  tone: "accent" | "info" | "muted";
  icon?: string;
}

function Counter({ label, value, tone, icon }: CounterProps) {
  return (
    <div className={`decision-log__counter decision-log__counter--${tone}`}>
      <span className="decision-log__counter-value">
        {icon && <span aria-hidden>{icon} </span>}
        {value}
      </span>
      <span className="decision-log__counter-label">{label}</span>
    </div>
  );
}

interface FilterButtonProps {
  current: FilterKind;
  value: FilterKind;
  onChange: (value: FilterKind) => void;
  children: React.ReactNode;
}

function FilterButton({ current, value, onChange, children }: FilterButtonProps) {
  return (
    <button
      type="button"
      className={`decision-log__filter${current === value ? " decision-log__filter--active" : ""}`}
      onClick={() => onChange(value)}
      aria-pressed={current === value}
    >
      {children}
    </button>
  );
}

interface DecisionItemProps {
  entry: DecisionLogEntryView;
}

function DecisionItem({ entry }: DecisionItemProps) {
  const [showAlternatives, setShowAlternatives] = useState(false);
  const hasAlternatives = entry.alternatives.length > 0;
  const resolution = formatResolution(entry);

  return (
    <li className={`decision-card decision-card--${entry.kind}`}>
      <header className="decision-card__head">
        <div className="decision-card__head-left">
          <span className={`decision-card__kind decision-card__kind--${entry.kind}`}>
            {kindLabel(entry.kind)}
          </span>
          {entry.auto_resolved && (
            <span className="decision-card__auto" title="Авто-решение системы">
              🤖 авто
            </span>
          )}
          <span className="decision-card__role">{ownerRoleLabel(entry.decision_owner_role)}</span>
        </div>
        <time className="decision-card__time" dateTime={entry.decided_at}>
          {formatTimestamp(entry.decided_at)}
        </time>
      </header>

      <h3 className="decision-card__title">{entry.title}</h3>

      {entry.question && (
        <p className="decision-card__question">
          <span className="decision-card__eyebrow">Вопрос:</span> {entry.question}
        </p>
      )}

      <div className="decision-card__resolution">
        <span className="decision-card__eyebrow">Решение:</span> {resolution}
      </div>

      {entry.rationale && (
        <p className="decision-card__rationale">
          <span className="decision-card__eyebrow">Почему:</span> {entry.rationale}
        </p>
      )}

      {hasAlternatives && (
        <div className="decision-card__alternatives">
          <button
            type="button"
            className="decision-card__alternatives-toggle"
            onClick={() => setShowAlternatives((v) => !v)}
            aria-expanded={showAlternatives}
          >
            {showAlternatives ? "▾" : "▸"} Рассматривались альтернативы (
            {entry.alternatives.length})
          </button>
          {showAlternatives && (
            <ul className="decision-card__alternatives-list" role="list">
              {entry.alternatives.map((alt) => (
                <li key={alt.option_id}>
                  <strong>{alt.label}</strong>
                  {alt.description && (
                    <span className="decision-card__alt-desc"> — {alt.description}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <footer className="decision-card__footer">
        <span className="decision-card__source">
          Источник: {sourceLabel(entry.source_type)}
          {entry.source_id ? ` · ${entry.source_id}` : ""}
        </span>
        {entry.related_artifact_ids.length > 0 && (
          <span className="decision-card__related">
            Связано: {entry.related_artifact_ids.length} артефакт(ов)
          </span>
        )}
      </footer>
    </li>
  );
}

function formatResolution(entry: DecisionLogEntryView): string {
  if (entry.resolution_summary) return entry.resolution_summary;
  if (entry.free_text) return entry.free_text;
  if (entry.selected_option_ids.length > 0) {
    const labels = entry.selected_option_ids
      .map((optId) => entry.alternatives.find((a) => a.option_id === optId)?.label ?? optId)
      .join(", ");
    if (labels) return labels;
    return entry.selected_option_ids.join(", ");
  }
  return entry.kind === "assumed" ? "(принято автоматически)" : "(без описания)";
}

function kindLabel(kind: DecisionLogEntryView["kind"]): string {
  return kind === "answered" ? "Ответ" : "Допущение";
}

function ownerRoleLabel(role: string): string {
  const labels: Record<string, string> = {
    business: "Бизнес",
    client: "Заказчик",
    methodologist: "Методолог",
    architect: "Архитектор",
    data_owner: "Данные",
    security: "ИБ",
  };
  return labels[role] ?? role;
}

function sourceLabel(sourceType: string): string {
  const labels: Record<string, string> = {
    task: "задача",
    validation: "валидация",
    planning: "планирование",
    domain_pack: "доменный пак",
    methodology_pack: "методология",
    quality_gate: "согласование (gate)",
  };
  return labels[sourceType] ?? sourceType;
}

function formatTimestamp(iso: string): string {
  if (!iso) return "";
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
