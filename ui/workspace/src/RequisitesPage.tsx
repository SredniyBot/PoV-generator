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

function RequisitesSection({ projectId }: { projectId: string }) {
  const query = useQuery({
    queryKey: ["project", projectId, "requisites"],
    queryFn: () => api.getRequisites(projectId),
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
