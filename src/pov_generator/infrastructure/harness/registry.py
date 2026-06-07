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
from dataclasses import dataclass
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
    """

    provider: str = _DEFAULT_PROVIDER
    image: str | None = None
    model: str | None = None
    command: str | None = None
    default_timeout_s: int | None = None


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
    },
    "claude_code": {
        "title": "Claude Code",
        "autonomy": "high",
        "models": "Claude (Anthropic)",
        "git_native": False,
        "needs_docker": True,
        "best_for": "Сложное многофайловое «построй X» с разведкой.",
    },
    "aider": {
        "title": "Aider",
        "autonomy": "medium",
        "models": "Любые (litellm)",
        "git_native": True,
        "needs_docker": True,
        "best_for": "Точные правки по спеке, контроль стоимости, чистый diff.",
    },
    "command": {
        "title": "Generic command",
        "autonomy": "varies",
        "models": "—",
        "git_native": False,
        "needs_docker": True,
        "best_for": "Escape hatch: произвольный агент-CLI.",
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
    )


class HarnessProviderRegistry:
    """Резолвит и собирает :class:`HarnessProvider` по подключению."""

    def __init__(
        self,
        *,
        connection: HarnessConnection | None = None,
        sandbox: SandboxRuntime | None = None,
    ) -> None:
        # Подключение явно или из env (по умолчанию stub).
        self._connection = connection or connection_from_env()
        # Песочница для реальных адаптеров. Если не передана — ленивый
        # DockerSandboxRuntime (импорт docker откладывается до прогона).
        self._sandbox = sandbox

    @property
    def supported_providers(self) -> tuple[str, ...]:
        return ("stub", *sorted(_ADAPTER_BUILDERS.keys()))

    def default_provider_name(self) -> str:
        """Имя выбранного harness (для метаданных/трейса узла)."""
        return self._connection.provider

    def _sandbox_runtime(self) -> SandboxRuntime:
        if self._sandbox is not None:
            return self._sandbox
        # Ленивый импорт: без Docker модуль грузится, ошибка — только на прогоне.
        from .sandbox import DockerSandboxRuntime

        self._sandbox = DockerSandboxRuntime()
        return self._sandbox

    def get(self, provider: str) -> HarnessProvider:
        """Собрать провайдера по имени (с конфигом текущего подключения)."""
        if provider == "stub":
            return StubHarnessProvider()
        builder = _ADAPTER_BUILDERS.get(provider)
        if builder is None:
            raise ConflictError(
                f"Неподдерживаемый harness-провайдер: '{provider}'. "
                f"Поддерживаются: {', '.join(self.supported_providers)}."
            )
        return builder(self._connection, self._sandbox_runtime())

    def resolve_default(self) -> HarnessProvider:
        """Собрать harness по текущему подключению (Ф7c)."""
        return self.get(self._connection.provider)
