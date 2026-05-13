/**
 * L6-5: Decision log page (P7 first-class journal).
 * Унифицирован под общий стиль workspace: SectionCard + segmented + StatusPill.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { DecisionLogEntryView } from "./types";
import { SectionCard, StatusPill, EmptyState, cx } from "./ui";

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

  const counts = {
    all: entries.length,
    answered: entries.filter((e) => e.kind === "answered").length,
    assumed: entries.filter((e) => e.kind === "assumed").length,
    auto: entries.filter((e) => e.auto_resolved).length,
  };

  return (
    <div className="decision-log-page">
      <SectionCard
        title="Журнал решений"
        subtitle={`Всего ${log.data?.total_count ?? 0} принятых решений по проекту`}
      >
        <div className="clar-hero">
          <DecisionCounter label="Всего" value={counts.all} tone="active" emphasis />
          <DecisionCounter label="Ответили вы" value={counts.answered} tone="success" />
          <DecisionCounter label="Допущения" value={counts.assumed} tone="muted" />
          <DecisionCounter label="🤖 Авто" value={counts.auto} tone="active" />
        </div>

        <div className="clar-toolbar">
          <div className="segmented">
            {(["all", "answered", "assumed", "auto"] as FilterKind[]).map((f) => (
              <button
                key={f}
                type="button"
                className={cx("segmented__item", filter === f && "segmented__item--active")}
                onClick={() => setFilter(f)}
              >
                {filterLabel(f)} ({counts[f]})
              </button>
            ))}
          </div>
        </div>

        {log.isLoading ? (
          <p className="decision-log__hint">Загрузка журнала…</p>
        ) : filteredEntries.length === 0 ? (
          <EmptyState
            title={
              entries.length === 0
                ? "Решений пока нет"
                : "Ничего не подходит под фильтр"
            }
            description={
              entries.length === 0
                ? "Когда система задаст вопрос и получит ответ — он появится здесь."
                : "Попробуйте другой фильтр или вкладку «Все»."
            }
          />
        ) : (
          <ul className="decision-list">
            {filteredEntries.map((entry) => (
              <DecisionItem key={entry.decision_id} entry={entry} />
            ))}
          </ul>
        )}
      </SectionCard>
    </div>
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

function filterLabel(f: FilterKind): string {
  switch (f) {
    case "all": return "Все";
    case "answered": return "Ответы";
    case "assumed": return "Допущения";
    case "auto": return "🤖 Авто";
  }
}

interface DecisionCounterProps {
  label: string;
  value: number;
  tone: "active" | "success" | "warning" | "danger" | "muted";
  emphasis?: boolean;
}

function DecisionCounter({ label, value, tone, emphasis }: DecisionCounterProps) {
  return (
    <div className={cx("clar-counter", `clar-counter--${tone}`, emphasis && "clar-counter--emphasis")}>
      <span className="clar-counter__value">{value}</span>
      <span className="clar-counter__label">{label}</span>
    </div>
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
    <li className={cx("decision-row", `decision-row--${entry.kind}`)}>
      <div className="decision-row__head">
        <StatusPill tone={entry.kind === "answered" ? "success" : "muted"}>
          {kindLabel(entry.kind)}
        </StatusPill>
        {entry.auto_resolved ? (
          <span className="clar-auto-badge" title="Авто-решение системы">🤖 авто</span>
        ) : null}
        <span className={cx("clar-role", `clar-role--${entry.decision_owner_role}`)}>
          {ownerRoleLabel(entry.decision_owner_role)}
        </span>
        <span className="decision-row__time">{formatTimestamp(entry.decided_at)}</span>
      </div>

      <div className="decision-row__title">{entry.title}</div>

      {entry.question ? (
        <div className="decision-row__field">
          <span className="decision-row__field-label">Вопрос</span>
          <span>{entry.question}</span>
        </div>
      ) : null}

      <div className="decision-row__field decision-row__field--resolution">
        <span className="decision-row__field-label">Решение</span>
        <span>{resolution}</span>
      </div>

      {entry.rationale ? (
        <div className="decision-row__field">
          <span className="decision-row__field-label">Почему</span>
          <span>{entry.rationale}</span>
        </div>
      ) : null}

      {hasAlternatives ? (
        <details
          className="decision-row__alternatives"
          open={showAlternatives}
          onToggle={(e) => setShowAlternatives((e.target as HTMLDetailsElement).open)}
        >
          <summary>Альтернативы ({entry.alternatives.length})</summary>
          <ul>
            {entry.alternatives.map((alt) => (
              <li key={alt.option_id}>
                <strong>{alt.label}</strong>
                {alt.description ? <span> — {alt.description}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <div className="decision-row__footer">
        <span>Источник: {sourceLabel(entry.source_type)}</span>
        {entry.source_id ? <span className="decision-row__source-id">{entry.source_id}</span> : null}
        {entry.related_artifact_ids.length > 0 ? (
          <span>· {entry.related_artifact_ids.length} связанных артефакт(а/ов)</span>
        ) : null}
      </div>
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
