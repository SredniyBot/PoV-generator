"""Claude Code-адаптер (Ф7): автономный «строитель» как harness-провайдер.

Claude Code — многошаговый агент: берёт постановку и сам решает, что и как
сделать (под-агенты, инструменты, многофайловые правки). Для harness'а это
дефолтный адаптер под «собери X по спеке».

Это АГЕНТСКАЯ роль ``claude`` CLI (инструменты включены, много ходов). Вторая,
ортогональная роль того же CLI — LLM-провайдер (completion: «ответь JSON»,
инструменты выключены, один ход) — живёт в
``infrastructure/llm/providers/claude_subscription.py``. Делит их лишь
аутентификация (подписка ``claude login``); конфигурация — разная. Сбор — **по соглашению**: brief
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

import os
from collections.abc import Sequence

from ..protocol import HarnessRunSpec, HarvestedArtifact
from ..sandbox import ResourceLimits, SandboxHandle, SandboxRuntime
from .base import _BRIEF_PATH, SandboxHarnessProvider, shell


def _docker_mcp_config_path() -> str | None:
    """Путь к MCP-конфигу Docker для host-агента (подход B: агент собирает/тестит
    код в контейнерах через docker-инструменты и итерирует по результату).

    Включается, если задан ``POV_HARNESS_DOCKER_MCP_CONFIG`` — путь к JSON
    MCP-конфигу (формат claude CLI: ``{"mcpServers": {"docker": {...}}}``).
    Путь должен быть в POSIX-форме (bash на хосте), напр.
    ``/c/Users/me/.povgen/docker-mcp.json``. Пусто → MCP не подключаем (поведение
    как раньше)."""
    raw = (os.environ.get("POV_HARNESS_DOCKER_MCP_CONFIG") or "").strip()
    return raw or None


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
        # Печать без интерактива (``-p``). Модель — опциональна (образ/сессия
        # может задавать дефолт).
        flags: list[str] = []
        if self._host_security is None:
            # Docker (изоляция контейнером): полный автономный доступ безопасен.
            flags.append("--dangerously-skip-permissions")
        elif self._host_security == "restricted":
            # Host без ОС-изоляции: ограничиваем агента файловыми правками в
            # workspace — авто-приём правок, но без хостового shell и сети.
            flags.append("--permission-mode acceptEdits")
            flags.append('--disallowedTools "Bash WebFetch WebSearch"')
        else:
            # Host «full» (опт-ин, без ОС-изоляции): ``--dangerously-skip-permissions``
            # на неизолированном хосте — запуск автономного агента с полным
            # доступом к хосту, поэтому НЕ используем его здесь; даём рабочий
            # максимум — авто-приём правок со всеми инструментами. Для полностью
            # автономного агента (shell и т.п.) используйте docker-движок.
            flags.append("--permission-mode acceptEdits")
        # Docker MCP (подход B, ТОЛЬКО host-режим): даём агенту docker-инструменты
        # через MCP, чтобы он собирал/тестировал код в контейнерах и чинил по
        # результату (host-claude уже авторизован подпиской — мост авторизации не
        # нужен). В docker-движке это был бы docker-in-docker — не включаем.
        # --strict-mcp-config: грузим ТОЛЬКО наш конфиг (не утаскиваем чужие MCP
        # пользователя в автономного агента). Исполнение идёт через docker, host-
        # shell не нужен → Bash запрещаем (если ещё не запрещён restricted-веткой).
        mcp_config = _docker_mcp_config_path() if self._host_security is not None else None
        if mcp_config:
            flags.append(f'--mcp-config "{mcp_config}" --strict-mcp-config')
            flags.append('--allowedTools "mcp__*"')
            if not any("--disallowedTools" in f for f in flags):
                flags.append('--disallowedTools "Bash"')
        # Модель: явный override подключения ИЛИ настроенная LLM-модель проекта
        # (model_hint) — не выдуманный дефолт. Пусто → claude берёт свою.
        model = self.model or spec.model_hint
        if model:
            flags.append(f"--model {model}")
        # Бриф подаём ЧЕРЕЗ STDIN (``cat brief | claude -p``), а НЕ аргументом
        # (``claude -p "$(cat brief)"``): реальный бриф (системный+пользовательский
        # промпт+контекст) большой и в виде argv упирается в лимит длины аргументов
        # ОС → exec падает «Argument list too long» (код 126). stdin лимита не имеет.
        cmd = f"cat {_BRIEF_PATH} | claude -p " + " ".join(flags)
        return shell("cd /work && " + cmd.rstrip())

    def _harvest(
        self, handle: SandboxHandle, spec: HarnessRunSpec
    ) -> Sequence[HarvestedArtifact]:
        return self._harvest_by_convention(handle, spec)
