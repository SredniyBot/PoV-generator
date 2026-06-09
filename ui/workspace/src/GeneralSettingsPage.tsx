/**
 * Раздел настроек «Общие»: системные предпочтения приложения, не привязанные к
 * конкретному проекту. Сейчас единственная настройка — режим «дебаг».
 *
 * Дебаг открывает в окне артефакта технические поля: Проверки, Provenance, JSON
 * (сырой выход задачи) и Контекст (запрос к LLM, поданный задаче). Без дебага
 * окно артефакта показывает только Документ / Рассуждение / Решения.
 *
 * Источник истины — backend (`/api/settings/app`, таблица app_settings в
 * settings.db), чтобы предпочтение не зависело от браузера.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bug, Loader2 } from "lucide-react";

import { api } from "./api";

export function GeneralSettingsPage(): JSX.Element {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ["app-settings"],
    queryFn: () => api.getAppSettings(),
  });
  const debug = settingsQuery.data?.debug ?? false;

  const mutation = useMutation({
    mutationFn: (next: boolean) => api.setAppSettings({ debug: next }),
    onSuccess: (data) => {
      queryClient.setQueryData(["app-settings"], data);
      void queryClient.invalidateQueries({ queryKey: ["app-settings"] });
    },
  });

  return (
    <div className="llm-settings mroom">
      <header className="llm-settings__header">
        <h1>Общие настройки</h1>
        <p className="mroom__subtitle">
          Системные предпочтения приложения. Действуют для всех проектов.
        </p>
      </header>

      <section className="mroom-card">
        <h2 className="mroom-card__title">
          <Bug size={16} /> Режим разработчика
        </h2>
        <p className="general-settings__desc">
          Когда дебаг включён, в окне артефакта появляются технические поля:{" "}
          <strong>Проверки</strong>, <strong>Provenance</strong>, <strong>JSON</strong> (сырой
          выход задачи) и <strong>Контекст</strong> (запрос к LLM, поданный задаче). Без дебага
          окно показывает только Документ, Рассуждение и Решения.
        </p>
        <label className="general-settings__toggle">
          <input
            type="checkbox"
            checked={debug}
            disabled={settingsQuery.isLoading || mutation.isPending}
            onChange={(event) => mutation.mutate(event.target.checked)}
          />
          <span>Дебаг</span>
          {mutation.isPending ? <Loader2 size={14} className="spin" /> : null}
        </label>
      </section>
    </div>
  );
}
