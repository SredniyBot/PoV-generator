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
import { Link } from "react-router-dom";
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
  const [engine, setEngine] = useState<"docker" | "host">("docker");
  const [hostSecurity, setHostSecurity] = useState<"restricted" | "full">("restricted");
  const [network, setNetwork] = useState<"none" | "online">("none");
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
      setEngine(conn.engine ?? "docker");
      setHostSecurity(conn.host_security ?? "restricted");
      setNetwork(conn.network ?? "none");
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
    // Host-движок доступен только адаптерам с supports_host — иначе сбрасываем.
    if (!cap?.supports_host) setEngine("docker");
    setBuilding(false);
  };

  const selectedCap = caps[provider];
  const canHost = Boolean(selectedCap?.supports_host);
  const isHost = engine === "host" && canHost;

  const supportsImage = provider !== "stub" && !isHost; // на хосте образ не нужен
  const needsCommand = provider === "command";

  const saveMutation = useMutation({
    mutationFn: () =>
      api.setHarnessConnection({
        provider,
        image: image.trim() || null,
        model: model.trim() || null,
        command: command.trim() || null,
        engine,
        host_security: hostSecurity,
        network,
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

  // Связка LLM↔агент: агент берёт креды и модель из настроенного LLM-провайдера
  // проекта (единый источник истины). Показываем явно, чтобы пользователь видел
  // связь и при необходимости перешёл в «Настройки → LLM».
  const llmQuery = useQuery({
    queryKey: ["harness", "llm"],
    queryFn: () => api.getHarnessLlm(),
  });
  const llm = llmQuery.data;
  // #1: модель агента выбирается из НАСТРОЕННЫХ моделей LLM (а не свободным
  // вводом). Пустое значение = «модель проекта» (берётся из настроек LLM).
  const modelsQuery = useQuery({
    queryKey: ["settings", "models"],
    queryFn: () => api.listModels(),
  });
  const configuredModels = (modelsQuery.data ?? []).map((m) => m.model_name);
  // #3: устаревший override модели (нет в каталоге настроенных LLM, напр. старый
  // выдуманный gpt-4o-mini) сбрасываем в «по умолчанию» — он невалиден (нет
  // маршрута). Только после загрузки каталога и засева формы.
  useEffect(() => {
    if (seeded && modelsQuery.data && model && !configuredModels.includes(model)) {
      setModel("");
    }
  }, [seeded, modelsQuery.data, model, configuredModels]);
  const renderModelSelect = () => (
    <label className="mroom-field">
      <span>Модель агента</span>
      <select
        className="mroom-select"
        value={configuredModels.includes(model) ? model : ""}
        onChange={(e) => setModel(e.target.value)}
      >
        <option value="">По умолчанию (модель проекта из настроек LLM)</option>
        {configuredModels.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    </label>
  );
  const usesHostSession = engine === "host";

  return (
    <section className="mroom-card">
      <h2 className="mroom-card__title">
        <Cpu size={16} /> Исполнитель
      </h2>

      <div className="mroom-llm">
        {usesHostSession ? (
          <p className="mroom-muted mroom-fineprint">
            На хосте агент использует вашу залогиненную сессию claude CLI — креды
            из настроек LLM не требуются.
          </p>
        ) : llm?.configured ? (
          <p className="mroom-muted mroom-fineprint">
            Модель и ключ агент берёт из ваших настроек LLM:{" "}
            <strong>{llm.provider}</strong>
            {llm.model ? (
              <>
                {" "}· модель <strong>{llm.model}</strong>
              </>
            ) : null}
            . <Link to="/settings/llm">Настройки LLM</Link>
          </p>
        ) : (
          <p className="mroom-result mroom-result--err">
            <AlertCircle size={13} /> LLM-провайдер для агента не настроен — без
            него агент в docker не сможет вызвать модель и написать код.{" "}
            <Link to="/settings/llm">Настроить LLM</Link>
          </p>
        )}
      </div>

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

      {/* Где исполнять: Docker (изоляция) или хост (сессия claude CLI). Виден
          только для адаптеров с host-режимом (claude_code). */}
      {canHost ? (
        <div className="mroom-engine">
          <span className="mroom-engine__label">Где исполнять</span>
          <div className="mroom-seg">
            <button
              type="button"
              className={"mroom-seg__btn" + (engine === "docker" ? " is-active" : "")}
              onClick={() => setEngine("docker")}
            >
              В Docker · изоляция
            </button>
            <button
              type="button"
              className={"mroom-seg__btn" + (engine === "host" ? " is-active" : "")}
              onClick={() => setEngine("host")}
            >
              На хосте · сессия claude CLI
            </button>
          </div>
          {isHost ? (
            <p className="mroom-muted mroom-fineprint">
              claude запускается на этом компьютере и переиспользует вашу залогиненную
              сессию claude CLI — без второй настройки и ключей. Сервисы проекта агент
              собирает и запускает в Docker, не на хосте.
            </p>
          ) : null}
        </div>
      ) : null}

      {/* Режим безопасности host-исполнения. */}
      {isHost ? (
        <div className="mroom-engine">
          <span className="mroom-engine__label">Доступ агента на хосте</span>
          <div className="mroom-seg">
            <button
              type="button"
              className={
                "mroom-seg__btn" + (hostSecurity === "restricted" ? " is-active" : "")
              }
              onClick={() => setHostSecurity("restricted")}
            >
              Только файлы · безопасно
            </button>
            <button
              type="button"
              className={"mroom-seg__btn" + (hostSecurity === "full" ? " is-active" : "")}
              onClick={() => setHostSecurity("full")}
            >
              Полный доступ
            </button>
          </div>
          {hostSecurity === "restricted" ? (
            <p className="mroom-muted mroom-fineprint">
              Агент правит только файлы в рабочем каталоге — без хостового shell и сети.
              Сборка, тесты и запуск сервисов — в Docker.
            </p>
          ) : (
            <p className="mroom-result mroom-result--err">
              <AlertCircle size={13} /> Полный доступ: агент сможет выполнять любые
              команды на хосте без ОС-изоляции. Включайте, только если доверяете прогону.
            </p>
          )}
        </div>
      ) : null}

      {/* Доступ в сеть для зависимостей (только docker-движок; на хосте — сеть
          хоста). Deny-by-default; online нужен, чтобы агент ставил библиотеки. */}
      {!isHost && provider !== "stub" ? (
        <div className="mroom-engine">
          <span className="mroom-engine__label">Доступ в сеть (зависимости)</span>
          <div className="mroom-seg">
            <button
              type="button"
              className={"mroom-seg__btn" + (network === "none" ? " is-active" : "")}
              onClick={() => setNetwork("none")}
            >
              Без сети · изоляция
            </button>
            <button
              type="button"
              className={"mroom-seg__btn" + (network === "online" ? " is-active" : "")}
              onClick={() => setNetwork("online")}
            >
              С сетью · ставить зависимости
            </button>
          </div>
          {network === "online" ? (
            <p className="mroom-result mroom-result--err">
              <AlertCircle size={13} /> С сетью агент и сборка могут ставить
              зависимости из реестров (pip/npm) — но у песочницы есть выход в
              интернет. Включайте, если доверяете прогону.
            </p>
          ) : (
            <p className="mroom-muted mroom-fineprint">
              Песочница без сети (изоляция). Если коду нужны внешние библиотеки —
              включите сеть, иначе их не установить и сборка офлайн упадёт.
            </p>
          )}
        </div>
      ) : null}

      {/* На хосте образ не нужен — только модель агента (из настроек LLM). */}
      {isHost ? <div className="mroom-form">{renderModelSelect()}</div> : null}

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
          {renderModelSelect()}
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
