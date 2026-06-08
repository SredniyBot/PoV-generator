/**
 * «Настройки окружения» — среда исполнения harness-агентов (Ф6/Ф7).
 *
 * Три секции:
 *   1. Среда — Docker и ёмкость хоста (информационно).
 *   2. Загрузка — живой статус рантайма: слоты, очередь, накопленный расход.
 *   3. Исполнитель — выбор адаптера + образ/модель + естественный шаг сборки
 *      образа агента (готов / собрать).
 *
 * System-wide, доступ через `/settings/environment` (не per-project).
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
  Save,
  Server,
  XCircle,
} from "lucide-react";

import { api } from "./api";

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
          Среда исполнения узлов-агентов: Docker, текущая загрузка и выбор исполнителя.
        </p>
      </header>

      <ReadinessSection statusQuery={statusQuery} />
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

// ── Среда ────────────────────────────────────────────────────────────────────

function ReadinessSection({
  statusQuery,
}: {
  statusQuery: ReturnType<typeof useQuery<Awaited<ReturnType<typeof api.getHarnessStatus>>>>;
}): JSX.Element {
  const data = statusQuery.data;
  const docker = data?.docker;
  const dockerOk = Boolean(docker?.available);

  return (
    <section className="mroom-card">
      <h2 className="mroom-card__title">
        <Server size={16} /> Среда
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
                dockerOk ? `доступен${docker?.version ? ` · ${docker.version}` : ""}` : "недоступен"
              }
            />
            <Tile label="Параллельных контейнеров" value={String(data.capacity.max_concurrent)} />
          </div>
          {!dockerOk && data.blockers.length > 0 ? (
            <ul className="mroom-blockers">
              {data.blockers.map((b) => (
                <li key={b}>
                  <AlertCircle size={13} /> {b}
                </li>
              ))}
            </ul>
          ) : null}
          {!dockerOk && docker?.hint ? (
            <p className="mroom-muted mroom-fineprint">{docker.hint}</p>
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
            <Tile label="Слоты" value={`${data.slots.in_use} / ${data.slots.capacity} занято`} />
            <Tile label="В очереди" value={String(data.slots.waiting)} />
            <Tile label="Прогонов" value={String(data.budget.runs)} />
            <Tile
              label="Токенов (накоплено)"
              value={data.budget.total_tokens.toLocaleString("ru-RU")}
            />
          </div>
          {data.budget_exceeded ? (
            <p className="mroom-result mroom-result--err">
              <AlertCircle size={13} /> {data.budget_exceeded}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

// ── Исполнитель (выбор адаптера + сборка образа) ─────────────────────────────

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

  // Выбор адаптера подставляет его дефолты (образ/модель встроены в проект).
  const chooseAdapter = (p: string) => {
    setProvider(p);
    const cap = caps[p];
    setImage(cap?.default_image ?? "");
    setModel(cap?.default_model ?? "");
    if (p !== "command") setCommand("");
    setBuilding(false);
  };

  const supportsImage = provider !== "stub";
  const needsCommand = provider === "command";

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

  // Состояние образа агента (готов / собирается / не собран) — естественный
  // шаг подготовки. Опрашиваем чаще во время сборки.
  const imageStatusQuery = useQuery({
    queryKey: ["harness", "image-status", image],
    queryFn: () => api.getHarnessImageStatus(image),
    enabled: supportsImage && Boolean(image.trim()),
    refetchInterval: building ? 2500 : 10000,
  });
  const imgStatus = imageStatusQuery.data;
  const imageReady = Boolean(imgStatus?.ready);
  const progress = imgStatus?.progress ?? null;
  const inProgress = building || Boolean(progress?.in_progress);

  // Сборка завершилась (поток закрыт) → выходим из состояния «собирается»
  // независимо от исхода; ready/error покажет результат.
  useEffect(() => {
    if (building && progress && !progress.in_progress) setBuilding(false);
  }, [progress, building]);

  const startBuild = () => {
    if (!image.trim()) return;
    setBuilding(true);
    void api.prepareHarnessImage(image.trim());
  };

  const orderedProviders = PROVIDER_ORDER.filter((p) => p in caps);

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

      {/* Естественный шаг подготовки: состояние образа агента прямо в форме. */}
      {supportsImage && image.trim() ? (
        <div className="mroom-imgstate">
          {inProgress ? (
            <p className="mroom-muted">
              <Loader2 size={13} className="spin" /> Сборка образа…{" "}
              {progress?.status ? `${progress.status} ` : ""}— может занять несколько минут
            </p>
          ) : imageReady ? (
            <p className="mroom-result mroom-result--ok">
              <CheckCircle2 size={13} /> Образ агента готов к запуску
            </p>
          ) : progress?.error ? (
            <p className="mroom-result mroom-result--err">
              <XCircle size={13} /> Сборка не удалась: {progress.error}
            </p>
          ) : (
            <p className="mroom-muted">
              Образ агента ещё не собран — соберите его перед запуском.
            </p>
          )}
        </div>
      ) : null}

      {error ? (
        <p className="mroom-result mroom-result--err">
          <XCircle size={13} /> {error}
        </p>
      ) : null}

      <div className="mroom-actions">
        {supportsImage && !imageReady ? (
          <button
            type="button"
            className="btn btn--primary"
            disabled={inProgress || !image.trim()}
            onClick={startBuild}
          >
            {inProgress ? <Loader2 size={14} className="spin" /> : <Box size={14} />}
            Собрать образ
          </button>
        ) : null}
        <button
          type="button"
          className={supportsImage && !imageReady ? "btn btn--ghost" : "btn btn--primary"}
          disabled={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
          Сохранить
        </button>
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
