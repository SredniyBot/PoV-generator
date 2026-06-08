/**
 * Раздел этапа «Реализация»: что нужно от пользователя («Реквизиты») и что мы
 * пока не умеем («Зоны роста»).
 *
 * Реквизиты v2 (Ф5): гибкий мультиформатный приём. Вид (`kind`) — лишь подсказка
 * (дефолтный режим + пример), не контракт; источник истины о форме данных —
 * пользователь, поэтому режим редактируемый и валидация мягкая. Режимы:
 *  — данные: значение / ссылка-выдано (файл добавится отдельной фазой);
 *  — обход: допущение / позже / неприменимо (честный гейтинг — снимает блок
 *    только задачи-потребителя).
 * Безопасность: для credential поле значения не предлагается — только «выдано
 * вне системы» (секрет в систему не попадает).
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { RequisiteItemView } from "./types";
import { EmptyState, LoadingPanel, SectionCard, StatusPill } from "./ui";

type ProvideMode = "value" | "file" | "reference" | "assumption" | "deferred" | "not_applicable";

interface ProvidePayload {
  mode: ProvideMode;
  value?: string;
  note?: string;
  attachment_id?: string;
}

const KIND_LABELS: Record<string, string> = {
  credential: "доступ/креды",
  dataset: "набор данных",
  file: "файл/таблица",
  setting: "настройка",
  interface_format: "формат интерфейса",
  sample: "образец",
};

// Подсказка-пример по виду (advisory, не контракт).
const KIND_HINTS: Record<string, string> = {
  credential:
    "Не вводите секрет. Отметьте, что доступ выдан вне системы (Vault, почта, отдельный канал).",
  dataset: "Например, CSV/Excel с примером строк — или опишите структуру значением.",
  file: "Например, таблица или документ — можно вставить содержимое значением.",
  sample: "Например, пример заполненной формы или записи.",
  interface_format: "Например, JSON-схема, список полей или описание формата.",
  setting: "Например, значение тайм-аута, лимит, флаг.",
};

const MODE_LABELS: Record<ProvideMode, string> = {
  value: "Значение",
  file: "Файл",
  reference: "Ссылка / выдано",
  assumption: "Допущение",
  deferred: "Позже",
  not_applicable: "Неприменимо",
};

const PROVIDED_LABELS: Record<string, string> = {
  value: "Значение получено",
  file: "Файл получен",
  reference: "Выдано / ссылка",
  assumption: "Допущение",
  deferred: "Отложено",
  not_applicable: "Неприменимо",
};

function providedTone(mode: string): "success" | "active" | "muted" {
  if (mode === "value" || mode === "file" || mode === "reference") return "success";
  if (mode === "assumption") return "active";
  return "muted"; // deferred / not_applicable
}

function defaultMode(kind?: string): ProvideMode {
  return kind === "credential" ? "reference" : "value";
}

// credential нельзя передать значением/файлом/допущением (секрет) — только «выдано».
function modesFor(kind?: string): ProvideMode[] {
  if (kind === "credential") return ["reference", "deferred", "not_applicable"];
  return ["value", "file", "reference", "assumption", "deferred", "not_applicable"];
}

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
  projectId,
  item,
  onProvide,
  pending,
}: {
  projectId: string;
  item: RequisiteItemView;
  onProvide: (payload: ProvidePayload) => void;
  pending: boolean;
}) {
  const provided = item.status === "provided";
  const kindLabel = item.kind ? KIND_LABELS[item.kind] : undefined;
  const hint = item.kind ? KIND_HINTS[item.kind] : undefined;

  const [editing, setEditing] = useState(false);
  const [mode, setMode] = useState<ProvideMode>(defaultMode(item.kind));
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const showForm = !provided || editing;
  const needsText = mode === "value" || mode === "assumption";
  const canSubmit =
    !pending && !uploading && (mode !== "file" ? !needsText || text.trim().length > 0 : file != null);

  async function submit() {
    setError(null);
    if (mode === "file") {
      if (!file) return;
      setUploading(true);
      try {
        const res = await api.uploadAttachment(projectId, file, "requisite");
        onProvide({ mode, attachment_id: res.attachment_id, note: file.name });
        setFile(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Не удалось загрузить файл");
        return;
      } finally {
        setUploading(false);
      }
    } else if (mode === "value" || mode === "assumption") {
      onProvide({ mode, value: text });
    } else if (mode === "reference") {
      onProvide({ mode, note: text });
    } else {
      onProvide({ mode });
    }
    setText("");
    setEditing(false);
  }

  return (
    <li className="requisite-card">
      <div className="requisite-card__head">
        <span className="requisites__item-title">
          {item.title}
          {kindLabel ? <span className="requisites__item-kind"> · {kindLabel}</span> : null}
          {item.blocking ? (
            <span className="requisites__item-blocking"> · ждёт задача-потребитель</span>
          ) : null}
        </span>
        {provided && !editing ? (
          <span className="requisite-card__provided">
            <StatusPill tone={providedTone(item.provided_mode || "value")}>
              {PROVIDED_LABELS[item.provided_mode || "value"] || "Получено"}
            </StatusPill>
            <button type="button" className="btn btn--ghost" onClick={() => setEditing(true)}>
              Изменить
            </button>
          </span>
        ) : null}
      </div>

      {provided && !editing && (item.provided_value || item.provided_note) ? (
        <p className="requisite-card__detail">{item.provided_value || item.provided_note}</p>
      ) : null}

      {showForm ? (
        <div className="requisite-card__form">
          <div className="requisite-card__modes">
            {modesFor(item.kind).map((m) => (
              <button
                key={m}
                type="button"
                className={`requisite-mode${mode === m ? " requisite-mode--active" : ""}`}
                onClick={() => setMode(m)}
              >
                {item.kind === "credential" && m === "reference" ? "Выдано вне системы" : MODE_LABELS[m]}
              </button>
            ))}
          </div>

          {mode === "value" ? (
            <textarea
              className="requisite-card__input"
              rows={3}
              placeholder={hint || "Введите значение"}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          ) : null}
          {mode === "file" ? (
            <div className="requisite-card__file">
              <input
                type="file"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              {hint ? <p className="requisite-card__note">{hint}</p> : null}
            </div>
          ) : null}
          {mode === "assumption" ? (
            <textarea
              className="requisite-card__input"
              rows={2}
              placeholder="Рабочее допущение — его можно будет переопределить позже"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          ) : null}
          {mode === "reference" ? (
            <input
              className="requisite-card__input"
              placeholder={
                item.kind === "credential"
                  ? "Пометка: доступ выдан вне системы (без секрета)"
                  : "Ссылка на ресурс или пометка «выдано вне системы»"
              }
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          ) : null}
          {mode === "deferred" ? (
            <p className="requisite-card__note">Отметить, что данные будут позже — блок задачи снимется, можно вернуться.</p>
          ) : null}
          {mode === "not_applicable" ? (
            <p className="requisite-card__note">Отметить, что реквизит не нужен для этого проекта.</p>
          ) : null}

          {error ? <p className="requisite-card__error">{error}</p> : null}
          <div className="requisite-card__actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              {uploading
                ? "Загрузка…"
                : mode === "deferred" || mode === "not_applicable"
                  ? "Отметить"
                  : "Предоставить"}
            </button>
            {editing ? (
              <button type="button" className="btn btn--ghost" onClick={() => setEditing(false)}>
                Отмена
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
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
    mutationFn: ({ key, payload }: { key: string; payload: ProvidePayload }) =>
      api.provideRequisite(projectId, { key, ...payload }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["project", projectId, "requisites"] });
      // Граф/лента: предоставление снимает блок задачи-потребителя.
      void qc.invalidateQueries({ queryKey: ["project", projectId, "task_graph"] });
      void qc.invalidateQueries({ queryKey: ["project", projectId, "situation"] });
    },
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
      subtitle="Конкретные данные под реализацию. Вид — лишь подсказка: выберите удобный способ (значение, ссылка/выдано) или обойдите (допущение / позже / неприменимо)."
    >
      <div className="requisites">
        {groupByNeededFor(data.items).map(([neededFor, items]) => (
          <div key={neededFor} className="requisites__group">
            <p className="requisites__group-title">Для: {neededFor}</p>
            <ul className="requisites__list">
              {items.map((item) => (
                <RequisiteRow
                  key={item.key || `${neededFor}:${item.title}`}
                  projectId={projectId}
                  item={item}
                  pending={provide.isPending}
                  onProvide={(payload) =>
                    provide.mutate({ key: item.key || item.title, payload })
                  }
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
