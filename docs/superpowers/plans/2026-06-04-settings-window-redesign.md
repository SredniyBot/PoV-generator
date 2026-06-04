# Settings Window Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Привести окно настроек LLM к продуктовому уровню: Modal-формы, Toast-уведомления, skeleton-загрузки, error states, collapsible ProviderRow, inline delete confirm, поиск по моделям, optimistic updates в Assignments, tab badges.

**Architecture:** Все изменения в двух файлах: `LlmSettingsPage.tsx` (переработка, новые компоненты-примитивы прямо в файле) и `styles.css` (новые CSS-классы в конце settings-секции). API, типы и роутинг не трогаем.

**Tech Stack:** React 18, TypeScript 5.8, @tanstack/react-query v5, lucide-react, CSS custom properties (тёмная тема проекта). Верификация: `npm run build` (tsc --noEmit + vite build).

**Spec:** `docs/superpowers/specs/2026-06-04-settings-window-redesign.md`

---

## File Map

| Файл | Изменение |
|------|-----------|
| `ui/workspace/src/styles.css` | +~130 строк после строки 4841 |
| `ui/workspace/src/LlmSettingsPage.tsx` | Полная переработка (~1000 строк) |

---

## Task 1: CSS — новые классы

**Files:**
- Modify: `ui/workspace/src/styles.css` (добавить в конец файла)

- [ ] **Step 1: Добавить все новые CSS-классы в конец `styles.css`**

Открыть `ui/workspace/src/styles.css` и дописать в самый конец:

```css
/* ── Settings redesign additions ─────────────────────────────────────────── */

/* Shimmer skeleton */
@keyframes shimmer {
  from { background-position: -200% 0; }
  to   { background-position:  200% 0; }
}

.sk-row {
  height: 72px;
  border-radius: 12px;
  background: linear-gradient(
    90deg,
    rgba(255, 255, 255, 0.04) 25%,
    rgba(255, 255, 255, 0.08) 50%,
    rgba(255, 255, 255, 0.04) 75%
  );
  background-size: 200% 100%;
  animation: shimmer 1.4s ease infinite;
}

/* Error state */
.llm-settings__error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 32px 24px;
  border: 1px dashed var(--border-subtle);
  border-radius: 12px;
  color: var(--text-secondary);
  text-align: center;
}

.llm-settings__error p {
  margin: 0;
  max-width: 400px;
}

/* Empty state additions */
.llm-settings__empty-icon {
  font-size: 28px;
  margin: 0;
}

.llm-settings__empty-steps {
  margin: 8px 0 0 0;
  padding-left: 20px;
  text-align: left;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.8;
}

/* Modal */
.settings-modal {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
}

.settings-modal__card {
  width: 100%;
  max-width: 480px;
  background: var(--surface-secondary, #1c222b);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 16px;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.5);
}

.settings-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.settings-modal__header h3 {
  margin: 0;
  font-size: 16px;
  color: var(--text-primary);
}

.settings-modal__close {
  padding: 4px 8px;
  font-size: 16px;
  color: var(--text-secondary);
  border-radius: 8px;
}

.settings-modal__close:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-primary);
}

/* Toast */
.toast-container {
  position: fixed;
  bottom: 16px;
  right: 16px;
  z-index: 2000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}

.toast {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-radius: 10px;
  font-size: 14px;
  max-width: 360px;
  pointer-events: all;
  animation: toast-in 0.2s ease;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}

@keyframes toast-in {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

.toast--success {
  background: rgba(120, 200, 140, 0.15);
  border: 1px solid rgba(120, 200, 140, 0.3);
  color: #a0d8b0;
}

.toast--error {
  background: rgba(220, 100, 100, 0.15);
  border: 1px solid rgba(220, 100, 100, 0.3);
  color: #e8a0a0;
}

.toast__close {
  flex-shrink: 0;
  padding: 2px 6px;
  border-radius: 6px;
  opacity: 0.7;
  font-size: 14px;
}

.toast__close:hover {
  opacity: 1;
  background: rgba(255, 255, 255, 0.08);
}

/* Tab badge */
.llm-settings__tab {
  position: relative;
}

.llm-tab__badge {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #d4a84b;
  margin-left: 6px;
  vertical-align: middle;
  flex-shrink: 0;
}

/* ProviderRow restructure */
.llm-row__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.llm-row__title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.llm-row__row-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.llm-row__expand-btn {
  padding: 4px 6px;
  border-radius: 8px;
  color: var(--text-secondary);
}

.llm-row__expand-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-primary);
}

.llm-row__details {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.llm-row__details-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

/* Inline delete confirm */
.llm-row__delete-confirm {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 12px;
  padding: 2px 0;
  color: var(--text-primary);
  font-size: 14px;
}

.llm-row__delete-confirm-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

/* Search */
.llm-search-wrap {
  position: relative;
  flex: 1;
  max-width: 320px;
}

.llm-search__icon {
  position: absolute;
  left: 10px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-tertiary);
  pointer-events: none;
}

.llm-search {
  width: 100%;
  padding: 8px 12px 8px 32px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  transition: border-color 180ms ease, box-shadow 180ms ease;
}

.llm-search:focus {
  border-color: rgba(120, 184, 201, 0.34);
  box-shadow: var(--focus-ring);
  background: rgba(255, 255, 255, 0.045);
}

.llm-search::-webkit-search-cancel-button {
  cursor: pointer;
}

/* Concurrency select in details (reuse llm-form select styles) */
.llm-row__concurrency-select {
  padding: 6px 36px 6px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  background-image:
    linear-gradient(45deg, transparent 50%, rgba(215, 221, 227, 0.88) 50%),
    linear-gradient(135deg, rgba(215, 221, 227, 0.88) 50%, transparent 50%);
  background-position:
    calc(100% - 16px) calc(50% - 3px),
    calc(100% - 10px) calc(50% - 3px);
  background-size: 6px 6px, 6px 6px;
  background-repeat: no-repeat;
  cursor: pointer;
  transition: border-color 180ms ease;
}

.llm-row__concurrency-select:focus {
  border-color: rgba(120, 184, 201, 0.34);
}
```

- [ ] **Step 2: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in` без ошибок.

- [ ] **Step 3: Commit**

```bash
git add ui/workspace/src/styles.css
git commit -m "style(settings): skeleton, modal, toast, collapsible row, search, tab badge"
```

---

## Task 2: Примитивы — SettingsModal, DeleteConfirm, SkeletonList, ErrorState

**Files:**
- Modify: `ui/workspace/src/LlmSettingsPage.tsx` (добавить блок примитивов в начало файла после импортов)

- [ ] **Step 1: Обновить импорты в `LlmSettingsPage.tsx`**

Заменить весь блок импортов (строки 1–33) на:

```tsx
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  XCircle,
} from "lucide-react";

import { api } from "./api";
import type {
  ModelCatalogEntry,
  ProviderConnectionView,
  ProviderType,
  TestResultView,
} from "./types";
```

- [ ] **Step 2: Добавить Toast-систему после импортов (перед `type TabKey`)**

```tsx
// ── Toast ─────────────────────────────────────────────────────────────────────

type ToastType = "success" | "error";
interface ToastItem { id: number; type: ToastType; message: string; }

const ToastCtx = createContext<(t: Omit<ToastItem, "id">) => void>(() => {});

function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const counter = useRef(0);

  const add = useCallback((t: Omit<ToastItem, "id">) => {
    const id = counter.current++;
    setToasts((prev) => [...prev.slice(-2), { ...t, id }]);
    setTimeout(
      () => setToasts((prev) => prev.filter((x) => x.id !== id)),
      t.type === "error" ? 6000 : 4000,
    );
  }, []);

  return (
    <ToastCtx.Provider value={add}>
      {children}
      <div className="toast-container">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast--${t.type}`}>
            <span>{t.message}</span>
            <button
              type="button"
              className="toast__close btn-inline"
              onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

function useToast() {
  return useContext(ToastCtx);
}
```

- [ ] **Step 3: Добавить SettingsModal после Toast-блока**

```tsx
// ── Modal ──────────────────────────────────────────────────────────────────────

function SettingsModal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [onClose]);

  return createPortal(
    <div className="settings-modal" onClick={onClose}>
      <div className="settings-modal__card" onClick={(e) => e.stopPropagation()}>
        <div className="settings-modal__header">
          <h3>{title}</h3>
          <button type="button" className="btn-inline settings-modal__close" onClick={onClose}>
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
```

- [ ] **Step 4: Добавить DeleteConfirm, SkeletonList, ErrorState после SettingsModal**

```tsx
// ── DeleteConfirm ─────────────────────────────────────────────────────────────

function DeleteConfirm({
  name,
  onConfirm,
  onCancel,
}: {
  name: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="llm-row__delete-confirm">
      <span>Удалить «{name}»?</span>
      <div className="llm-row__delete-confirm-actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>
          Отмена
        </button>
        <button type="button" className="btn btn--ghost btn--danger" onClick={onConfirm}>
          Удалить
        </button>
      </div>
    </div>
  );
}

// ── Skeleton + Error ──────────────────────────────────────────────────────────

function SkeletonList() {
  return (
    <div className="llm-settings__list">
      <div className="sk-row" aria-hidden />
      <div className="sk-row" aria-hidden />
      <div className="sk-row" aria-hidden />
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="llm-settings__error">
      <AlertCircle size={20} />
      <p>{message}</p>
      <button type="button" className="btn btn--ghost" onClick={onRetry}>
        Повторить
      </button>
    </div>
  );
}
```

- [ ] **Step 5: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in` без ошибок TypeScript.

- [ ] **Step 6: Commit**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx
git commit -m "feat(settings): add SettingsModal, DeleteConfirm, SkeletonList, ErrorState, Toast primitives"
```

---

## Task 3: LlmSettingsPage и TabButton — tab badges + ToastProvider

**Files:**
- Modify: `ui/workspace/src/LlmSettingsPage.tsx` (заменить `type TabKey`, `LlmSettingsPage`, `TabButton`)

- [ ] **Step 1: Заменить `type TabKey = ...` и функции `LlmSettingsPage` и `TabButton`**

Найти блок (строки 35–87 оригинала, теперь сдвинутые после добавления примитивов) и заменить:

```tsx
type TabKey = "providers" | "models" | "assignments";

export function LlmSettingsPage() {
  const [tab, setTab] = useState<TabKey>("providers");

  const providersQuery = useQuery({
    queryKey: ["llm-settings", "providers"],
    queryFn: () => api.listProviders(),
  });
  const assignmentsQuery = useQuery({
    queryKey: ["llm-settings", "assignments"],
    queryFn: () => api.listAssignments(),
  });
  const modelsQuery = useQuery({
    queryKey: ["llm-settings", "models"],
    queryFn: () => api.listModels(),
  });

  const untestedCount = (providersQuery.data ?? []).filter(
    (p) => p.last_test_status !== "ok",
  ).length;
  const availableModels = (modelsQuery.data ?? []).map((m) => m.model_name);
  const missingCount = (assignmentsQuery.data ?? []).filter(
    (a) => a.model_name && !availableModels.includes(a.model_name),
  ).length;

  return (
    <ToastProvider>
      <div className="llm-settings">
        <header className="llm-settings__header">
          <h1>Настройки LLM</h1>
        </header>

        <div className="llm-settings__tabs">
          <TabButton
            active={tab === "providers"}
            onClick={() => setTab("providers")}
            badge={untestedCount > 0}
          >
            Источники
          </TabButton>
          <TabButton active={tab === "models"} onClick={() => setTab("models")}>
            Модели
          </TabButton>
          <TabButton
            active={tab === "assignments"}
            onClick={() => setTab("assignments")}
            badge={missingCount > 0}
          >
            Назначения
          </TabButton>
        </div>

        <div className="llm-settings__body">
          {tab === "providers" ? <ProvidersTab /> : null}
          {tab === "models" ? <ModelsTab /> : null}
          {tab === "assignments" ? <AssignmentsTab /> : null}
        </div>
      </div>
    </ToastProvider>
  );
}


function TabButton({
  active,
  onClick,
  badge,
  children,
}: {
  active: boolean;
  onClick: () => void;
  badge?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={active ? "llm-settings__tab llm-settings__tab--active" : "llm-settings__tab"}
      onClick={onClick}
    >
      {children}
      {badge ? <span className="llm-tab__badge" aria-label="требует внимания" /> : null}
    </button>
  );
}
```

- [ ] **Step 2: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in`.

- [ ] **Step 3: Commit**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx
git commit -m "feat(settings): tab badges for untested providers and missing models"
```

---

## Task 4: ProviderRow — collapsible, inline delete, self-contained mutations

**Files:**
- Modify: `ui/workspace/src/LlmSettingsPage.tsx` (заменить константу `AUTO_CONCURRENCY` и функцию `ProviderRow`)

- [ ] **Step 1: Заменить `AUTO_CONCURRENCY` и `ProviderRow`**

Найти и заменить блок от `const AUTO_CONCURRENCY` до конца `ProviderRow` (включительно):

```tsx
const AUTO_CONCURRENCY: Record<string, number> = {
  anthropic: 5,
  claude_cli: 2,
  openrouter: 5,
};

function ProviderRow({
  provider,
  onDelete,
}: {
  provider: ProviderConnectionView;
  onDelete: () => void;
}) {
  const toast = useToast();
  const qc = useQueryClient();

  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [concurrency, setConcurrency] = useState(provider.extras.max_concurrency ?? "");
  const [lastTestResult, setLastTestResult] = useState<TestResultView | null>(null);
  const [lastSyncResult, setLastSyncResult] = useState<{
    added_count: number;
    added_models: string[];
  } | null>(null);

  const testMutation = useMutation({
    mutationFn: () => api.testProvider(provider.connection_id),
    onSuccess: (result) => {
      setLastTestResult(result);
      qc.invalidateQueries({ queryKey: ["llm-settings", "providers"] });
      if (result.status === "ok") {
        toast({ type: "success", message: `Соединение работает · ${result.latency_ms} ms` });
      } else {
        toast({ type: "error", message: `Ошибка: ${result.message}` });
      }
    },
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка теста" }),
  });

  const syncMutation = useMutation({
    mutationFn: () => api.syncKnownModels(provider.connection_id),
    onSuccess: (result) => {
      setLastSyncResult(result);
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
      toast({
        type: "success",
        message:
          result.added_count === 0
            ? "Каталог актуален"
            : `Добавлено ${result.added_count} модель(-и)`,
      });
    },
    onError: (e) =>
      toast({
        type: "error",
        message: e instanceof Error ? e.message : "Ошибка синхронизации",
      }),
  });

  const concurrencyMutation = useMutation({
    mutationFn: (value: string) =>
      api.updateProvider(provider.connection_id, {
        extras: { ...provider.extras, max_concurrency: value },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "providers"] }),
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Не удалось сохранить" }),
  });

  if (confirmDelete) {
    return (
      <li className="llm-row">
        <DeleteConfirm
          name={provider.display_name}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={onDelete}
        />
      </li>
    );
  }

  const statusBadge = (() => {
    if (provider.last_test_status === "ok") {
      return (
        <span className="llm-badge llm-badge--ok">
          <CheckCircle2 size={12} /> работает
        </span>
      );
    }
    if (provider.last_test_status === "error") {
      return (
        <span className="llm-badge llm-badge--err">
          <XCircle size={12} /> ошибка
        </span>
      );
    }
    return <span className="llm-badge llm-badge--neutral">не протестирован</span>;
  })();

  const autoDefault = AUTO_CONCURRENCY[provider.provider_type] ?? 3;

  return (
    <li className="llm-row">
      <div className="llm-row__main">
        <div className="llm-row__top">
          <div className="llm-row__title-group">
            <strong>{provider.display_name}</strong>
            {statusBadge}
          </div>
          <div className="llm-row__row-actions">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => testMutation.mutate()}
              disabled={testMutation.isPending}
            >
              {testMutation.isPending ? (
                <Loader2 size={14} className="spin" />
              ) : (
                <RefreshCw size={14} />
              )}{" "}
              Проверить
            </button>
            <button
              type="button"
              className="btn-inline llm-row__expand-btn"
              onClick={() => setExpanded((e) => !e)}
              aria-label={expanded ? "Свернуть" : "Развернуть"}
            >
              {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
            </button>
            <button
              type="button"
              className="btn-inline"
              onClick={() => setConfirmDelete(true)}
              title="Удалить"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </div>

        <span className="llm-row__sub">
          {humanProviderType(provider.provider_type)}
          {provider.api_key_preview ? ` · ключ ${provider.api_key_preview}` : null}
          {provider.source === "env_bootstrap" ? (
            <span className="llm-badge llm-badge--neutral"> из .env</span>
          ) : null}
        </span>

        {expanded ? (
          <div className="llm-row__details">
            <div className="llm-row__details-row">
              <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: 8, margin: 0 }}>
                <span style={{ fontSize: 12, color: "var(--text-tertiary)", whiteSpace: "nowrap" }}>
                  Параллельных шагов
                </span>
                <select
                  className="llm-row__concurrency-select"
                  value={concurrency}
                  onChange={(e) => {
                    setConcurrency(e.target.value);
                    concurrencyMutation.mutate(e.target.value);
                  }}
                >
                  <option value="">авто ({autoDefault})</option>
                  {Array.from({ length: 16 }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={String(n)}>
                      {n}
                    </option>
                  ))}
                </select>
                {concurrencyMutation.isPending ? <Loader2 size={13} className="spin" /> : null}
              </label>

              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => syncMutation.mutate()}
                disabled={syncMutation.isPending}
                title="Добавить routings для известных моделей провайдера"
              >
                {syncMutation.isPending ? (
                  <Loader2 size={14} className="spin" />
                ) : (
                  <RefreshCw size={14} />
                )}{" "}
                Обновить каталог
              </button>
            </div>

            {lastTestResult ? (
              <p
                className={
                  lastTestResult.status === "ok"
                    ? "llm-row__hint llm-row__hint--ok"
                    : "llm-row__hint llm-row__hint--err"
                }
              >
                {lastTestResult.message}
                {lastTestResult.latency_ms ? ` · ${lastTestResult.latency_ms} ms` : null}
                {lastTestResult.sample_response
                  ? ` · ответ: "${lastTestResult.sample_response}"`
                  : null}
              </p>
            ) : provider.last_test_message ? (
              <p className="llm-row__hint">{provider.last_test_message}</p>
            ) : null}

            {lastSyncResult ? (
              <p className="llm-row__hint llm-row__hint--ok">
                {lastSyncResult.added_count === 0
                  ? "Каталог моделей уже актуален — новых записей нет."
                  : `Добавлено ${lastSyncResult.added_count}: ${lastSyncResult.added_models.join(", ")}.`}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}
```

- [ ] **Step 2: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in`.

- [ ] **Step 3: Commit**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx
git commit -m "feat(settings): ProviderRow collapsible details, inline delete confirm, self-contained mutations"
```

---

## Task 5: ProvidersTab — Modal форма, skeleton, error state

**Files:**
- Modify: `ui/workspace/src/LlmSettingsPage.tsx` (заменить `ProvidersTab` и `NewProviderForm`)

- [ ] **Step 1: Заменить `ProvidersTab`**

Найти функцию `ProvidersTab` (от `function ProvidersTab()` до конца закрывающей `}`) и заменить:

```tsx
function ProvidersTab() {
  const qc = useQueryClient();
  const toast = useToast();
  const [showModal, setShowModal] = useState(false);

  const providersQuery = useQuery({
    queryKey: ["llm-settings", "providers"],
    queryFn: () => api.listProviders(),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteProvider(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-settings", "providers"] });
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
      toast({ type: "success", message: "Подключение удалено" });
    },
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка удаления" }),
  });

  if (providersQuery.isLoading) return <SkeletonList />;
  if (providersQuery.isError)
    return (
      <ErrorState
        message="Не удалось загрузить подключения"
        onRetry={() => providersQuery.refetch()}
      />
    );

  const providers = providersQuery.data ?? [];

  return (
    <div>
      <div className="llm-settings__row-head">
        <button type="button" className="btn btn--primary" onClick={() => setShowModal(true)}>
          <Plus size={14} /> Подключить источник
        </button>
      </div>

      {providers.length === 0 ? (
        <div className="llm-settings__empty">
          <p className="llm-settings__empty-icon">🔌</p>
          <p>
            <strong>Нет подключений</strong>
          </p>
          <p>Добавьте хотя бы один источник — без него workflow не запустится.</p>
          <button type="button" className="btn btn--primary" onClick={() => setShowModal(true)}>
            <Plus size={14} /> Подключить источник
          </button>
        </div>
      ) : (
        <ul className="llm-settings__list">
          {providers.map((p) => (
            <ProviderRow
              key={p.connection_id}
              provider={p}
              onDelete={() => deleteMutation.mutate(p.connection_id)}
            />
          ))}
        </ul>
      )}

      {showModal ? (
        <SettingsModal title="Новый источник моделей" onClose={() => setShowModal(false)}>
          <NewProviderForm onClose={() => setShowModal(false)} />
        </SettingsModal>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: Обновить `NewProviderForm` — добавить toast на успех**

Найти `NewProviderForm` и заменить `onSuccess` в `createMutation`:

```tsx
function NewProviderForm({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [providerType, setProviderType] = useState<ProviderType>("anthropic");
  const [displayName, setDisplayName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");

  const createMutation = useMutation({
    mutationFn: () =>
      api.createProvider({
        provider_type: providerType,
        display_name: displayName,
        api_key: apiKey || undefined,
        extras: baseUrl ? { base_url: baseUrl } : undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-settings", "providers"] });
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
      toast({ type: "success", message: "Подключение добавлено" });
      onClose();
    },
  });

  const error = createMutation.error instanceof Error ? createMutation.error.message : null;

  return (
    <form
      className="llm-form"
      style={{ border: "none", padding: 0, marginTop: 0, background: "transparent" }}
      onSubmit={(e) => {
        e.preventDefault();
        createMutation.mutate();
      }}
    >
      <label>
        Тип
        <select
          value={providerType}
          onChange={(e) => setProviderType(e.target.value as ProviderType)}
        >
          <option value="anthropic">Anthropic API</option>
          <option value="openrouter">OpenRouter</option>
          <option value="claude_cli">Claude CLI</option>
        </select>
      </label>

      <label>
        Название
        <input
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="напр. Anthropic prod"
          required
        />
      </label>

      {providerType !== "claude_cli" ? (
        <label>
          API key
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={providerType === "anthropic" ? "sk-ant-…" : "sk-or-…"}
            autoComplete="off"
            required
          />
        </label>
      ) : null}

      {providerType === "openrouter" ? (
        <label>
          Base URL <span className="llm-form__optional">(опционально)</span>
          <input
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://openrouter.ai/api/v1"
          />
        </label>
      ) : null}

      {error ? <p className="llm-form__error">{error}</p> : null}

      <div className="llm-form__actions">
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Отмена
        </button>
        <button type="submit" className="btn btn--primary" disabled={createMutation.isPending}>
          {createMutation.isPending ? <Loader2 size={14} className="spin" /> : null} Сохранить
        </button>
      </div>
    </form>
  );
}
```

- [ ] **Step 3: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in`.

- [ ] **Step 4: Commit**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx
git commit -m "feat(settings): ProvidersTab — modal form, skeleton, error state, toast on delete"
```

---

## Task 6: ModelsTab — поиск, Modal форма, skeleton, error state, toast

**Files:**
- Modify: `ui/workspace/src/LlmSettingsPage.tsx` (заменить `ModelsTab` и `AddCustomModelForm`)

- [ ] **Step 1: Заменить `ModelsTab`**

Найти функцию `ModelsTab` (от `function ModelsTab()` до конца) и заменить:

```tsx
function ModelsTab() {
  const qc = useQueryClient();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const [showModal, setShowModal] = useState(false);

  const modelsQuery = useQuery({
    queryKey: ["llm-settings", "models"],
    queryFn: () => api.listModels(),
  });
  const providersQuery = useQuery({
    queryKey: ["llm-settings", "providers"],
    queryFn: () => api.listProviders(),
  });

  const testMutation = useMutation({
    mutationFn: (modelName: string) => api.testModel(modelName),
    onSuccess: (result, modelName) => {
      if (result.status === "ok") {
        toast({ type: "success", message: `${modelName} работает · ${result.latency_ms} ms` });
      } else {
        toast({ type: "error", message: `Ошибка теста: ${result.message}` });
      }
    },
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка теста" }),
  });

  const deleteRoutingMutation = useMutation({
    mutationFn: (routingId: string) => api.deleteRouting(routingId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
      toast({ type: "success", message: "Маршрут удалён" });
    },
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка удаления" }),
  });

  const reorderRoutingMutation = useMutation({
    mutationFn: async ({
      routingA,
      routingB,
    }: {
      routingA: { id: string; newPriority: number };
      routingB: { id: string; newPriority: number };
    }) => {
      await api.updateRouting(routingA.id, { priority: routingA.newPriority });
      await api.updateRouting(routingB.id, { priority: routingB.newPriority });
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "models"] }),
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка сортировки" }),
  });

  if (modelsQuery.isLoading) return <SkeletonList />;
  if (modelsQuery.isError)
    return (
      <ErrorState
        message="Не удалось загрузить каталог моделей"
        onRetry={() => modelsQuery.refetch()}
      />
    );

  const models = modelsQuery.data ?? [];
  const providers = providersQuery.data ?? [];
  const filtered = models.filter(
    (m) => !search || m.model_name.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div>
      <div className="llm-settings__row-head">
        <div className="llm-search-wrap">
          <Search size={14} className="llm-search__icon" />
          <input
            type="search"
            className="llm-search"
            placeholder="Поиск по имени модели…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => setShowModal(true)}
          disabled={providers.length === 0}
        >
          <Plus size={14} /> Добавить свою модель
        </button>
      </div>

      {models.length === 0 ? (
        <div className="llm-settings__empty">
          <p className="llm-settings__empty-icon">📦</p>
          <p>
            <strong>Каталог пуст</strong>
          </p>
          <ol className="llm-settings__empty-steps">
            <li>Перейдите на вкладку «Источники»</li>
            <li>Подключите провайдера</li>
            <li>Нажмите ↻ «Обновить каталог» в строке провайдера</li>
          </ol>
        </div>
      ) : filtered.length === 0 ? (
        <div className="llm-settings__empty">
          <p>Нет моделей по запросу «{search}»</p>
        </div>
      ) : (
        <ul className="llm-settings__list">
          {filtered.map((m) => (
            <ModelRow
              key={m.model_name}
              entry={m}
              onTest={() => testMutation.mutate(m.model_name)}
              testPending={testMutation.isPending && testMutation.variables === m.model_name}
              testResult={
                testMutation.data && testMutation.variables === m.model_name
                  ? testMutation.data
                  : null
              }
              onDeleteRouting={(routingId) => deleteRoutingMutation.mutate(routingId)}
              onMoveRouting={(routingIdToMove, direction) => {
                const list = m.routings;
                const idx = list.findIndex((r) => r.routing_id === routingIdToMove);
                const targetIdx = direction === "up" ? idx - 1 : idx + 1;
                if (idx < 0 || targetIdx < 0 || targetIdx >= list.length) return;
                const a = list[idx];
                const b = list[targetIdx];
                if (!a || !b) return;
                reorderRoutingMutation.mutate({
                  routingA: { id: a.routing_id, newPriority: b.priority },
                  routingB: { id: b.routing_id, newPriority: a.priority },
                });
              }}
              reorderPending={reorderRoutingMutation.isPending}
            />
          ))}
        </ul>
      )}

      {showModal ? (
        <SettingsModal title="Добавить кастомную модель" onClose={() => setShowModal(false)}>
          <AddCustomModelForm
            providers={providers}
            defaultConnectionId={providers[0]?.connection_id ?? ""}
            onClose={() => setShowModal(false)}
          />
        </SettingsModal>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: Обновить `AddCustomModelForm` — toast на успех, стили без border**

Найти `AddCustomModelForm` и заменить полностью:

```tsx
function AddCustomModelForm({
  providers,
  defaultConnectionId,
  onClose,
}: {
  providers: ProviderConnectionView[];
  defaultConnectionId: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const qc = useQueryClient();
  const [connectionId, setConnectionId] = useState(defaultConnectionId);
  const [modelName, setModelName] = useState("");

  const addMutation = useMutation({
    mutationFn: () => api.addCustomModel({ connection_id: connectionId, model_name: modelName }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
      toast({ type: "success", message: `Модель ${modelName} добавлена` });
      onClose();
    },
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка добавления" }),
  });

  return (
    <form
      className="llm-form"
      style={{ border: "none", padding: 0, marginTop: 0, background: "transparent" }}
      onSubmit={(e) => {
        e.preventDefault();
        addMutation.mutate();
      }}
    >
      <label>
        Источник
        <select value={connectionId} onChange={(e) => setConnectionId(e.target.value)}>
          {providers.map((p) => (
            <option key={p.connection_id} value={p.connection_id}>
              {p.display_name} ({humanProviderType(p.provider_type)})
            </option>
          ))}
        </select>
      </label>

      <label>
        Имя модели
        <input
          type="text"
          value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          placeholder="например, claude-opus-4-1 или mistralai/mixtral-8x7b"
          required
        />
      </label>

      <div className="llm-form__actions">
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          Отмена
        </button>
        <button type="submit" className="btn btn--primary" disabled={addMutation.isPending}>
          {addMutation.isPending ? <Loader2 size={14} className="spin" /> : null} Добавить
        </button>
      </div>
    </form>
  );
}
```

- [ ] **Step 3: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in`.

- [ ] **Step 4: Commit**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx
git commit -m "feat(settings): ModelsTab — search filter, modal form, skeleton, error state, toast"
```

---

## Task 7: AssignmentsTab — optimistic updates, skeleton, error state, onboarding empty state

**Files:**
- Modify: `ui/workspace/src/LlmSettingsPage.tsx` (заменить `AssignmentsTab`)

- [ ] **Step 1: Заменить `AssignmentsTab`**

Найти функцию `AssignmentsTab` (от `function AssignmentsTab()` до конца) и заменить:

```tsx
function AssignmentsTab() {
  const qc = useQueryClient();
  const toast = useToast();

  const purposesQuery = useQuery({
    queryKey: ["llm-settings", "purposes"],
    queryFn: () => api.listPurposes(),
  });
  const assignmentsQuery = useQuery({
    queryKey: ["llm-settings", "assignments"],
    queryFn: () => api.listAssignments(),
  });
  const modelsQuery = useQuery({
    queryKey: ["llm-settings", "models"],
    queryFn: () => api.listModels(),
  });
  const diagnosticsQuery = useQuery({
    queryKey: ["llm-settings", "diagnostics"],
    queryFn: () => api.getSettingsDiagnostics(),
  });

  // Optimistic local state — mirror of server assignments.
  const [localAssignments, setLocalAssignments] = useState<Record<string, string>>({});
  useEffect(() => {
    setLocalAssignments(
      Object.fromEntries((assignmentsQuery.data ?? []).map((a) => [a.purpose, a.model_name])),
    );
  }, [assignmentsQuery.data]);

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: ["llm-settings", "assignments"] });
    qc.invalidateQueries({ queryKey: ["llm-settings", "diagnostics"] });
  };

  const setMutation = useMutation({
    mutationFn: ({ purpose, modelName }: { purpose: string; modelName: string }) =>
      api.setAssignment(purpose, modelName),
    onMutate: ({ purpose, modelName }) => {
      const prev = localAssignments[purpose];
      setLocalAssignments((a) => ({ ...a, [purpose]: modelName }));
      return { prev, purpose };
    },
    onError: (e, _, ctx) => {
      if (ctx) setLocalAssignments((a) => ({ ...a, [ctx.purpose]: ctx.prev ?? "" }));
      toast({ type: "error", message: "Не удалось сохранить" });
    },
    onSuccess: () => {
      toast({ type: "success", message: "Сохранено" });
      invalidateAll();
    },
  });

  const resetMutation = useMutation({
    mutationFn: () => api.resetAssignmentsToRecommended(),
    onSuccess: () => {
      toast({ type: "success", message: "Назначения сброшены к рекомендуемым" });
      invalidateAll();
    },
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка сброса" }),
  });

  if (purposesQuery.isLoading || assignmentsQuery.isLoading || modelsQuery.isLoading)
    return <SkeletonList />;
  if (purposesQuery.isError || assignmentsQuery.isError)
    return (
      <ErrorState
        message="Не удалось загрузить назначения"
        onRetry={() => {
          purposesQuery.refetch();
          assignmentsQuery.refetch();
        }}
      />
    );

  const purposes = purposesQuery.data ?? [];
  const availableModels = (modelsQuery.data ?? []).map((m) => m.model_name);

  if (availableModels.length === 0) {
    return (
      <div className="llm-settings__empty">
        <p className="llm-settings__empty-icon">⚙️</p>
        <p>
          <strong>Сначала добавьте модели</strong>
        </p>
        <p>
          Перейдите в «Источники» → подключите провайдера → нажмите «Обновить каталог».
          Затем вернитесь сюда.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="llm-settings__row-head">
        <span />
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => resetMutation.mutate()}
          disabled={resetMutation.isPending}
        >
          {resetMutation.isPending ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}{" "}
          Сбросить к рекомендуемым
        </button>
      </div>

      <table className="llm-table">
        <thead>
          <tr>
            <th>Сценарий</th>
            <th>Модель</th>
            <th>Куда пойдёт при запуске</th>
          </tr>
        </thead>
        <tbody>
          {purposes.map((p) => {
            const current = localAssignments[p.id] ?? "";
            const missing = Boolean(current) && !availableModels.includes(current);
            const hasValidAssignment = Boolean(current) && !missing;
            const diag = diagnosticsQuery.data?.find((d) => d.purpose === p.id);
            return (
              <tr key={p.id} className={missing ? "llm-table__row--warn" : undefined}>
                <td>{p.label}</td>
                <td>
                  <select
                    value={missing ? "" : current}
                    onChange={(e) =>
                      setMutation.mutate({ purpose: p.id, modelName: e.target.value })
                    }
                  >
                    {!hasValidAssignment ? (
                      <option value="" disabled>
                        {missing
                          ? `${current} — потеряна, выберите другую`
                          : "не назначено"}
                      </option>
                    ) : null}
                    {availableModels.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                  {missing ? (
                    <p className="llm-form__error" style={{ marginTop: 4 }}>
                      Модель «{current}» недоступна — выберите другую.
                    </p>
                  ) : null}
                </td>
                <td className="llm-diag-cell">
                  <ResolutionPreview diag={diag} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Проверить build**

```bash
cd ui/workspace && npm run build 2>&1 | tail -10
```

Ожидаемый результат: `✓ built in`.

- [ ] **Step 3: Commit**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx
git commit -m "feat(settings): AssignmentsTab — optimistic updates, skeleton, error state, onboarding empty state"
```

---

## Task 8: Финальная проверка

- [ ] **Step 1: Полный build**

```bash
cd ui/workspace && npm run build 2>&1
```

Ожидаемый результат: `✓ built in` без ошибок TypeScript. Если есть ошибки — исправить и перепроверить.

- [ ] **Step 2: Dev-сервер + ручная проверка**

```bash
cd ui/workspace && npm run dev
```

Открыть `http://localhost:5173`, перейти на `/settings`.

Чеклист проверки:

- [ ] **Tab badges:** создать провайдера без теста → видна amber-точка на табе «Источники». После успешного теста — точка исчезает.
- [ ] **Skeleton:** перезагрузить страницу с throttle сети → видны 3 shimmer-placeholder'а вместо «Загрузка…».
- [ ] **ProviderRow collapsed:** строка компактная, нет поля concurrency и кнопки «Обновить каталог».
- [ ] **ProviderRow expanded:** нажать ▼ → раскрывается секция с select concurrency + кнопка «Обновить каталог».
- [ ] **Inline delete confirm:** нажать 🗑 → строка заменяется блоком «Удалить «имя»? [Отмена] [Удалить]». Отмена возвращает строку.
- [ ] **Modal — новый провайдер:** кнопка «Подключить источник» → Modal открывается. Escape закрывает. Клик на backdrop закрывает.
- [ ] **Modal — кастомная модель:** то же самое.
- [ ] **Toast success:** сохранить назначение → появляется зелёный toast «Сохранено», исчезает через 4s.
- [ ] **Toast error:** отключить сеть, сохранить → появляется красный toast «Не удалось сохранить».
- [ ] **Поиск моделей:** вкладка «Модели» → ввести часть имени → список фильтруется. Ввести несуществующее → empty state.
- [ ] **Optimistic update:** на вкладке «Назначения» выбрать другую модель → select меняется мгновенно (нет задержки).
- [ ] **Onboarding empty state — Источники:** удалить все провайдеры → empty state с иконкой и кнопкой «Подключить источник».

- [ ] **Step 3: Финальный commit (если были мелкие правки)**

```bash
git add ui/workspace/src/LlmSettingsPage.tsx ui/workspace/src/styles.css
git commit -m "fix(settings): post-review tweaks"
```
