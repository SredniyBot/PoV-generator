/**
 * L6-7: Settings consolidation (§5C IA).
 *
 * Объединяет старые отдельные вкладки «Состояние» / «Замечания» /
 * «Технические детали» в одну Settings-страницу с sub-tabs.
 *
 * Sub-tabs через URL ?tab=state|review|debug. Default = "state".
 * Сохраняет старые URL `/state`, `/review`, `/debug` — они продолжают
 * работать через alias-роуты в App.tsx; Settings — каноническая точка
 * входа.
 */
import { useSearchParams } from "react-router-dom";

interface SettingsPageProps {
  projectId: string;
  panels: Record<SettingsTab, React.ReactNode>;
}

type SettingsTab = "state" | "review" | "debug";

const TAB_ORDER: { id: SettingsTab; label: string; helper: string }[] = [
  {
    id: "state",
    label: "Состояние",
    helper: "Цель, допущения, готовность, активные пакеты — текущий снимок ProblemState.",
  },
  {
    id: "review",
    label: "Замечания",
    helper: "Выводы автоматического ревью артефактов: что сильно, что слабо, что добавить.",
  },
  {
    id: "debug",
    label: "Технические детали",
    helper: "Сырые данные исполнения: задачи, события, манифесты контекста, валидации. Для экспертов.",
  },
];

export function SettingsPage({ panels }: SettingsPageProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = (searchParams.get("tab") as SettingsTab | null) ?? "state";
  const activeTab: SettingsTab =
    TAB_ORDER.find((t) => t.id === requested)?.id ?? "state";

  const activeMeta = TAB_ORDER.find((t) => t.id === activeTab)!;

  return (
    <section className="settings-page">
      <header className="settings-page__header">
        <div>
          <p className="settings-page__eyebrow">Настройки и детали проекта</p>
          <h1 className="settings-page__title">{activeMeta.label}</h1>
          <p className="settings-page__helper">{activeMeta.helper}</p>
        </div>
      </header>

      <nav className="settings-page__tabs" aria-label="Разделы настроек">
        {TAB_ORDER.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`settings-page__tab${
              activeTab === tab.id ? " settings-page__tab--active" : ""
            }`}
            onClick={() => {
              const next = new URLSearchParams(searchParams);
              next.set("tab", tab.id);
              setSearchParams(next, { replace: true });
            }}
            aria-pressed={activeTab === tab.id}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <div className="settings-page__panel">{panels[activeTab]}</div>
    </section>
  );
}
