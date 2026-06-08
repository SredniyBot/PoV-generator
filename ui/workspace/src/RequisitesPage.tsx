/**
 * Раздел «Реквизиты» — что нужно от пользователя под реализацию.
 *
 * Дизайн согласован с карточкой решения (DecisionCard): реквизит — это вопрос к
 * пользователю, и выглядит так же. Принципы:
 * - Система сама выводит форму ответа из вида реквизита (input_kind) — пользователь
 *   НЕ выбирает «режим». Одна естественная форма на карточку: значение / файл /
 *   подтверждение доступа. Кросс-affordance (приложить файл ↔ ввести текстом) —
 *   один тихий переключатель на случай неверной догадки.
 * - Обход (позже / допущение / неприменимо) спрятан за тихим «не могу
 *   предоставить», не как равноправные кнопки.
 * - Конкретность: показываем «зачем» (why) и «пример» (example). Расплывчатые
 *   предпосылки реализуемости вынесены в отдельный мягкий блок (advisory) — это
 *   условия, а не запросы «дай сейчас».
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Paperclip, Upload } from "lucide-react";

import { api } from "./api";
import type { RequisiteItemView } from "./types";
import { Button, EmptyState, LoadingPanel, SectionCard, cx } from "./ui";

type ProvideMode = "value" | "file" | "reference" | "assumption" | "deferred" | "not_applicable";

interface ProvidePayload {
  mode: ProvideMode;
  value?: string;
  note?: string;
  attachment_id?: string;
}

const KIND_LABELS: Record<string, string> = {
  credential: "доступ",
  dataset: "данные",
  file: "файл",
  setting: "настройка",
  interface_format: "формат",
  sample: "образец",
};

const PROVIDED_LABELS: Record<string, string> = {
  value: "Значение",
  file: "Файл",
  reference: "Доступ выдан",
  assumption: "Допущение",
  deferred: "Отложено",
  not_applicable: "Неприменимо",
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

function RequisiteCard({
  projectId,
  item,
  onProvide,
  onUnprovide,
  pending,
}: {
  projectId: string;
  item: RequisiteItemView;
  onProvide: (payload: ProvidePayload) => void;
  onUnprovide: () => void;
  pending: boolean;
}) {
  const provided = item.status === "provided";
  const kindLabel = item.kind ? KIND_LABELS[item.kind] : undefined;
  const baseMode: "text" | "file" | "access" =
    item.input_kind === "access" ? "access" : item.input_kind === "file" ? "file" : "text";

  const [editing, setEditing] = useState(false);
  const [altMode, setAltMode] = useState<"text" | "file" | null>(null);
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [escapeOpen, setEscapeOpen] = useState(false);
  const [assumeOpen, setAssumeOpen] = useState(false);
  const [assumeDraft, setAssumeDraft] = useState("");

  const mode = baseMode === "access" ? "access" : (altMode ?? baseMode);
  const showForm = !provided || editing;

  function reset() {
    setEditing(false);
    setAltMode(null);
    setDraft("");
    setNote("");
    setFile(null);
    setError(null);
    setEscapeOpen(false);
    setAssumeOpen(false);
    setAssumeDraft("");
  }

  async function submitFile() {
    if (!file) return;
    setError(null);
    setUploading(true);
    try {
      const res = await api.uploadAttachment(projectId, file, "requisite");
      onProvide({ mode: "file", attachment_id: res.attachment_id, note: file.name });
      reset();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось загрузить файл");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div
      className={cx(
        "decision-card",
        showForm && "decision-card--interactive",
        provided && !editing && "decision-card--answered",
      )}
    >
      <header className="decision-card__head">
        <div className="decision-card__head-text">
          <h3 className="decision-card__title">
            {kindLabel ? <span className="decision-card__section-tag">{kindLabel}</span> : null}
            <span>{item.title}</span>
          </h3>
        </div>
        {item.blocking && !provided ? (
          <span className="requisite-need" title="Без этого задача-потребитель не соберётся">
            нужно для сборки
          </span>
        ) : null}
      </header>

      <div className="decision-card__body">
        {item.why ? <p className="decision-card__description">{item.why}</p> : null}

        {provided && !editing ? (
          <div className="decision-card__answer">
            <div className="decision-card__answer-head">
              <span className="decision-card__answer-value">
                {PROVIDED_LABELS[item.provided_mode || "value"] || "Получено"}
              </span>
            </div>
            {item.provided_value || item.provided_note ? (
              <p className="decision-card__answer-desc">{item.provided_value || item.provided_note}</p>
            ) : null}
            <div className="requisite-quiet-row">
              <button type="button" className="requisite-link" onClick={() => setEditing(true)}>
                Изменить
              </button>
              <button type="button" className="requisite-link" disabled={pending} onClick={onUnprovide}>
                Отменить
              </button>
            </div>
          </div>
        ) : null}

        {showForm ? (
          <div className="requisite-form">
            {mode === "access" ? (
              <>
                <p className="requisite-hint">
                  Не вводите секрет — подтвердите, что доступ выдан вне системы (Vault, почта,
                  отдельный канал){item.example ? `. ${item.example}` : ""}.
                </p>
                <input
                  className="decision-card__free-input"
                  placeholder="Где/как выдан — необязательно"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
                <div className="requisite-form__actions">
                  <Button
                    tone="primary"
                    disabled={pending}
                    onClick={() => {
                      onProvide({ mode: "reference", note });
                      reset();
                    }}
                  >
                    Доступ выдан
                  </Button>
                </div>
              </>
            ) : mode === "file" ? (
              <>
                {item.example ? <p className="requisite-hint">Пример: {item.example}</p> : null}
                <label className={cx("requisite-filedrop", file && "requisite-filedrop--filled")}>
                  <input
                    type="file"
                    className="requisite-filedrop__input"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  />
                  {file ? <Check size={16} /> : <Upload size={16} />}
                  <span className="requisite-filedrop__text">{file ? file.name : "Выбрать файл"}</span>
                </label>
                {error ? <p className="requisite-error">{error}</p> : null}
                <div className="requisite-form__actions">
                  <Button tone="primary" disabled={pending || uploading || !file} onClick={() => void submitFile()}>
                    {uploading ? "Загрузка…" : "Предоставить"}
                  </Button>
                  <button type="button" className="requisite-link" onClick={() => setAltMode("text")}>
                    ввести текстом
                  </button>
                </div>
              </>
            ) : (
              <>
                <textarea
                  className="decision-card__free-input"
                  rows={3}
                  placeholder={item.example ? `Например: ${item.example}` : "Введите или вставьте значение"}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                />
                <div className="requisite-form__actions">
                  <Button
                    tone="primary"
                    disabled={pending || !draft.trim()}
                    onClick={() => {
                      onProvide({ mode: "value", value: draft });
                      reset();
                    }}
                  >
                    Предоставить
                  </Button>
                  <button
                    type="button"
                    className="requisite-link requisite-link--icon"
                    onClick={() => setAltMode("file")}
                  >
                    <Paperclip size={13} /> приложить файл
                  </button>
                </div>
              </>
            )}

            <div className="requisite-escape">
              {!escapeOpen ? (
                <button type="button" className="requisite-link" onClick={() => setEscapeOpen(true)}>
                  Не могу предоставить →
                </button>
              ) : assumeOpen ? (
                <>
                  <textarea
                    className="decision-card__free-input"
                    rows={2}
                    placeholder="Рабочее допущение — его можно будет переопределить"
                    value={assumeDraft}
                    onChange={(e) => setAssumeDraft(e.target.value)}
                  />
                  <div className="requisite-form__actions">
                    <Button
                      tone="primary"
                      disabled={pending || !assumeDraft.trim()}
                      onClick={() => {
                        onProvide({ mode: "assumption", value: assumeDraft });
                        reset();
                      }}
                    >
                      Принять допущение
                    </Button>
                    <button type="button" className="requisite-link" onClick={() => setAssumeOpen(false)}>
                      назад
                    </button>
                  </div>
                </>
              ) : (
                <div className="requisite-escape__opts">
                  {item.kind !== "credential" ? (
                    <button
                      type="button"
                      className="decision-card__free-skip"
                      onClick={() => setAssumeOpen(true)}
                    >
                      Принять допущение
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="decision-card__free-skip"
                    onClick={() => {
                      onProvide({ mode: "deferred" });
                      reset();
                    }}
                  >
                    Позже
                  </button>
                  <button
                    type="button"
                    className="decision-card__free-skip"
                    onClick={() => {
                      onProvide({ mode: "not_applicable" });
                      reset();
                    }}
                  >
                    Неприменимо
                  </button>
                </div>
              )}
              {editing ? (
                <button type="button" className="requisite-link" onClick={reset}>
                  Отмена
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function useRequisites(projectId: string) {
  return useQuery({
    queryKey: ["project", projectId, "requisites"],
    queryFn: () => api.getRequisites(projectId),
  });
}

function RequisitesSection({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const query = useRequisites(projectId);
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["project", projectId, "requisites"] });
    void qc.invalidateQueries({ queryKey: ["project", projectId, "task_graph"] });
    void qc.invalidateQueries({ queryKey: ["project", projectId, "situation"] });
  };
  const provide = useMutation({
    mutationFn: ({ key, payload }: { key: string; payload: ProvidePayload }) =>
      api.provideRequisite(projectId, { key, ...payload }),
    onSuccess: invalidate,
  });
  const unprovide = useMutation({
    mutationFn: (key: string) => api.unprovideRequisite(projectId, key),
    onSuccess: invalidate,
  });
  const pending = provide.isPending || unprovide.isPending;

  if (query.isLoading || !query.data) return <LoadingPanel title="Загрузка реквизитов…" />;
  const data = query.data;

  if (data.status === "missing") {
    return (
      <SectionCard title="Реквизиты">
        <EmptyState
          title="Пока нечего предоставлять"
          description="Список появится после оценки реализуемости и модели компонентов — они определяют, какие конкретные данные нужны от вас."
        />
      </SectionCard>
    );
  }
  if (data.items.length === 0) {
    return (
      <SectionCard title="Реквизиты">
        <EmptyState
          title="Конкретных запросов данных пока нет"
          description="Они появятся на этапе архитектуры, когда модель компонентов определит, что именно нужно. Ниже — предварительные предпосылки реализуемости."
        />
      </SectionCard>
    );
  }

  return (
    <SectionCard
      title="Реквизиты"
      subtitle="Конкретные данные под реализацию. Заполните удобным способом — система предлагает форму, но можно приложить файл или обойти (позже / допущение / неприменимо)."
    >
      <div className="requisites">
        {groupByNeededFor(data.items).map(([neededFor, items]) => (
          <div key={neededFor} className="requisites__group">
            <p className="requisites__group-title">Для: {neededFor}</p>
            <div className="requisites__cards">
              {items.map((item) => (
                <RequisiteCard
                  key={item.key || `${neededFor}:${item.title}`}
                  projectId={projectId}
                  item={item}
                  pending={pending}
                  onProvide={(payload) => provide.mutate({ key: item.key || item.title, payload })}
                  onUnprovide={() => unprovide.mutate(item.key || item.title)}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </SectionCard>
  );
}

function AdvisorySection({ projectId }: { projectId: string }) {
  const query = useRequisites(projectId);
  const advisory = query.data?.advisory ?? [];
  if (advisory.length === 0) return null;
  return (
    <SectionCard
      title="Предпосылки реализуемости"
      subtitle="Условия, при которых части проекта реализуемы (доступы, согласования, компетенции). Это ранние подсказки — конкретные запросы данных появятся выше, на этапе архитектуры."
    >
      <ul className="requisite-advisory">
        {advisory.map((item) => (
          <li key={item.key || item.title} className="requisite-advisory__item">
            <span className="requisite-advisory__title">{item.title}</span>
            {item.needed_for && item.needed_for !== "проект" ? (
              <span className="requisite-advisory__for"> · {item.needed_for}</span>
            ) : null}
          </li>
        ))}
      </ul>
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
      <ul className="requisite-advisory">
        {data.items.map((gap) => (
          <li key={gap.title} className="requisite-advisory__item">
            <span className="requisite-advisory__title">{gap.title}</span>
            {gap.reason ? <span className="requisite-advisory__for"> · {gap.reason}</span> : null}
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
      <AdvisorySection projectId={projectId} />
      <GapsSection projectId={projectId} />
    </div>
  );
}
