/**
 * Реквизиты — требуемые от пользователя входные данные (этап «Реализация»).
 *
 * Фаза 1: показываем, что нужно предоставить (доступ / файл / настройка /
 * значение), сгруппированно по тому, что это разблокирует. Источник —
 * предусловия из артефакта оценки реализуемости. Это просьба, а не
 * блокировка: пока данные не получены, конвейер идёт по остальному.
 *
 * Следующая фаза (см. docs/plans/2026-06-06-realizability-capabilities-redesign.md):
 * предоставление данных прямо в карточке + переоценка зависящего требования.
 */
import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { RequisiteItemView } from "./types";
import { EmptyState, LoadingPanel, SectionCard, StatusPill } from "./ui";

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

export function RequisitesPage({ projectId }: { projectId: string }) {
  const query = useQuery({
    queryKey: ["project", projectId, "requisites"],
    queryFn: () => api.getRequisites(projectId),
  });

  if (query.isLoading || !query.data) {
    return <LoadingPanel title="Загрузка реквизитов…" />;
  }

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

  const groups = groupByNeededFor(data.items);

  return (
    <SectionCard
      title="Реквизиты"
      subtitle="Что нужно предоставить, чтобы продвинуть реализацию. Это просьба — остальное считается без ожидания."
    >
      <div className="requisites">
        {groups.map(([neededFor, items]) => (
          <div key={neededFor} className="requisites__group">
            <p className="requisites__group-title">Для: {neededFor}</p>
            <ul className="requisites__list">
              {items.map((item) => (
                <li key={`${neededFor}:${item.title}`} className="requisites__item">
                  <span className="requisites__item-title">{item.title}</span>
                  <StatusPill tone={item.status === "provided" ? "success" : "warning"}>
                    {item.status === "provided" ? "Получено" : "Нужно предоставить"}
                  </StatusPill>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </SectionCard>
  );
}
