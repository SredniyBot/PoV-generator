/**
 * «Машинное отделение» — наблюдаемость и настройки harness-исполнителей (Ф6/Ф7).
 *
 * Три секции:
 *   1. Готовность — Docker, ёмкость хоста, подготовка образа, самопроверка.
 *   2. Загрузка — живой статус рантайма: слоты, очередь, накопленный расход.
 *   3. Исполнитель — выбор адаптера (матрица возможностей) + образ/модель.
 *
 * System-wide, доступ через `/machine-room` (root-level, не per-project).
 * Деньги/время показываем как внутренние лимиты прогона, не оценки заказчику.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  AlertCircle,
  Box,
  CheckCircle2,
  Cpu,
  Loader2,
  PlayCircle,
  Save,
  Server,
  XCircle,
} from "lucide-react";

import { api } from "./api";
import type { HarnessSelfTestView } from "./types";

const PROVIDER_ORDER = ["claude_code", "aider", "command", "stub"] as const;

export function MachineRoomPage(): JSX.Element {
  const queryClient = useQueryClient();

  const statusQuery = useQuery({
    queryKey: ["harness", "status"],
    queryFn: () => api.getHarnessStatus(),
    refetchInterval: 5000,
  });
  const runtimeQuery = useQuery({
    queryKey: ["harness", "runtime"],
    queryFn: () => api.getHarnessRuntime(),
    refetchInterval: 4000,
  });
  const adaptersQuery = useQuery({
    queryKey: ["harness", "adapters"],
    queryFn: () => api.getHarnessAdapters(),
  });
  const connectionQuery = useQuery({
    queryKey: ["harness", "connection"],
    queryFn: () => api.getHarnessConnection(),
  });

  return (
    <div className="llm-settings mroom">
      <header className="llm-settings__header">
        <h1>Настройки окружения</h1>
        <p className="mroom__subtitle">
          Среда исполнения узлов-агентов: готовность Docker, текущая загрузка и выбор исполнителя.
        </p>
      </header>

      <ReadinessSection
        statusQuery={statusQuery}
        onChanged={() => {
          void queryClient.invalidateQueries({ queryKey: ["harness", "status"] });
        }}
      />
      <LoadSection runtimeQuery={runtimeQuery} />
      <ExecutorSection
        adaptersQuery={adaptersQuery}
        connectionQuery={connectionQuery}
        onSaved={() => {
          void queryClient.invalidateQueries({ queryKey: ["harness", "connection"] });
          void queryClient.invalidateQueries({ queryKey: ["harness", "adapters"] });
          void queryClient.invalidateQueries({ queryKey: ["harness", "runtime"] });
        }}
      />
    </div>
  );
}

// ── Готовность ─────────────────────────────────────────────────────────────

function ReadinessSection({
  statusQuery,
  onChanged,
}: {
  statusQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof api.getHarnessStatus>>>>;
  onChanged: () => void;
}): JSX.Element {
  const [selfTest, setSelfTest] = useState<HarnessSelfTestView | null>(null);

  const prepareMutation = useMutation({
    mutationFn: () => api.prepareHarnessImage(),
    onSuccess: onChanged,
  });
  const selfTestMutation = useMutation({
    mutationFn: () => api.harnessSelfTest(),
    onSuccess: (result) => setSelfTest(result),
  });

  const data = statusQuery.data;
  const docker = data?.docker;
  const dockerOk = Boolean(docker?.available);
  const pull = data?.pull ?? null;

  return (
    <section className="mroom-card">
      <h2 className="mroom-card__title">
        <Server size={16} /> Готовность
      </h2>
      {statusQuery.isLoading || !data ? (
        <p className="mroom-muted">Опрашиваем подсистему…</p>
      ) : (
        <>
          <div className="mroom-grid">
            <StatusTile
              label="Docker"
              ok={dockerOk}
              value={
                dockerOk
                  ? `доступен${docker?.version ? ` · ${docker.version}` : ""}`
                  : "недоступен"
              }
            />
            <Tile label="Ёмкость хоста" value={`${data.capacity.max_concurrent} контейнер(ов)`} />
            <StatusTile
              label="Образ агента"
              ok={data.image_ready}
              value={data.image_ready ? "готов" : "не подготовлен"}
            />
            <StatusTile
              label="Готовность"
              ok={data.ready}
              value={data.ready ? "можно запускать" : "есть блокеры"}
            />
          </div>

          {data.blockers.length > 0 ? (
            <ul className="mroom-blockers">
              {data.blockers.map((b) => (
                <li key={b}>
                  <AlertCircle size={13} /> {b}
                </li>
              ))}
            </ul>
          ) : null}

          {/* Подсказка по Docker (напр. «найден, но нужен Python-SDK»). */}
          {docker?.hint ? <p className="mroom-muted mroom-fineprint">{docker.hint}</p> : null}

          {pull && pull.in_progress ? (
            <p className="mroom-muted">
              <Loader2 size={13} className="spin" /> Подготовка образа: {pull.status ?? "…"}
              {typeof pull.progress === "number" ? ` (${pull.progress}%)` : ""}
            </p>
          ) : null}

          <div className="mroom-actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={!dockerOk || prepareMutation.isPending}
              onClick={() => prepareMutation.mutate()}
              title={dockerOk ? "Скачать образ агента" : "Нужен Docker"}
            >
              {prepareMutation.isPending ? <Loader2 size={14} className="spin" /> : <Box size={14} />}
              Подготовить образ
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={!dockerOk || selfTestMutation.isPending}
              onClick={() => selfTestMutation.mutate()}
              title={dockerOk ? "Тривиальный прогон в песочнице" : "Нужен Docker"}
            >
              {selfTestMutation.isPending ? (
                <Loader2 size={14} className="spin" />
              ) : (
                <PlayCircle size={14} />
              )}
              Самопроверка
            </button>
          </div>

          {selfTest ? (
            <p className={selfTest.ok ? "mroom-result mroom-result--ok" : "mroom-result mroom-result--err"}>
              {selfTest.ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
              {selfTest.ok
                ? `Самопроверка пройдена за ${selfTest.duration_ms} мс`
                : `Самопроверка не пройдена: ${selfTest.error ?? "без деталей"}`}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

// ── Загрузка (живой статус) ──────────────────────────────────────────────────

function LoadSection({
  runtimeQuery,
}: {
  runtimeQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof api.getHarnessRuntime>>>>;
}): JSX.Element {
  const data = runtimeQuery.data;
  return (
    <section className="mroom-card">
      <h2 className="mroom-card__title">
        <Activity size={16} /> Загрузка
      </h2>
      {!data ? (
        <p className="mroom-muted">Опрашиваем рантайм…</p>
      ) : (
        <>
          <div className="mroom-grid">
            <Tile label="Исполнитель" value={data.provider_name} />
            <Tile
              label="Слоты"
              value={`${data.slots.in_use} / ${data.slots.capacity} занято`}
            />
            <Tile label="В очереди" value={String(data.slots.waiting)} />
            <Tile label="Прогонов" value={String(data.budget.runs)} />
            <Tile label="Токенов (накоплено)" value={data.budget.total_tokens.toLocaleString("ru-RU")} />
          </div>
          {data.budget_exceeded ? (
            <p className="mroom-result mroom-result--err">
              <AlertCircle size={13} /> {data.budget_exceeded}
            </p>
          ) : null}
          <p className="mroom-muted mroom-fineprint">
            Лимиты прогона — внутреннее управление ресурсами, не оценки стоимости для заказчика.
          </p>
        </>
      )}
    </section>
  );
}

// ── Исполнитель (выбор адаптера) ─────────────────────────────────────────────

function ExecutorSection({
  adaptersQuery,
  connectionQuery,
  onSaved,
}: {
  adaptersQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof api.getHarnessAdapters>>>>;
  connectionQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof api.getHarnessConnection>>>>;
  onSaved: () => void;
}): JSX.Element {
  const [provider, setProvider] = useState<string>("stub");
  const [image, setImage] = useState<string>("");
  const [model, setModel] = useState<string>("");
  const [command, setCommand] = useState<string>("");
  const [seeded, setSeeded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [building, setBuilding] = useState(false);

  const caps = adaptersQuery.data?.capabilities ?? {};

  // Засеваем форму сохранённым подключением один раз при загрузке.
  const conn = connectionQuery.data;
  useEffect(() => {
    if (conn && !seeded) {
      setProvider(conn.provider);
      setImage(conn.image ?? "");
      setModel(conn.model ?? "");
      setCommand(conn.command ?? "");
      setSeeded(true);
    }
  }, [conn, seeded]);

  // Выбор адаптера подставляет его дефолты (образ/модель встроены в проект) —
  // пользователю не нужно ничего вписывать вручную.
  const chooseAdapter = (p: string) => {
    setProvider(p);
    const cap = caps[p];
    setImage(cap?.default_image ?? "");
    setModel(cap?.default_model ?? "");
    if (p !== "command") setCommand("");
    setBuilding(false);
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      api.setHarnessConnection({
        provider,
        image: image.trim() || null,
        model: model.trim() || null,
        command: command.trim() || null,
      }),
    onSuccess: () => {
      setError(null);
      onSaved();
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  // Сборка образа агента из встроенного Dockerfile: запуск + опрос прогресса.
  const buildProgress = useQuery({
    queryKey: ["harness", "image-progress", image],
    queryFn: () => api.getHarnessImageProgress(image),
    enabled: building && Boolean(image.trim()),
    refetchInterval: building ? 2500 : false,
  });
  useEffect(() => {
    const p = buildProgress.data;
    if (building && p && !p.in_progress && (p.ready || p.error)) setBuilding(false);
  }, [buildProgress.data, building]);
  const startBuild = () => {
    if (!image.trim()) return;
    setBuilding(true);
    void api.prepareHarnessImage(image.trim());
  };
  const build = buildProgress.data;

  const orderedProviders = PROVIDER_ORDER.filter((p) => p in caps);
  const needsCommand = provider === "command";
  const supportsImage = provider !== "stub";

  return (
    <section className="mroom-card">
      <h2 className="mroom-card__title">
        <Cpu size={16} /> Исполнитель
      </h2>

      <div className="mroom-adapters">
        {orderedProviders.map((p) => {
          const cap = caps[p];
          if (!cap) return null;
          return (
            <button
              key={p}
              type="button"
              className={"mroom-adapter" + (provider === p ? " mroom-adapter--active" : "")}
              onClick={() => chooseAdapter(p)}
            >
              <span className="mroom-adapter__title">{cap.title}</span>
              <span className="mroom-adapter__best">{cap.best_for}</span>
            </button>
          );
        })}
      </div>

      {supportsImage ? (
        <div className="mroom-form">
          <label className="mroom-field">
            <span>Docker-образ</span>
            <input
              type="text"
              value={image}
              placeholder="povgen/aider:latest"
              onChange={(e) => setImage(e.target.value)}
            />
          </label>
          <label className="mroom-field">
            <span>Модель</span>
            <input
              type="text"
              value={model}
              placeholder={provider === "aider" ? "gpt-4o-mini" : "claude-opus-4-8"}
              onChange={(e) => setModel(e.target.value)}
            />
          </label>
          {needsCommand ? (
            <label className="mroom-field">
              <span>Команда</span>
              <input
                type="text"
                value={command}
                placeholder="run-agent --task"
                onChange={(e) => setCommand(e.target.value)}
              />
            </label>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <p className="mroom-result mroom-result--err">
          <XCircle size={13} /> {error}
        </p>
      ) : null}

      {build && supportsImage ? (
        build.error ? (
          <p className="mroom-result mroom-result--err">
            <XCircle size={13} /> Сборка не удалась: {build.error}
          </p>
        ) : build.ready ? (
          <p className="mroom-result mroom-result--ok">
            <CheckCircle2 size={13} /> Образ собран
          </p>
        ) : building ? (
          <p className="mroom-muted">
            <Loader2 size={13} className="spin" /> Сборка образа: {build.status ?? "…"}
          </p>
        ) : null
      ) : null}

      <div className="mroom-actions">
        <button
          type="button"
          className="btn btn--primary"
          disabled={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
          Сохранить
        </button>
        {supportsImage ? (
          <button
            type="button"
            className="btn btn--ghost"
            disabled={building || !image.trim()}
            onClick={startBuild}
            title="Собрать образ агента из встроенного Dockerfile"
          >
            {building ? <Loader2 size={14} className="spin" /> : <Box size={14} />}
            Собрать образ
          </button>
        ) : null}
      </div>
    </section>
  );
}

// ── Мелкие плитки ─────────────────────────────────────────────────────────────

function Tile({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="mroom-tile">
      <span className="mroom-tile__label">{label}</span>
      <span className="mroom-tile__value">{value}</span>
    </div>
  );
}

function StatusTile({
  label,
  value,
  ok,
}: {
  label: string;
  value: string;
  ok: boolean;
}): JSX.Element {
  return (
    <div className="mroom-tile">
      <span className="mroom-tile__label">{label}</span>
      <span className={"mroom-tile__value " + (ok ? "mroom-ok" : "mroom-warn")}>
        {ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />} {value}
      </span>
    </div>
  );
}
