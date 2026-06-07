"""Песочница исполнения harness-агентов (Ф2).

`SandboxRuntime` — абстракция среды, в которой реально работает агент: поднять
изолированную песочницу из образа, засеять входные файлы, исполнить команду со
стримом логов и лимитами, собрать результат, снести. Движок за абстракцией:
сейчас Docker (`DockerSandboxRuntime`) и in-memory `StubSandboxRuntime` для
тестов/CI (без Docker). Позже — Podman/remote/k8s/hosted.

Контейнер ВСЕГДА ephemeral (поднял на прогон → снёс). Область рабочего каталога
(per-node / per-group общий том) — забота вызывающего слоя, не песочницы.

Docker — опциональная зависимость (`pip install .[harness]`): импортируется
лениво, отсутствие → понятная ошибка. CI без Docker остаётся зелёным на стабе.
"""

from __future__ import annotations

import io
import shlex
import tarfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ...common.errors import ConflictError

# Сетевой режим песочницы. Ф2: none (egress запрещён, дефолт безопасности) или
# bridge (есть сеть). Белый список хостов — позже (Ф4), поверх bridge.
NetworkMode = str  # "none" | "bridge"


@dataclass(frozen=True)
class ResourceLimits:
    """cgroup-лимиты контейнера. None → лимит не задаётся (берётся дефолт движка)."""

    cpus: float | None = None        # docker --cpus (доли ядра)
    memory_mb: int | None = None     # docker --memory
    pids: int | None = None          # docker --pids-limit
    network: NetworkMode = "none"    # дефолт безопасности — без сети


@dataclass(frozen=True)
class SandboxSpec:
    """Параметры поднятия песочницы.

    ``volume`` (Ф8): ключ общего тома сборочной группы. Несколько песочниц с
    одним ``volume`` видят общий рабочий каталог — это «B-lite общий том» для
    fan-out реализации компонентов одной группы. None → изолированный per-node
    каталог (значение по умолчанию, безопаснее).
    """

    image: str
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    workdir: str = "/work"
    env: Mapping[str, str] = field(default_factory=dict)
    volume: str | None = None


@dataclass(frozen=True)
class SandboxHandle:
    """Непрозрачная ссылка на поднятую песочницу. ``native`` — ref движка."""

    id: str
    workdir: str
    native: Any = None


@dataclass(frozen=True)
class ExecResult:
    """Итог одной исполненной в песочнице команды."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


LogSink = Callable[[str], None]


@runtime_checkable
class SandboxRuntime(Protocol):
    """Контракт среды исполнения агента (движок-агностично)."""

    def provision(self, spec: SandboxSpec) -> SandboxHandle: ...
    def put_files(self, handle: SandboxHandle, files: Mapping[str, bytes]) -> None: ...
    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
        on_log: LogSink | None = None,
    ) -> ExecResult: ...
    def get_files(self, handle: SandboxHandle, path: str) -> dict[str, bytes]: ...
    def stop(self, handle: SandboxHandle) -> None: ...
    def destroy(self, handle: SandboxHandle) -> None: ...


# --- In-memory stub (тесты/CI, без Docker) ----------------------------------


class StubSandboxRuntime:
    """Детерминированная in-memory песочница.

    Файлы держатся в словаре по абсолютному пути. ``exec`` не запускает реальный
    процесс — вызывает инъектированный ``exec_handler`` (тест эмулирует поведение
    агента: пишет файлы через ``put_files``). По умолчанию — успех без вывода.
    """

    def __init__(
        self,
        exec_handler: Callable[["StubSandboxRuntime", SandboxHandle, list[str]], ExecResult]
        | None = None,
    ) -> None:
        # handle.id → ссылка на словарь-ФС (общий для группы тома или приватный).
        self._fs: dict[str, dict[str, bytes]] = {}
        # ключ тома → общий словарь-ФС (Ф8: общий том сборочной группы).
        self._volumes: dict[str, dict[str, bytes]] = {}
        self._specs: dict[str, SandboxSpec] = {}
        self._alive: set[str] = set()
        self._counter = 0
        self._exec_handler = exec_handler
        # История exec'ов (для проверок в тестах).
        self.exec_calls: list[tuple[str, list[str]]] = []

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        self._counter += 1
        handle_id = f"stub-{self._counter}"
        # Общий том группы: все песочницы с одним volume делят один словарь-ФС.
        # Без volume — приватный per-node каталог.
        if spec.volume is not None:
            store = self._volumes.setdefault(spec.volume, {})
        else:
            store = {}
        self._fs[handle_id] = store
        self._specs[handle_id] = spec
        self._alive.add(handle_id)
        return SandboxHandle(id=handle_id, workdir=spec.workdir, native=None)

    def put_files(self, handle: SandboxHandle, files: Mapping[str, bytes]) -> None:
        self._ensure_alive(handle)
        store = self._fs[handle.id]
        for path, content in files.items():
            store[_norm(path)] = content

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
        on_log: LogSink | None = None,
    ) -> ExecResult:
        self._ensure_alive(handle)
        argv_list = list(argv)
        self.exec_calls.append((handle.id, argv_list))
        if self._exec_handler is not None:
            result = self._exec_handler(self, handle, argv_list)
        else:
            result = ExecResult(exit_code=0, stdout="", stderr="")
        if on_log and result.stdout:
            on_log(result.stdout)
        return result

    def get_files(self, handle: SandboxHandle, path: str) -> dict[str, bytes]:
        self._ensure_alive(handle)
        prefix = _norm(path).rstrip("/") + "/"
        store = self._fs[handle.id]
        return {p: data for p, data in store.items() if p == _norm(path) or p.startswith(prefix)}

    def stop(self, handle: SandboxHandle) -> None:
        self._alive.discard(handle.id)

    def destroy(self, handle: SandboxHandle) -> None:
        self._alive.discard(handle.id)
        self._fs.pop(handle.id, None)
        self._specs.pop(handle.id, None)

    def spec_for(self, handle: SandboxHandle) -> SandboxSpec:
        """Спека поднятия (для проверки лимитов в тестах)."""
        return self._specs[handle.id]

    def _ensure_alive(self, handle: SandboxHandle) -> None:
        if handle.id not in self._alive:
            raise ConflictError(f"Песочница '{handle.id}' уже снесена/не существует.")


def _norm(path: str) -> str:
    """Нормализуем путь к абсолютному виду без двойных слэшей."""
    p = path.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    while "//" in p:
        p = p.replace("//", "/")
    return p


# --- Docker (реальная песочница) --------------------------------------------


def _require_docker() -> Any:
    """Лениво импортировать docker SDK; понятная ошибка, если не установлен."""
    try:
        import docker  # type: ignore
    except ImportError as exc:  # noqa: BLE001
        raise ConflictError(
            "Для harness-песочницы нужен Docker SDK. Установите: pip install '.[harness]' "
            "и запустите Docker. Без Docker используйте stub-harness."
        ) from exc
    return docker


class DockerSandboxRuntime:
    """Песочница на Docker через docker-py.

    Контейнер поднимается долгоживущим (``sleep infinity``) и сносится в
    ``destroy``; команды агента идут через ``exec_run`` со стримом логов.
    Лимиты — cgroup-флаги; egress запрещён по умолчанию (``network_mode=none``).
    Таймаут прогона enforce'им сами (docker exec нативного таймаута не имеет):
    исполняем в потоке и при истечении гасим контейнер.
    """

    _KEEPALIVE = ["sleep", "infinity"]

    def __init__(self, client: Any | None = None) -> None:
        # client передаётся в тестах (mock); иначе — from_env при первом
        # использовании, чтобы импорт модуля не требовал Docker.
        self._client = client

    def _docker_client(self) -> Any:
        if self._client is None:
            docker = _require_docker()
            try:
                self._client = docker.from_env()
            except Exception as exc:  # noqa: BLE001 — демон недоступен
                raise ConflictError(
                    f"Docker-демон недоступен: {str(exc).strip() or type(exc).__name__}. "
                    "Запустите Docker и повторите."
                ) from exc
        return self._client

    def provision(self, spec: SandboxSpec) -> SandboxHandle:
        client = self._docker_client()
        limits = spec.limits
        kwargs: dict[str, Any] = {
            "image": spec.image,
            "command": self._KEEPALIVE,
            "detach": True,
            "working_dir": spec.workdir,
            "environment": dict(spec.env),
            "network_mode": "none" if limits.network == "none" else limits.network,
            # Безопасность: не даём поднимать привилегии внутри контейнера.
            "security_opt": ["no-new-privileges"],
        }
        if limits.cpus is not None:
            kwargs["nano_cpus"] = int(limits.cpus * 1_000_000_000)
        if limits.memory_mb is not None:
            kwargs["mem_limit"] = f"{limits.memory_mb}m"
        if limits.pids is not None:
            kwargs["pids_limit"] = limits.pids
        # Ф8: общий том сборочной группы. Именованный docker-том монтируется в
        # рабочий каталог; контейнеры одной группы (один volume) делят файлы.
        # Том переживает снос контейнера (ephemeral — только контейнер).
        if spec.volume is not None:
            kwargs["volumes"] = {
                f"povgen-grp-{spec.volume}": {"bind": spec.workdir, "mode": "rw"}
            }
        try:
            container = client.containers.run(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise ConflictError(
                f"Не удалось поднять песочницу из образа '{spec.image}': "
                f"{str(exc).strip() or type(exc).__name__}."
            ) from exc
        # рабочий каталог должен существовать для put/get
        try:
            container.exec_run(["mkdir", "-p", spec.workdir])
        except Exception:  # noqa: BLE001 — не критично, образ мог уже иметь workdir
            pass
        return SandboxHandle(id=container.id, workdir=spec.workdir, native=container)

    def put_files(self, handle: SandboxHandle, files: Mapping[str, bytes]) -> None:
        container = handle.native
        # Группируем по каталогу назначения: put_archive принимает tar + dest-dir.
        by_dir: dict[str, dict[str, bytes]] = {}
        for path, content in files.items():
            norm = _norm(path)
            directory, _, name = norm.rpartition("/")
            by_dir.setdefault(directory or "/", {})[name] = content
        for directory, members in by_dir.items():
            container.exec_run(["mkdir", "-p", directory])
            container.put_archive(directory, _make_tar(members))

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
        on_log: LogSink | None = None,
    ) -> ExecResult:
        container = handle.native
        out_chunks: list[bytes] = []
        result_holder: dict[str, Any] = {}

        def _run() -> None:
            try:
                exit_code, stream = container.exec_run(
                    list(argv),
                    environment=dict(env or {}),
                    workdir=handle.workdir,
                    stream=True,
                    demux=False,
                )
                for chunk in stream:
                    if not chunk:
                        continue
                    out_chunks.append(chunk)
                    if on_log:
                        on_log(chunk.decode("utf-8", errors="replace"))
                result_holder["exit_code"] = exit_code
            except Exception as exc:  # noqa: BLE001
                result_holder["error"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            # Таймаут: гасим контейнер (форсированный обрыв), помечаем timed_out.
            try:
                container.kill()
            except Exception:  # noqa: BLE001
                pass
            return ExecResult(
                exit_code=124,
                stdout=b"".join(out_chunks).decode("utf-8", errors="replace"),
                stderr="",
                timed_out=True,
            )
        if "error" in result_holder:
            raise ConflictError(
                f"Ошибка исполнения в песочнице: {str(result_holder['error']).strip()}"
            )
        # exec_run в stream-режиме возвращает exit_code=None до завершения; берём
        # фактический код через inspect, если поток отдал None.
        exit_code = result_holder.get("exit_code")
        if exit_code is None:
            exit_code = self._exec_exit_code(container)
        return ExecResult(
            exit_code=int(exit_code or 0),
            stdout=b"".join(out_chunks).decode("utf-8", errors="replace"),
            stderr="",
        )

    def get_files(self, handle: SandboxHandle, path: str) -> dict[str, bytes]:
        container = handle.native
        norm = _norm(path)
        try:
            stream, _stat = container.get_archive(norm)
        except Exception:  # noqa: BLE001 — пути может не быть (агент не создал)
            return {}
        raw = b"".join(stream)
        return _read_tar(raw, base=norm)

    def stop(self, handle: SandboxHandle) -> None:
        try:
            handle.native.stop(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def destroy(self, handle: SandboxHandle) -> None:
        try:
            handle.native.remove(force=True)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _exec_exit_code(container: Any) -> int:
        try:
            container.reload()
            return int(container.attrs.get("State", {}).get("ExitCode", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0


# --- tar helpers (общие для Docker put/get) ---------------------------------


def _make_tar(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _read_tar(raw: bytes, *, base: str) -> dict[str, bytes]:
    """Распаковать tar из get_archive в {абсолютный_путь: содержимое}.

    docker get_archive отдаёт пути относительно родителя ``base`` (имя
    последнего сегмента — корень архива). Восстанавливаем абсолютные пути.
    """
    parent = base.rsplit("/", 1)[0] or "/"
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            abs_path = _norm(f"{parent}/{member.name}")
            out[abs_path] = extracted.read()
    return out


# Удобный псевдоним для команд через shell-строку (агент-CLI часто так задают).
def shell_argv(command: str) -> list[str]:
    """Превратить shell-команду в argv для exec (через sh -lc)."""
    return ["sh", "-lc", command if isinstance(command, str) else shlex.join(command)]
