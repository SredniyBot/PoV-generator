/**
 * System-wide настройки LLM: подключения, каталог моделей, назначения.
 *
 * Три таба (см. план в Stage 5):
 *   1. Источники — CRUD provider connections + test button.
 *   2. Модели — каталог с routings + test + добавление custom.
 *   3. Назначения — какая модель для какого purpose; reset to recommended.
 *
 * Доступ через `/settings` (root-level, не per-project). Кнопка в
 * ProjectRail. Менеджер на эту страницу не ходит — он работает с
 * дефолтами, которые здесь настраиваются.
 */

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
      <div className="settings-toast-container">
        {toasts.map((t) => (
          <div key={t.id} className={`settings-toast settings-toast--${t.type}`}>
            <span>{t.message}</span>
            <button
              type="button"
              className="settings-toast__close btn-inline"
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


// --- Tab 1: Providers --------------------------------------------------------


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
      qc.invalidateQueries({ queryKey: ["llm-settings", "assignments"] });
      qc.invalidateQueries({ queryKey: ["llm-settings", "diagnostics"] });
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
    mutationFn: ({ value, extras }: { value: string; extras: Record<string, string> }) =>
      api.updateProvider(provider.connection_id, {
        extras: { ...extras, max_concurrency: value },
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
              <label className="field llm-row__concurrency-field">
                <span className="llm-row__concurrency-label">Параллельных шагов</span>
                <select
                  className="llm-row__concurrency-select"
                  value={concurrency}
                  onChange={(e) => {
                    setConcurrency(e.target.value);
                    concurrencyMutation.mutate({ value: e.target.value, extras: provider.extras });
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
    onError: (e) =>
      toast({ type: "error", message: e instanceof Error ? e.message : "Ошибка создания" }),
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


// --- Tab 2: Models -----------------------------------------------------------


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

  const setLimitMutation = useMutation({
    mutationFn: ({ modelName, tokens }: { modelName: string; tokens: number | null }) =>
      api.setModelContextLimit(modelName, tokens),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "models"] }),
  });

  // Перемещение routing вверх/вниз по списку — обмениваем priority с
  // соседом. Это устраняет «магический» числовой ввод (баг #4): пользователь
  // видит порядок и двигает стрелками, как в обычных списках.
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
              onSetContextLimit={(tokens) =>
                setLimitMutation.mutate({ modelName: m.model_name, tokens })
              }
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


/** Токены в компактную подпись: 200000 -> «200k», 1000000 -> «1M». */
function formatTokens(n: number): string {
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  return `${Math.round(n / 1000)}k`;
}

/**
 * Строка каталога моделей — аккордеон. В покое: имя + основной провайдер +
 * окно контекста тихим текстом. Редактирование (порядок маршрутов, окно,
 * удаление) раскрывается по клику — каталог по умолчанию читается, а не правится.
 */
function ModelRow({
  entry,
  onTest,
  testPending,
  testResult,
  onDeleteRouting,
  onMoveRouting,
  reorderPending,
  onSetContextLimit,
}: {
  entry: ModelCatalogEntry;
  onTest: () => void;
  testPending: boolean;
  testResult: TestResultView | null;
  onDeleteRouting: (routingId: string) => void;
  onMoveRouting: (routingId: string, direction: "up" | "down") => void;
  reorderPending: boolean;
  onSetContextLimit: (tokens: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const primary = entry.routings[0];
  const extra = entry.routings.length - 1;

  return (
    <li className={"llm-model" + (open ? " llm-model--open" : "")}>
      <div
        className="llm-model__head"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((v) => !v);
          }
        }}
      >
        <span className="llm-model__chevron" aria-hidden="true">
          {open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </span>
        <span className="llm-model__name">{entry.model_name}</span>
        <span className="llm-model__via">
          {primary ? (
            <>
              через {primary.connection_display_name}
              <em> ({humanProviderType(primary.provider_type)})</em>
              {extra > 0 ? (
                <span className="llm-model__extra" title="запасных маршрутов">
                  +{extra}
                </span>
              ) : null}
            </>
          ) : (
            <span className="llm-model__via--none">нет маршрута</span>
          )}
        </span>
        <span className="llm-model__window" title="Окно контекста модели">
          окно {formatTokens(entry.context_limit)}
          {!entry.context_limit_is_default ? (
            <span className="llm-model__window-mark">изменено</span>
          ) : null}
        </span>
        <button
          type="button"
          className="btn btn--ghost llm-model__test"
          onClick={(e) => {
            e.stopPropagation();
            onTest();
          }}
          disabled={testPending}
        >
          {testPending ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Тест
        </button>
      </div>

      {testResult ? (
        <p
          className={
            testResult.status === "ok"
              ? "llm-model__result llm-model__result--ok"
              : "llm-model__result llm-model__result--err"
          }
        >
          {testResult.message}
          {testResult.latency_ms ? ` · ${testResult.latency_ms} ms` : null}
        </p>
      ) : null}

      {open ? (
        <div className="llm-model__detail">
          <div className="llm-model__detail-block">
            <span className="llm-model__detail-label">Маршруты — порядок перебора</span>
            <ol className="llm-model__routings">
              {entry.routings.map((r, idx) => {
                const isFirst = idx === 0;
                const isLast = idx === entry.routings.length - 1;
                const onlyOne = entry.routings.length === 1;
                return (
                  <li key={r.routing_id} className="llm-model__routing">
                    <span
                      className={
                        "llm-model__rank" + (isFirst ? " llm-model__rank--primary" : "")
                      }
                    >
                      {isFirst ? "основной" : `#${idx + 1}`}
                    </span>
                    <span className="llm-model__routing-name">
                      {r.connection_display_name}
                      <em> ({humanProviderType(r.provider_type)})</em>
                      {!r.enabled ? (
                        <span className="llm-badge llm-badge--neutral">выкл</span>
                      ) : null}
                    </span>
                    <div className="llm-model__routing-actions">
                      {!onlyOne ? (
                        <>
                          <button
                            type="button"
                            className="btn-inline"
                            onClick={() => onMoveRouting(r.routing_id, "up")}
                            disabled={isFirst || reorderPending}
                            title="Сделать приоритетнее"
                          >
                            <ArrowUp size={14} />
                          </button>
                          <button
                            type="button"
                            className="btn-inline"
                            onClick={() => onMoveRouting(r.routing_id, "down")}
                            disabled={isLast || reorderPending}
                            title="Понизить приоритет"
                          >
                            <ArrowDown size={14} />
                          </button>
                        </>
                      ) : null}
                      <button
                        type="button"
                        className="btn-inline"
                        onClick={() => onDeleteRouting(r.routing_id)}
                        title="Удалить этот маршрут"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
          <div className="llm-model__detail-block">
            <ContextLimitControl
              value={entry.context_limit}
              isDefault={entry.context_limit_is_default}
              onSave={onSetContextLimit}
            />
          </div>
        </div>
      ) : null}
    </li>
  );
}

/**
 * Поле окна контекста модели (токены) — её реальная ёмкость. Это потолок: из
 * него система выводит рабочий бюджет входа задачи. Сохраняется по blur;
 * «сбросить» возвращает дефолт-окно по семейству модели.
 */
function ContextLimitControl({
  value,
  isDefault,
  onSave,
}: {
  value: number;
  isDefault: boolean;
  onSave: (tokens: number | null) => void;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => {
    setText(String(value));
  }, [value]);

  const commit = () => {
    const next = Number.parseInt(text, 10);
    if (Number.isNaN(next) || next === value) {
      setText(String(value));
      return;
    }
    onSave(next);
  };

  return (
    <div className="llm-row__limit">
      <label className="llm-row__limit-label">Окно модели, токенов</label>
      <input
        type="number"
        min={1000}
        step={1000}
        className="llm-row__limit-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        title="Окно контекста модели (её ёмкость). Потолок бюджета входа задачи."
      />
      {isDefault ? (
        <span className="llm-row__limit-hint">по умолчанию</span>
      ) : (
        <button
          type="button"
          className="btn-inline llm-row__limit-reset"
          onClick={() => onSave(null)}
          title="Сбросить к значению по умолчанию"
        >
          сбросить
        </button>
      )}
    </div>
  );
}


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


// --- Tab 3: Assignments ------------------------------------------------------


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

  // Optimistic local state — mirrors server assignments, updated instantly on change.
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
                    <p className="llm-form__error llm-form__error--row">
                      <AlertCircle size={12} /> Модель «{current}» недоступна — выберите другую.
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


function ResolutionPreview({
  diag,
}: {
  diag: NonNullable<Awaited<ReturnType<typeof api.getSettingsDiagnostics>>>[number] | undefined;
}) {
  if (!diag) return <span className="llm-diag llm-diag--neutral">…</span>;
  if (diag.error) {
    return <span className="llm-diag llm-diag--err">{diag.error}</span>;
  }
  const r = diag.resolved;
  if (!r) return <span className="llm-diag llm-diag--neutral">—</span>;
  return (
    <span className="llm-diag llm-diag--ok">
      <strong>{r.model_name}</strong>
      <span className="llm-diag__via">через {r.connection_display_name}</span>
      {r.fallback_routings.length > 0 ? (
        <span
          className="llm-diag__fallback"
          title={r.fallback_routings.map((f) => f.connection_display_name).join(", ")}
        >
          +{r.fallback_routings.length} backup
        </span>
      ) : null}
    </span>
  );
}


// --- Helpers -----------------------------------------------------------------


function humanProviderType(t: ProviderType): string {
  return t === "anthropic" ? "Anthropic API" : t === "openrouter" ? "OpenRouter" : "Claude CLI";
}
