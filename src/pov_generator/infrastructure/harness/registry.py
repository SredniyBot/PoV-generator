"""Резолвер harness-провайдеров.

Зеркало ``infrastructure/llm/registry.py``: единственное место, где живёт выбор
конкретного harness-исполнителя. Сервисам отдаёт готовый
:class:`HarnessProvider`.

Ф7c: помимо ``stub`` поддерживаются реальные адаптеры (``aider``,
``claude_code``, ``command``). Какой адаптер — задаётся подключением
(:class:`HarnessConnection`): тип + образ + модель. Подключение приходит из
настроек, а на первом этапе — из env (bootstrap, как у LLM): ``POV_HARNESS_*``.
Если ничего не настроено — дефолт ``stub`` (детерминированный, без Docker), так
что CI остаётся зелёным.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Callable

from ...common.errors import ConflictError
from .protocol import HarnessProvider
from .providers.aider import AiderHarnessProvider
from .providers.claude_code import ClaudeCodeHarnessProvider
from .providers.command import CommandHarnessProvider
from .providers.stub import StubHarnessProvider
from .sandbox import SandboxRuntime

# Дефолтный harness, пока ничего не настроено. Stub — детерминированный, без
# Docker; в проде дефолт сменится на настоящий адаптер через подключение.
_DEFAULT_PROVIDER = "stub"

# Дефолтные образы адаптеров (если подключение не задало свой). Имена-плейсхолдеры:
# реальные образы собираются/тянутся на онбординге (Ф4).
_DEFAULT_IMAGES = {
    "aider": "povgen/aider:latest",
    "claude_code": "povgen/claude-code:latest",
    "command": "busybox:latest",
}


@dataclass(frozen=True)
class HarnessConnection:
    """Конфигурация выбранного harness-исполнителя.

    Секреты (креды модели) ЗДЕСЬ НЕ ХРАНЯТСЯ — они подаются в песочницу эфемерно
    в момент прогона (правило проекта). Тут только нечувствительный выбор:
    тип адаптера, образ, имя модели, (для generic) команда.

    ``engine`` (Ф7e): ``docker`` (изоляция, креды по API) или ``host``
    (исполнение на хосте — переиспользует залогиненную сессию claude CLI; только
    для ``claude_code``). ``host_security``: для host-режима — ``restricted``
    (только файловые правки) или ``full`` (полный доступ, опт-ин).
    """

    provider: str = _DEFAULT_PROVIDER
    image: str | None = None
    model: str | None = None
    command: str | None = None
    default_timeout_s: int | None = None
    engine: str = "docker"
    host_security: str = "restricted"


# Матрица возможностей адаптеров — для выбора в настройках (Ф7c) и подсказок UI.
# Не оценки/гарантии, а характеристики инструмента.
ADAPTER_CAPABILITIES: dict[str, dict[str, object]] = {
    "stub": {
        "title": "Stub (фикстуры)",
        "autonomy": "none",
        "models": "—",
        "git_native": False,
        "needs_docker": False,
        "best_for": "Тесты и CI без Docker и сети.",
        "default_image": "",
        "default_model": "",
        "supports_host": False,
    },
    "claude_code": {
        "title": "Claude Code",
        "autonomy": "high",
        "models": "Claude (Anthropic)",
        "git_native": False,
        "needs_docker": True,
        "best_for": "Сложное многофайловое «построй X» с разведкой.",
        "default_image": _DEFAULT_IMAGES["claude_code"],
        "default_model": "claude-opus-4-8",
        # Единственный адаптер с host-режимом: переиспользует залогиненную
        # сессию claude CLI с хоста (Ф7e).
        "supports_host": True,
    },
    "aider": {
        "title": "Aider",
        "autonomy": "medium",
        "models": "Любые (litellm)",
        "git_native": True,
        "needs_docker": True,
        "best_for": "Точные правки по спеке, контроль стоимости, чистый diff.",
        "default_image": _DEFAULT_IMAGES["aider"],
        "default_model": "gpt-4o-mini",
        "supports_host": False,
    },
    "command": {
        "title": "Generic command",
        "autonomy": "varies",
        "models": "—",
        "git_native": False,
        "needs_docker": True,
        "best_for": "Escape hatch: произвольный агент-CLI.",
        "default_image": _DEFAULT_IMAGES["command"],
        "default_model": "",
        "supports_host": False,
    },
}

# Билдеры адаптеров: тип → как собрать из подключения + песочницы.
_ADAPTER_BUILDERS: dict[
    str, Callable[["HarnessConnection", SandboxRuntime], HarnessProvider]
] = {
    "aider": lambda c, sb: AiderHarnessProvider(
        sandbox=sb,
        image=c.image or _DEFAULT_IMAGES["aider"],
        model=c.model,
        default_timeout_s=c.default_timeout_s,
    ),
    "claude_code": lambda c, sb: ClaudeCodeHarnessProvider(
        sandbox=sb,
        image=c.image or _DEFAULT_IMAGES["claude_code"],
        model=c.model,
        default_timeout_s=c.default_timeout_s,
    ),
    "command": lambda c, sb: CommandHarnessProvider(
        sandbox=sb,
        image=c.image or _DEFAULT_IMAGES["command"],
        command=c.command or "true",
        model=c.model,
        default_timeout_s=c.default_timeout_s,
    ),
}


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def connection_from_env() -> HarnessConnection:
    """Подключение из env (bootstrap). Без ``POV_HARNESS_PROVIDER`` → stub."""
    return HarnessConnection(
        provider=os.environ.get("POV_HARNESS_PROVIDER", _DEFAULT_PROVIDER) or _DEFAULT_PROVIDER,
        image=os.environ.get("POV_HARNESS_IMAGE") or None,
        model=os.environ.get("POV_HARNESS_MODEL") or None,
        command=os.environ.get("POV_HARNESS_COMMAND") or None,
        default_timeout_s=_int_env("POV_HARNESS_TIMEOUT_S"),
        engine=os.environ.get("POV_HARNESS_ENGINE", "docker") or "docker",
        host_security=os.environ.get("POV_HARNESS_HOST_SECURITY", "restricted")
        or "restricted",
    )


class HarnessProviderRegistry:
    """Резолвит и собирает :class:`HarnessProvider` по подключению."""

    def __init__(
        self,
        *,
        connection: HarnessConnection | None = None,
        connection_loader: Callable[[], HarnessConnection] | None = None,
        sandbox: SandboxRuntime | None = None,
    ) -> None:
        # Подключение: ленивый загрузчик (читается на каждый резолв — смена
        # настроек применяется без перезапуска), либо статическое, либо env.
        self._static_connection = connection
        self._connection_loader = connection_loader
        # Песочница для реальных адаптеров. Если не передана — ленивый
        # DockerSandboxRuntime (импорт docker откладывается до прогона).
        self._sandbox = sandbox

    def _active_connection(self) -> HarnessConnection:
        if self._connection_loader is not None:
            return self._connection_loader()
        return self._static_connection or connection_from_env()

    @property
    def supported_providers(self) -> tuple[str, ...]:
        return ("stub", *sorted(_ADAPTER_BUILDERS.keys()))

    def default_provider_name(self) -> str:
        """Имя выбранного harness (для метаданных/трейса узла)."""
        return self._active_connection().provider

    def _runtime_for(self, connection: HarnessConnection) -> SandboxRuntime:
        # Инъекция (тесты) имеет приоритет для любого движка.
        if self._sandbox is not None:
            return self._sandbox
        # Ленивый импорт: без Docker модуль грузится, ошибка — только на прогоне.
        if connection.engine == "host":
            from .sandbox import HostSandboxRuntime

            return HostSandboxRuntime()
        from .sandbox import DockerSandboxRuntime

        self._sandbox = DockerSandboxRuntime()
        return self._sandbox

    def _build(self, connection: HarnessConnection) -> HarnessProvider:
        if connection.provider == "stub":
            return StubHarnessProvider()
        # Host-движок: исполнение на хосте (переиспользует сессию claude CLI).
        # Допустим только для claude_code — у прочих адаптеров своя аутентификация.
        if connection.engine == "host":
            if connection.provider != "claude_code":
                raise ConflictError(
                    "Исполнение на хосте доступно только для адаптера claude_code "
                    "(переиспользует залогиненную сессию claude CLI). "
                    "Для остальных адаптеров используйте docker-движок."
                )
            return ClaudeCodeHarnessProvider(
                sandbox=self._runtime_for(connection),
                image=connection.image or _DEFAULT_IMAGES["claude_code"],
                model=connection.model,
                default_timeout_s=connection.default_timeout_s,
                host_security=connection.host_security or "restricted",
            )
        builder = _ADAPTER_BUILDERS.get(connection.provider)
        if builder is None:
            raise ConflictError(
                f"Неподдерживаемый harness-провайдер: '{connection.provider}'. "
                f"Поддерживаются: {', '.join(self.supported_providers)}."
            )
        return builder(connection, self._runtime_for(connection))

    def get(self, provider: str) -> HarnessProvider:
        """Собрать провайдера по имени (с конфигом текущего подключения)."""
        connection = self._active_connection()
        if provider != connection.provider:
            connection = replace(connection, provider=provider)
        return self._build(connection)

    def resolve_default(self) -> HarnessProvider:
        """Собрать harness по текущему подключению (Ф7c)."""
        return self._build(self._active_connection())
