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

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Plus, RefreshCw, Trash2, XCircle } from "lucide-react";

import { api } from "./api";
import type {
  ModelCatalogEntry,
  ProviderConnectionView,
  ProviderType,
  TestResultView,
} from "./types";

type TabKey = "providers" | "models" | "assignments";


export function LlmSettingsPage() {
  const [tab, setTab] = useState<TabKey>("providers");

  return (
    <div className="llm-settings">
      <header className="llm-settings__header">
        <h1>Настройки LLM</h1>
        <p>
          Подключите источники моделей, проверьте, что они работают, и назначьте, какие модели
          использовать в каких сценариях. Менеджеры проектов на эту страницу не заходят — они
          работают с дефолтами, которые здесь настроены.
        </p>
      </header>

      <div className="llm-settings__tabs">
        <TabButton active={tab === "providers"} onClick={() => setTab("providers")}>
          Источники
        </TabButton>
        <TabButton active={tab === "models"} onClick={() => setTab("models")}>
          Модели
        </TabButton>
        <TabButton active={tab === "assignments"} onClick={() => setTab("assignments")}>
          Назначения
        </TabButton>
      </div>

      <div className="llm-settings__body">
        {tab === "providers" ? <ProvidersTab /> : null}
        {tab === "models" ? <ModelsTab /> : null}
        {tab === "assignments" ? <AssignmentsTab /> : null}
      </div>
    </div>
  );
}


function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={active ? "llm-settings__tab llm-settings__tab--active" : "llm-settings__tab"}
      onClick={onClick}
    >
      {children}
    </button>
  );
}


// --- Tab 1: Providers --------------------------------------------------------


function ProvidersTab() {
  const qc = useQueryClient();
  const providersQuery = useQuery({
    queryKey: ["llm-settings", "providers"],
    queryFn: () => api.listProviders(),
  });
  const [showForm, setShowForm] = useState(false);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteProvider(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-settings", "providers"] });
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => api.testProvider(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "providers"] }),
  });

  if (providersQuery.isLoading) return <p>Загрузка…</p>;
  const providers = providersQuery.data ?? [];

  return (
    <div>
      <div className="llm-settings__row-head">
        <span>Подключено источников: {providers.length}</span>
        <button type="button" className="btn btn--primary" onClick={() => setShowForm(true)}>
          <Plus size={14} /> Подключить источник
        </button>
      </div>

      {providers.length === 0 ? (
        <div className="llm-settings__empty">
          <p>
            Нет ни одного подключения. Без них workflow не запустится — модель будет некому отдать промпт.
          </p>
        </div>
      ) : (
        <ul className="llm-settings__list">
          {providers.map((p) => (
            <ProviderRow
              key={p.connection_id}
              provider={p}
              onTest={() => testMutation.mutate(p.connection_id)}
              onDelete={() => {
                if (confirm(`Удалить подключение «${p.display_name}»?`)) {
                  deleteMutation.mutate(p.connection_id);
                }
              }}
              testPending={testMutation.isPending && testMutation.variables === p.connection_id}
              testResult={
                testMutation.data && testMutation.variables === p.connection_id
                  ? testMutation.data
                  : null
              }
            />
          ))}
        </ul>
      )}

      {showForm ? <NewProviderForm onClose={() => setShowForm(false)} /> : null}
    </div>
  );
}


function ProviderRow({
  provider,
  onTest,
  onDelete,
  testPending,
  testResult,
}: {
  provider: ProviderConnectionView;
  onTest: () => void;
  onDelete: () => void;
  testPending: boolean;
  testResult: TestResultView | null;
}) {
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

  return (
    <li className="llm-row">
      <div className="llm-row__main">
        <strong>{provider.display_name}</strong>
        <span className="llm-row__sub">
          {humanProviderType(provider.provider_type)}
          {provider.api_key_preview ? ` · ключ ${provider.api_key_preview}` : null}
          {provider.source === "env_bootstrap" ? (
            <span className="llm-badge llm-badge--neutral"> из .env</span>
          ) : null}
        </span>
        {provider.last_test_message ? (
          <p className="llm-row__hint">{provider.last_test_message}</p>
        ) : null}
        {testResult ? (
          <p
            className={
              testResult.status === "ok"
                ? "llm-row__hint llm-row__hint--ok"
                : "llm-row__hint llm-row__hint--err"
            }
          >
            {testResult.message}
            {testResult.latency_ms ? ` · ${testResult.latency_ms} ms` : null}
            {testResult.sample_response ? ` · ответ: "${testResult.sample_response}"` : null}
          </p>
        ) : null}
      </div>
      <div className="llm-row__side">
        {statusBadge}
        <button type="button" className="btn btn--ghost" onClick={onTest} disabled={testPending}>
          {testPending ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Проверить
        </button>
        <button type="button" className="btn btn--ghost btn--danger" onClick={onDelete}>
          <Trash2 size={14} /> Удалить
        </button>
      </div>
    </li>
  );
}


function NewProviderForm({ onClose }: { onClose: () => void }) {
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
      onClose();
    },
  });

  const error = createMutation.error instanceof Error ? createMutation.error.message : null;

  return (
    <form
      className="llm-form"
      onSubmit={(e) => {
        e.preventDefault();
        createMutation.mutate();
      }}
    >
      <h3>Новый источник моделей</h3>

      <label>
        Тип
        <select
          value={providerType}
          onChange={(e) => setProviderType(e.target.value as ProviderType)}
        >
          <option value="anthropic">Anthropic API (ключ от Anthropic)</option>
          <option value="openrouter">OpenRouter (агрегатор)</option>
          <option value="claude_cli">Claude CLI (подписка через локальный `claude`)</option>
        </select>
      </label>

      <label>
        Название
        <input
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="напр. «Anthropic prod» или «OpenRouter dev»"
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
      ) : (
        <p className="llm-form__note">
          CLI работает через локальный <code>claude</code> и авторизуется через{" "}
          <code>claude login</code>. API-key не нужен.
        </p>
      )}

      {providerType === "openrouter" ? (
        <label>
          Base URL <span className="llm-form__optional">(необязательно)</span>
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

      <p className="llm-form__hint">
        После сохранения нажмите «Проверить» у созданного источника — система пошлёт минимальный
        test-запрос.
      </p>
    </form>
  );
}


// --- Tab 2: Models -----------------------------------------------------------


function ModelsTab() {
  const qc = useQueryClient();
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
  });

  const deleteRoutingMutation = useMutation({
    mutationFn: (routingId: string) => api.deleteRouting(routingId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "models"] }),
  });

  const [addModelFor, setAddModelFor] = useState<string | null>(null);

  if (modelsQuery.isLoading) return <p>Загрузка…</p>;
  const models = modelsQuery.data ?? [];
  const providers = providersQuery.data ?? [];

  return (
    <div>
      <div className="llm-settings__row-head">
        <span>Моделей в каталоге: {models.length}</span>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => setAddModelFor(providers[0]?.connection_id ?? null)}
          disabled={providers.length === 0}
        >
          <Plus size={14} /> Добавить свою модель
        </button>
      </div>

      {models.length === 0 ? (
        <div className="llm-settings__empty">
          <p>Каталог пуст — сначала подключите хотя бы один источник на вкладке «Источники».</p>
        </div>
      ) : (
        <ul className="llm-settings__list">
          {models.map((m) => (
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
              onDeleteRouting={(routingId) => {
                if (confirm("Удалить этот маршрут модели?")) {
                  deleteRoutingMutation.mutate(routingId);
                }
              }}
            />
          ))}
        </ul>
      )}

      {addModelFor ? (
        <AddCustomModelForm
          providers={providers}
          defaultConnectionId={addModelFor}
          onClose={() => setAddModelFor(null)}
        />
      ) : null}
    </div>
  );
}


function ModelRow({
  entry,
  onTest,
  testPending,
  testResult,
  onDeleteRouting,
}: {
  entry: ModelCatalogEntry;
  onTest: () => void;
  testPending: boolean;
  testResult: TestResultView | null;
  onDeleteRouting: (routingId: string) => void;
}) {
  return (
    <li className="llm-row">
      <div className="llm-row__main">
        <strong>{entry.model_name}</strong>
        <span className="llm-row__sub">
          {entry.routings.length === 1 && entry.routings[0]
            ? `через ${entry.routings[0].connection_display_name}`
            : `${entry.routings.length} маршрута`}
        </span>
        <ul className="llm-row__sublist">
          {entry.routings.map((r) => (
            <li key={r.routing_id}>
              <span>
                {r.connection_display_name} ({humanProviderType(r.provider_type)}) — priority{" "}
                {r.priority}
                {!r.enabled ? " · disabled" : ""}
              </span>
              <button
                type="button"
                className="btn-inline"
                onClick={() => onDeleteRouting(r.routing_id)}
                title="Удалить этот маршрут"
              >
                <Trash2 size={12} />
              </button>
            </li>
          ))}
        </ul>
        {testResult ? (
          <p
            className={
              testResult.status === "ok"
                ? "llm-row__hint llm-row__hint--ok"
                : "llm-row__hint llm-row__hint--err"
            }
          >
            {testResult.message}
            {testResult.latency_ms ? ` · ${testResult.latency_ms} ms` : null}
          </p>
        ) : null}
      </div>
      <div className="llm-row__side">
        <button type="button" className="btn btn--ghost" onClick={onTest} disabled={testPending}>
          {testPending ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Тест
        </button>
      </div>
    </li>
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
  const qc = useQueryClient();
  const [connectionId, setConnectionId] = useState(defaultConnectionId);
  const [modelName, setModelName] = useState("");

  const addMutation = useMutation({
    mutationFn: () =>
      api.addCustomModel({ connection_id: connectionId, model_name: modelName }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-settings", "models"] });
      onClose();
    },
  });

  return (
    <form
      className="llm-form"
      onSubmit={(e) => {
        e.preventDefault();
        addMutation.mutate();
      }}
    >
      <h3>Добавить кастомную модель</h3>
      <p className="llm-form__note">
        Используется, если нужной модели нет в стандартном каталоге провайдера.
      </p>

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
          Добавить
        </button>
      </div>
    </form>
  );
}


// --- Tab 3: Assignments ------------------------------------------------------


function AssignmentsTab() {
  const qc = useQueryClient();
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

  const setMutation = useMutation({
    mutationFn: ({ purpose, modelName }: { purpose: string; modelName: string }) =>
      api.setAssignment(purpose, modelName),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "assignments"] }),
  });

  const resetMutation = useMutation({
    mutationFn: () => api.resetAssignmentsToRecommended(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["llm-settings", "assignments"] }),
  });

  if (purposesQuery.isLoading || assignmentsQuery.isLoading || modelsQuery.isLoading) {
    return <p>Загрузка…</p>;
  }

  const purposes = purposesQuery.data ?? [];
  const assignmentsByPurpose = Object.fromEntries(
    (assignmentsQuery.data ?? []).map((a) => [a.purpose, a.model_name]),
  );
  const availableModels = (modelsQuery.data ?? []).map((m) => m.model_name);

  return (
    <div>
      <div className="llm-settings__row-head">
        <p className="llm-settings__hint">
          Какая модель используется в каком сценарии. Менеджеры проектов не выбирают модели
          на лету — они идут через эти дефолты. Можно вернуться к рекомендуемым значениям
          одной кнопкой.
        </p>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => resetMutation.mutate()}
          disabled={resetMutation.isPending || availableModels.length === 0}
        >
          {resetMutation.isPending ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
          Сбросить к рекомендуемым
        </button>
      </div>

      {availableModels.length === 0 ? (
        <div className="llm-settings__empty">
          <p>В каталоге пока нет моделей — подключите источник на вкладке «Источники».</p>
        </div>
      ) : (
        <table className="llm-table">
          <thead>
            <tr>
              <th>Сценарий</th>
              <th>Модель</th>
            </tr>
          </thead>
          <tbody>
            {purposes.map((p) => {
              const current = assignmentsByPurpose[p.id] ?? "";
              const missing = current && !availableModels.includes(current);
              return (
                <tr key={p.id}>
                  <td>{p.label}</td>
                  <td>
                    <select
                      value={missing ? "" : current}
                      onChange={(e) =>
                        setMutation.mutate({ purpose: p.id, modelName: e.target.value })
                      }
                    >
                      <option value="" disabled>
                        {missing
                          ? `${current} — модель потеряна, выберите другую`
                          : "не назначено"}
                      </option>
                      {availableModels.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}


// --- Helpers -----------------------------------------------------------------


function humanProviderType(t: ProviderType): string {
  return t === "anthropic" ? "Anthropic API" : t === "openrouter" ? "OpenRouter" : "Claude CLI";
}
