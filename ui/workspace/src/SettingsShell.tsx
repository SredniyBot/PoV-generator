/**
 * Оболочка настроек: верхнеуровневые разделы (Общие / LLM / Окружение).
 *
 * Каждый раздел — своя страница со своими подразделами:
 *   - Общие      → GeneralSettingsPage (режим «дебаг» и пр.)
 *   - LLM        → LlmSettingsPage (Источники / Модели / Назначения)
 *   - Окружение  → MachineRoomPage («Настройки окружения»: Docker, исполнитель)
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

const SECTIONS = [
  { key: "general", label: "Общие", to: "/settings/general" },
  { key: "llm", label: "LLM", to: "/settings/llm" },
  { key: "environment", label: "Окружение", to: "/settings/environment" },
] as const;

export type SettingsSection = (typeof SECTIONS)[number]["key"];

export function SettingsShell({
  section,
  children,
}: {
  section: SettingsSection;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="settings-shell">
      <nav className="settings-shell__sections" role="tablist" aria-label="Разделы настроек">
        {SECTIONS.map((s) => (
          <Link
            key={s.key}
            to={s.to}
            role="tab"
            aria-selected={section === s.key}
            className={
              "settings-shell__section" +
              (section === s.key ? " settings-shell__section--active" : "")
            }
          >
            {s.label}
          </Link>
        ))}
      </nav>
      <div className="settings-shell__body">{children}</div>
    </div>
  );
}
