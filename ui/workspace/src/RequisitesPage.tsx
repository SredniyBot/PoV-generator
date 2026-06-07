/**
 * Раздел этапа «Реализация»: что нужно от пользователя («Реквизиты») и что мы
 * пока не умеем («Зоны роста»). Обе секции — производные от артефакта оценки
 * реализуемости. Реквизиты — просьба о данных (не блокировка). Зоны роста —
 * требования, не закрытые ни одним умением (кандидаты на расширение каталога).
 *
 * Следующие фазы (см. docs/plans/2026-06-06-realizability-capabilities-redesign.md):
 * предоставление данных прямо в карточке реквизита + продвижение зоны роста в
 * пробное умение.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { RequisiteItemView } from "./types";
import { EmptyState, LoadingPanel, SectionCard, StatusPill } from "./ui";

const KIND_LABELS: Record<string, string> = {
  credential: "доступ/креды",
  dataset: "набор данных",
  file: "файл/таблица",
  setting: "настройка",
  interface_format: "формат интерфейса",
  sample: "образец",
};

function groupByNeededFor(items: RequisiteItemView[]): [string, RequisiteItemView[]][] {
  const groups = new Map<string, RequisiteItemView[]>();
  for (const item of items) {
    const key = item.needed_for || "проект";
    const bucket = groups.get(key);
    if (bucket) bucket.push(item);
    else groups.set(key, [item]);
  }
  return Array.from(groups.entries());
}

function RequisiteRow({
  item,
  onProvide,
  pending,
}: {
  item: RequisiteItemView;
  onProvide: (note: string) => void;
  pending: boolean;
}) {
  const [note, setNote] = useState("");
  const provided = item.status === "provided";
  const kindLabel = item.kind ? KIND_LABELS[item.kind] : undefined;
  return (
    <li className="requisites__item">
      <span className="requisites__item-title">
        {item.title}
        {kindLabel ? <span className="requisites__item-kind"> · {kindLabel}</span> : null}
        {item.blocking ? (
          <span className="requisites__item-blocking"> · обязателен для перехода</span>
        ) : null}
      </span>
      {provided ? (
        <StatusPill tone="success">Получено</StatusPill>
      ) : (
        <span className="requisites__provide">
          <input
            className="requisites__provide-input"
            placeholder="значение / «доступ выдан»"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            type="button"
            className="btn btn--ghost"
            disabled={pending}
            onClick={() => onProvide(note)}
          >
            Предоставлено
          </button>
        </span>
      )}
    </li>
  );
}

function RequisitesSection({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["project", projectId, "requisites"],
    queryFn: () => api.getRequisites(projectId),
  });
  const provide = useMutation({
    mutationFn: ({ key, note }: { key: string; note: string }) =>
      api.provideRequisite(projectId, key, note),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project", projectId, "requisites"] }),
  });

  if (query.isLoading || !query.data) return <LoadingPanel title="Загрузка реквизитов…" />;
  const data = query.data;

  if (data.status === "missing") {
    return (
      <SectionCard title="Реквизиты">
        <EmptyState
          title="Пока нечего предоставлять"
          description="Список появится после оценки реализуемости — она определяет, какие входные данные нужны от вас."
        />
      </SectionCard>
    );
  }
  if (data.items.length === 0) {
    return (
      <SectionCard title="Реквизиты">
        <EmptyState
          title="Дополнительные данные не требуются"
          description="Для того, что взято в реализацию, всё необходимое уже есть."
        />
      </SectionCard>
    );
  }

  return (
    <SectionCard
      title="Реквизиты"
      subtitle="Что нужно предоставить, чтобы продвинуть реализацию. Это просьба — остальное считается без ожидания."
    >
      <div className="requisites">
        {groupByNeededFor(data.items).map(([neededFor, items]) => (
          <div key={neededFor} className="requisites__group">
            <p className="requisites__group-title">Для: {neededFor}</p>
            <ul className="requisites__list">
              {items.map((item) => (
                <RequisiteRow
                  key={`${neededFor}:${item.title}`}
                  item={item}
                  pending={provide.isPending}
                  onProvide={(note) => provide.mutate({ key: item.key || item.title, note })}
                />
              ))}
            </ul>
          </div>
        ))}
      </div>
    </SectionCard>
  );
}

function GapsSection({ projectId }: { projectId: string }) {
  const query = useQuery({
    queryKey: ["project", projectId, "capability-gaps"],
    queryFn: () => api.getCapabilityGaps(projectId),
  });

  // Нет данных/нет оценки/нет пробелов — секцию не показываем (не шумим).
  if (query.isLoading || !query.data) return null;
  const data = query.data;
  if (data.status !== "ready" || data.items.length === 0) return null;

  return (
    <SectionCard
      title="Зоны роста"
      subtitle="Эти требования пока не закрыты нашими умениями. Не «никогда» — кандидаты на то, чтобы научиться и брать такое в будущем."
    >
      <ul className="requisites__list">
        {data.items.map((gap) => (
          <li key={gap.title} className="gap-item">
            <p className="gap-item__title">{gap.title}</p>
            {gap.reason ? <p className="gap-item__reason">Почему: {gap.reason}</p> : null}
            {gap.suggestion ? (
              <p className="gap-item__suggestion">Как закрыть: {gap.suggestion}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </SectionCard>
  );
}

export function RequisitesPage({ projectId }: { projectId: string }) {
  return (
    <div className="stacked-sections">
      <RequisitesSection projectId={projectId} />
      <GapsSection projectId={projectId} />
    </div>
  );
}
