"""Claude Code-адаптер (Ф7): автономный «строитель» как harness-провайдер.

Claude Code — многошаговый агент: берёт постановку и сам решает, что и как
сделать (под-агенты, инструменты, многофайловые правки). Для harness'а это
дефолтный адаптер под «собери X по спеке». Сбор — **по соглашению**: brief
велит положить каждый ожидаемый артефакт в ``.povgen/out/<role>.<fmt>``, и агент
это делает своими файловыми инструментами.

Тонкая специализация :class:`SandboxHarnessProvider`: отличается лишь командой
запуска; сбор переиспользует общий ``_harvest_by_convention``.

Headless-прогон: ``claude -p`` (print mode, неинтерактивно). В изолированной
песочнице (egress запрещён) автономная правка файлов безопасна, поэтому
разрешения не выспрашиваем (``--dangerously-skip-permissions``). Реальный прогон
требует образа с установленным ``claude`` и кредами (эфемерно). В CI —
``StubSandboxRuntime`` (эмуляция через exec_handler), без Docker и сети.

Host-режим (Ф7e): тот же адаптер исполняется на хосте через
``HostSandboxRuntime`` и переиспользует залогиненную сессию claude CLI. Тогда у
прогона нет ОС-изоляции, поэтому ``host_security`` управляет правами агента:
``restricted`` — только файловые правки (без хостового shell); ``full`` —
полный доступ (``--dangerously-skip-permissions``, осознанный опт-ин). В docker
``host_security`` не задаётся (изоляция даёт сам контейнер).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..protocol import HarnessRunSpec, HarvestedArtifact
from ..sandbox import ResourceLimits, SandboxHandle, SandboxRuntime
from .base import _BRIEF_PATH, SandboxHarnessProvider, shell


class ClaudeCodeHarnessProvider(SandboxHarnessProvider):
    """Запускает Claude Code headless по brief; собирает выход по соглашению."""

    def __init__(
        self,
        *,
        sandbox: SandboxRuntime,
        image: str,
        model: str | None = None,
        name: str = "claude_code",
        resource_limits: ResourceLimits | None = None,
        default_timeout_s: int | None = None,
        host_security: str | None = None,
    ) -> None:
        super().__init__(
            sandbox=sandbox,
            image=image,
            name=name,
            model=model,
            resource_limits=resource_limits,
            default_timeout_s=default_timeout_s,
        )
        # None — docker (изоляция контейнером); "restricted"/"full" — host-режим.
        self._host_security = host_security

    def _build_command(self, spec: HarnessRunSpec) -> list[str]:
        # Постановку подаём как промпт (из файла, чтобы не упереться в лимиты
        # аргументов). Печать без интерактива. Модель — опциональна (образ может
        # задавать дефолт).
        parts = ['claude -p "$(cat ' + _BRIEF_PATH + ')"']
        if self._host_security == "restricted":
            # Host без ОС-изоляции: ограничиваем агента файловыми правками в
            # workspace — авто-приём правок, но без хостового shell и сети.
            parts.append("--permission-mode acceptEdits")
            parts.append('--disallowedTools "Bash WebFetch WebSearch"')
        else:
            # Docker (изоляция) или host full (опт-ин): полный доступ.
            parts.append("--dangerously-skip-permissions")
        if self.model:
            parts.append(f"--model {self.model}")
        return shell("cd /work && " + " ".join(parts))

    def _harvest(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> Sequence[HarvestedArtifact]:
        return self._harvest_by_convention(handle, spec)
